"""A backup source: a local .hbk folder plus optional rclone remotes and extra read-only roots, and the registry of configured
sources (``sources.json``). Sources are read-only; downloads land in the app cache only."""
import fnmatch, json, os, re, subprocess, threading, time, uuid
from pathlib import Path
from callido_restore import paths
from callido_restore.core.hbkmeta import rev_of
from callido_restore.paths import RCLONE
from callido_restore.sources import rclone

def is_remote_path(p):
    """rclone 'remote:path' rather than a local path."""
    return ":" in p and not p.startswith("/")

class Source:
    """A .hbk folder: local path, optionally mirrored on-demand from an rclone remote."""
    def __init__(self, sid, name, local, remote=None, extra=None):
        self.id, self.name = sid, name
        self.remotes = [r for r in (remote if isinstance(remote, list) else [remote]) if r]
        self.remote = self.remotes[0] if self.remotes else None
        self.local = Path(os.path.expanduser(local))
        self.extra = [Path(os.path.expanduser(x)) for x in (extra or []) if x]
        self._list = {}
        self._lock = threading.Lock()
        self.index_dir = paths.INDEX_DIR / sid
        self.index_dir.mkdir(parents=True, exist_ok=True)
        # Fetched metadata and pool objects land here, not in self.local / extra.
        self.cache_dir = paths.CACHE_DIR / sid
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def read_roots(self):
        """Configured folders. Read-only: the backup, extra roots, or a user-made mirror."""
        return [self.local, *self.extra]

    def in_cache(self, path):
        try:
            Path(path).resolve().relative_to(self.cache_dir.resolve())
            return True
        except (OSError, ValueError):
            return False

    def locate(self, rel):
        """Existing readable file, or None. Does not download or modify anything."""
        cached = self.cache_dir / rel
        if cached.is_file():
            return cached
        for root in self.read_roots():
            q = root / rel
            if q.is_file():
                return q
        return None

    def has_sized_file(self, rel, size):
        """True when the cache or a read-only root has ``rel`` at exactly ``size`` bytes."""
        try:
            expected = int(size)
        except (TypeError, ValueError):
            return False
        for root in (self.cache_dir, *self.read_roots()):
            p = root / rel
            try:
                if p.is_file() and p.stat().st_size == expected:
                    return True
            except OSError:
                continue
        return False

    def contains_dest(self, dest, shares=()):
        """The restore writes dest/<share>/<path> for every selected share. Return the first such written root that is the
        backup itself, lies inside it, or has the backup inside it (checked on every local root and every source remote),
        else "". With no shares only dest itself is checked and a parent of the backup passes: nothing is written into the
        .hbk then. The source is read-only, so a restore must never write into or around the .hbk; a restore back into the
        original shares (dest = the NAS root) is fine as long as the backup does not live in one of the restored shares."""
        d = dest.strip().replace("\\", "/").rstrip("/")
        remote = ":" in d and not d.startswith("/")
        def join(base, share):
            share = share.strip("/")
            if remote:
                rem, _, path = base.partition(":"); path = path.strip("/")
                return f"{rem}:{path}/{share}" if path else f"{rem}:{share}"
            return os.path.join(base, share)
        roots = [join(d, sh) for sh in shares if sh and sh.strip("/")] if shares else [d]
        for w in roots:
            if self._writes_into_source(w, remote, both_ways=bool(shares)): return w
        return ""

    def contains_root(self, root):
        """True when an exact written root (an H8 per-folder target) is the backup, lies inside it, or has it inside."""
        r = root.strip().replace("\\", "/").rstrip("/")
        return bool(r) and self._writes_into_source(r, is_remote_path(r), both_ways=True)

    def _writes_into_source(self, w, remote, both_ways):
        """True when the written root w equals or lies inside a source path, or (both_ways) has a source path inside it."""
        if remote:
            rem, _, path = w.partition(":"); path = path.strip("/")
            for r in self.remotes:
                rr, _, rp = r.partition(":"); rp = rp.strip("/")
                if rem != rr: continue
                if path == rp or path.startswith(rp + "/"): return True
                if both_ways and (path == "" or rp.startswith(path + "/")): return True
            return False
        try: dp = Path(os.path.expanduser(w)).resolve()
        except OSError: dp = Path(os.path.expanduser(w))
        roots = self.read_roots() + [Path(r) for r in self.remotes if r.startswith("/")]     # a "remote" may be a plain folder
        for root in roots:
            try: rr = root.resolve()
            except OSError: rr = root
            if dp == rr or rr in dp.parents: return True
            if both_ways and dp in rr.parents: return True
        return False

    def to_json(self):
        return {"id": self.id, "name": self.name, "local": str(self.local), "remote": self.remote, "remotes": self.remotes, "extra": [str(x) for x in self.extra],
                "local_exists": self.local.is_dir()}

    def listdir(self, rel, refresh=False):
        """Merged listing of a relative dir: {name: {size, dir, local, remote}}."""
        rel = rel.strip("/")
        with self._lock:
            if rel in self._list and not refresh: return self._list[rel]
        ents = {}
        for root in [self.cache_dir, *self.read_roots()]:          # a file fetched into the app cache is local too
            p = root / rel if rel else root
            if not p.is_dir(): continue
            try: it = list(p.iterdir())
            except OSError: continue
            for e in it:
                try: st = e.stat()
                except OSError: continue
                if e.name in ents: continue
                ents[e.name] = {"size": st.st_size if e.is_file() else 0, "dir": e.is_dir(), "local": True, "remote": False}
        for remote in self.remotes:
            try: entries = rclone.ls(f"{remote}/{rel}" if rel else remote, no_modtime=True)
            except rclone.RcloneError: continue                 # an unreachable remote adds nothing (as before)
            for e in entries:
                ent = ents.setdefault(e["Name"], {"size": e["Size"], "dir": e["IsDir"], "local": False, "remote": False})
                ent["remote"] = True
                if not ent["local"]: ent["size"] = e["Size"]
        with self._lock: self._list[rel] = ents
        return ents

    def find(self, rel, pattern):
        return {n: e for n, e in self.listdir(rel).items() if fnmatch.fnmatch(n, pattern)}

    def latest(self, rel, base):
        """Highest-revision file 'base.N' in rel. Returns (name, entry, rev, locked) or None."""
        best = None
        for n, e in self.listdir(rel).items():
            if e["dir"]: continue
            r = rev_of(n, base)
            if r and (best is None or r[0] > best[2]): best = (n, e, r[0], r[1])
        return best

    def ensure(self, rel, job=None):
        """Return a readable path for rel.

        An existing file in the app cache or in a source folder is reused (so an
        earlier download into a mirror is not fetched again). A missing file is
        downloaded from the remote into the app cache only. Source folders are
        not created, replaced, or deleted.
        """
        found = self.locate(rel)
        if found is not None:
            return found
        if not self.remotes:
            raise FileNotFoundError(f"not in local copy: {rel}")
        dest = self.cache_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part"); errs = []
        for attempt in range(4):                       # outer retry: 4 rounds over all remotes, growing pauses
            for remote in self.remotes:
                if job: job["message"] = f"fetching {rel} from {remote.split(':')[0]}" + (f" (retry {attempt})" if attempt else "")
                # copyto reads the remote and writes the local temp file. Never the other way.
                r = subprocess.run([RCLONE, "copyto", f"{remote}/{rel}", str(tmp), "--retries", "5", "--low-level-retries", "20",
                                    "--retries-sleep", "5s", "--tpslimit", "8", "--drive-pacer-min-sleep", "100ms"], capture_output=True, text=True)
                if r.returncode == 0 and tmp.is_file():
                    tmp.rename(dest)
                    parent = str(Path(rel).parent).strip("/")
                    with self._lock: self._list.pop("" if parent == "." else parent, None)   # that listing is stale now (root key is "")
                    return dest
                errs.append(f"{remote}: {r.stderr.strip()[-160:]}")
                if tmp.exists(): tmp.unlink()
            time.sleep([3, 15, 60, 0][attempt])
        raise RuntimeError(f"fetch failed for {rel} after retries: {errs[-1] if errs else '?'}")

SOURCES = {}

def load_sources():
    paths.CONF_DIR.mkdir(parents=True, exist_ok=True)
    if paths.CONF_FILE.is_file():
        for s in json.loads(paths.CONF_FILE.read_text()):
            SOURCES[s["id"]] = Source(s["id"], s["name"], s["local"], s.get("remotes") or s.get("remote"), s.get("extra"))

def save_sources():
    paths.CONF_FILE.write_text(json.dumps([{"id": s.id, "name": s.name, "local": str(s.local), "remotes": s.remotes, "extra": [str(x) for x in s.extra]} for s in SOURCES.values()], indent=1))

def add_source(name, local, remote, extra=None):
    sid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or uuid.uuid4().hex[:6]
    while sid in SOURCES: sid += "x"
    SOURCES[sid] = Source(sid, name, local, remote, extra); save_sources(); return SOURCES[sid]
