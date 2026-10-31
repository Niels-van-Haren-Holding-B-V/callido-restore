"""A backup source: a local .hbk folder plus optional rclone remotes and extra read-only roots, and the registry of configured
sources (``sources.json``). Sources are read-only; downloads land in the app cache only."""
import fnmatch, json, os, re, subprocess, threading, time, uuid
from pathlib import Path
from callido_restore import paths
from callido_restore.core.hbkmeta import rev_of
from callido_restore.paths import RCLONE, is_local_abs, is_remote_path  # noqa: F401  (is_remote_path is re-exported)
from callido_restore.sources import rclone

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
        # Remote folder listings survive a restart (listings.json): opening a backup again costs no Drive round trips.
        # A listing older than LISTING_TTL seconds, or a refresh (the ↻ button, the pool index fetch), asks the remote again.
        self._remote_lists = self._load_listings()
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
        remote = is_remote_path(d)
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
        roots = self.read_roots() + [Path(r) for r in self.remotes if is_local_abs(r)]     # a "remote" may be a plain folder
        for root in roots:
            try: rr = root.resolve()
            except OSError: rr = root
            if dp == rr or rr in dp.parents: return True
            if both_ways and dp in rr.parents: return True
        return False

    def to_json(self):
        return {"id": self.id, "name": self.name, "local": str(self.local), "remote": self.remote, "remotes": self.remotes, "extra": [str(x) for x in self.extra],
                "local_exists": self.local.is_dir()}

    LISTING_TTL = 3600.0

    def _listings_file(self): return self.index_dir / "listings.json"

    def _load_listings(self):
        try: d = json.loads(self._listings_file().read_text())
        except (OSError, ValueError): return {}
        return d if isinstance(d, dict) else {}

    def _save_listings(self):
        tmp = self._listings_file().with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._remote_lists)); os.replace(tmp, self._listings_file())
        except OSError: pass

    def forget_listings(self):
        """Drop every cached folder listing (memory and disk): the next listdir asks the remote again."""
        with self._lock: self._list.clear(); self._remote_lists.clear()
        try: self._listings_file().unlink()
        except OSError: pass

    def _remote_entries(self, rel, refresh):
        """Remote entries of ``rel`` as {name: {size, dir}}: from listings.json when fresh enough, else from the remotes."""
        with self._lock: saved = None if refresh else self._remote_lists.get(rel)
        if saved and time.time() - saved.get("t", 0) < self.LISTING_TTL: return saved["e"]
        if not self.remotes: return {}
        out = {}; asked = False
        for remote in self.remotes:
            try: entries = rclone.ls(f"{remote}/{rel}" if rel else remote, no_modtime=True)
            except rclone.RcloneError: continue                 # an unreachable remote adds nothing (as before)
            asked = True
            for e in entries: out.setdefault(e["Name"], {"size": e["Size"], "dir": e["IsDir"]})
        if asked:
            with self._lock: self._remote_lists[rel] = {"t": time.time(), "e": out}
            self._save_listings()
        elif saved: return saved["e"]                            # remote unreachable right now: the stale listing beats nothing
        return out

    def listdir(self, rel, refresh=False):
        """Merged listing of a relative dir: {name: {size, dir, local, remote}}. Local roots are read fresh, the remote part comes
        from listings.json when it is younger than LISTING_TTL (see ``_remote_entries``)."""
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
        for name, e in self._remote_entries(rel, refresh).items():
            ent = ents.setdefault(name, {"size": e["size"], "dir": e["dir"], "local": False, "remote": False})
            ent["remote"] = True
            if not ent["local"]: ent["size"] = e["size"]
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

    def ensure(self, rel, job=None, progress=None, cancelled=None):
        """Return a readable path for rel.

        An existing file in the app cache or in a source folder is reused (so an
        earlier download into a mirror is not fetched again). A missing file is
        downloaded from the remote into the app cache only. Source folders are
        not created, replaced, or deleted. ``progress(bytes, speed, note)`` follows
        the download (through the rclone daemon: live bytes, speed and a
        'rate-limited, retrying' note); ``cancelled()`` true aborts it.
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
                # the copy reads the remote and writes the local temp file. Never the other way.
                try:
                    rclone.copyfile(f"{remote}/{rel}", tmp, progress=progress, cancelled=cancelled)
                    ok = tmp.is_file()
                except rclone.Cancelled:
                    if tmp.exists(): tmp.unlink()
                    raise RuntimeError(f"fetch of {rel} cancelled")
                except rclone.RcloneError as e:
                    ok = False; errs.append(f"{remote}: {str(e)[-160:]}")
                if ok:
                    tmp.rename(dest)
                    parent = str(Path(rel).parent).strip("/")
                    with self._lock: self._list.pop("" if parent == "." else parent, None)   # that listing is stale now (root key is "")
                    return dest
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
