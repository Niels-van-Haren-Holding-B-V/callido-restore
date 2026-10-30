#!/usr/bin/env python3
"""
Hyper Backup Explorer — browse a Synology Hyper Backup (.hbk) on a timeline.

Works on a local .hbk folder (complete or partial) or on an rclone remote
(e.g. gdrive:Synology/droopy_1.hbk); remote metadata files are fetched on
demand into a local cache directory. Only the small metadata databases are
needed to browse; the data pool is only touched when extracting a file.

Usage:  python3 hbk_explorer.py [--port 8765] [--browser | --no-window] [--smoke]
        (app window by default; --browser uses the system browser, --no-window runs only the server, --smoke is a build check)
"""
import argparse, collections, fnmatch, json, os, re, sqlite3, subprocess, sys, threading, time, traceback, uuid, zlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

def _resource_roots():
    """Directories that may hold bundled files (static/, rclone).

    Source checkout: the directory of this file. PyInstaller sets sys._MEIPASS
    (onedir `_internal`, or Contents/Frameworks inside a macOS .app). Data files
    in a .app live under Contents/Resources and are normally cross-linked into
    Frameworks; sibling folders are checked so a missing link still resolves.
    """
    roots, seen = [], set()
    def add(p):
        key = os.path.normpath(str(p))
        if key not in seen:
            seen.add(key); roots.append(Path(key))
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass: add(meipass)
        exe_dir = Path(sys.executable).resolve().parent
        add(exe_dir); add(exe_dir / "_internal")
        if exe_dir.name == "MacOS":
            contents = exe_dir.parent
            add(contents / "Frameworks"); add(contents / "Resources")
    else:
        add(Path(__file__).resolve().parent)
    return roots
def _bundled(rel):
    for root in _resource_roots():
        p = root / rel
        if p.is_file(): return p
    return None
def _ensure_executable(path):
    """Copies of a collected binary sometimes lose the executable bit."""
    if os.name == "nt" or os.access(path, os.X_OK): return True
    try: path.chmod(path.stat().st_mode | 0o755)
    except OSError: return False
    return os.access(path, os.X_OK)
def _find_rclone():
    names = ("rclone.exe", "rclone") if os.name == "nt" else ("rclone",)
    found = []
    for root in _resource_roots():
        for name in names:
            c = root / name
            if c.is_file(): found.append(c)
    found.sort(key=lambda p: (p.is_symlink(), str(p)))  # real file before a cross-link
    for c in found:
        if _ensure_executable(c): return str(c)
    # A packaged app must not silently run whatever rclone is on the machine.
    if getattr(sys, "frozen", False):
        return ""
    for c in (Path("/opt/homebrew/bin/rclone"), Path("/usr/local/bin/rclone")):
        if c.is_file() and os.access(c, os.X_OK): return str(c)
    return "rclone.exe" if os.name == "nt" else "rclone"
RCLONE = _find_rclone()
def _verify_frozen():
    """Fail fast when a packaged build is missing modules or bundled files."""
    if not getattr(sys, "frozen", False): return
    import hbk_pool, hbk_poolmap, lz4.block, webview  # noqa: F401
    if _bundled("static/index.html") is None:
        roots = ", ".join(str(r) for r in _resource_roots())
        raise SystemExit("bundled static/index.html not found in: " + roots)
    if not RCLONE or not Path(RCLONE).is_file() or not os.access(RCLONE, os.X_OK):
        raise SystemExit("bundled rclone is missing or not executable; refusing to use an rclone installed on the system")
CONF_DIR = Path.home() / ".hbk-explorer"
CONF_FILE = CONF_DIR / "sources.json"
INDEX_DIR = CONF_DIR / "index"
CACHE_DIR = CONF_DIR / "cache"   # downloaded bytes only; never a configured source folder
JOBS_DIR = CONF_DIR / "jobs"
VERSION = "0.1.0"
ROOT_PARENT = "5058F1AF5058F1AF8388633F609CADB75A75DC9D"   # pname_id_v2 of share-root entries
RANSOM_SUFFIX = ".L0CK3D"
RANSOM_NOTE = re.compile(r"^(read-me.*\.txt|.*how[_ -]to[_ -](decrypt|recover|restore).*|.*(your|all)[_ -]files.*\.(txt|html)|.*\.L0CK3D)$", re.I)

SCRIPT_EXT = {"sh","bash","zsh","py","pl","php","rb","ps1","bat","cmd","vbs","vbe","js","jse","wsf","hta","jar","elf",
              "bin","run","so","ko","dylib","exe","dll","scr","com","msi","apk","lnk","pyc","cgi","phtml","php5","phar","deb","rpm","spk"}
BENIGN_EXEC_EXT = {"jpg","jpeg","png","gif","heic","mov","mp4","mp3","pdf","docx","xlsx","pptx","txt","nef","raw","cr2","avi","m4a","m4v","zip","7z","rar"}
DOC_EXT = {"pdf","doc","docx","xls","xlsx","ppt","pptx","txt","rtf","odt","jpg","jpeg","png"}

# ----------------------------------------------------------------------------- helpers
def jdump(o): return json.dumps(o, ensure_ascii=False).encode("utf-8")
def rev_of(name, base):
    """'version_info.db.2334' -> 2334 ; also tolerates ransomware suffix."""
    m = re.match(r"^" + re.escape(base) + r"\.(\d+)(" + re.escape(RANSOM_SUFFIX) + r")?$", name)
    return (int(m.group(1)), bool(m.group(2))) if m else None
def is_sqlite(p):
    try:
        with open(p, "rb") as f: return f.read(16) == b"SQLite format 3\x00"
    except OSError: return False
def human(n):
    for u in ["B","KB","MB","GB","TB"]:
        if n < 1024: return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"

JOBS = {}
def start_job(fn, label):
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "label": label, "state": "running", "progress": 0.0, "message": "starting", "result": None, "error": None, "started": time.time()}
    JOBS[jid] = job
    def run():
        try:
            job["result"] = fn(job); job["state"] = "done"
            if not (isinstance(job["result"], dict) and job["result"].get("stopped")): job["progress"] = 1.0
        except Exception as e:
            job["state"] = "error"; job["error"] = f"{e}"; job["trace"] = traceback.format_exc()
    threading.Thread(target=run, daemon=True).start()
    return job

# ----------------------------------------------------------------------------- source
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
        self.index_dir = INDEX_DIR / sid
        self.index_dir.mkdir(parents=True, exist_ok=True)
        # Fetched metadata and pool objects land here, not in self.local / extra.
        self.cache_dir = CACHE_DIR / sid
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
            r = subprocess.run([RCLONE, "lsjson", "--no-modtime", "--no-mimetype", f"{remote}/{rel}" if rel else remote],
                               capture_output=True, text=True)
            if r.returncode == 0:
                for e in json.loads(r.stdout or "[]"):
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

# ----------------------------------------------------------------------------- backup
class Backup:
    def __init__(self, src: Source):
        self.src = src
        self.info = {"source": src.to_json(), "ransomware": {"locked_files": 0, "notes": [], "examples": []},
                     "task": {}, "shares": [], "versions": [], "readable": False, "problems": []}
        self.versions = []
        self._snap_cache = {}
        self.scan()

    # --- discovery
    def scan(self):
        s = self.src; info = self.info
        top = s.listdir("")
        locked, notes, ex = 0, [], []
        def tally(ents):
            nonlocal locked
            for n in ents:
                if n.endswith(RANSOM_SUFFIX):
                    locked += 1
                    if len(ex) < 5: ex.append(n)
                if RANSOM_NOTE.match(n): notes.append(n)
        tally(top)
        cfg = s.listdir("Config") if "Config" in top else {}
        tally(cfg)
        shares = sorted(n for n, e in s.listdir("Config/@Share").items() if e["dir"]) if "@Share" in cfg else []
        for sh in shares: tally(s.listdir(f"Config/@Share/{sh}"))
        info["ransomware"] = {"locked_files": locked, "notes": notes, "examples": ex}
        info["layout"] = {"top": sorted(top), "config": sorted(cfg), "pool_present": "Pool" in top, "control_present": "Control" in top}
        # task config
        if "_Syno_TaskConfig" in top:
            try:
                txt = s.ensure("_Syno_TaskConfig").read_text(errors="replace")
                task = {}
                for line in txt.splitlines():
                    if "=" in line and not line.startswith("["):
                        k, v = line.split("=", 1)
                        try: v = json.loads(v)
                        except Exception: pass
                        task[k.strip()] = v
                info["task"] = task
            except Exception as e: info["problems"].append(f"task config: {e}")
        # versions
        vi = s.latest("Config", "version_info.db")
        if not vi: info["problems"].append("Config/version_info.db not found"); 
        elif vi[3]: info["problems"].append(f"version database is ransomware-encrypted ({vi[0]})")
        else:
            p = s.ensure(f"Config/{vi[0]}")
            if not is_sqlite(p): info["problems"].append(f"{vi[0]} is not a readable SQLite file (encrypted?)")
            else:
                con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                for row in con.execute("select id, timestamp, status, share, depose_time from version_info order by id"):
                    self.versions.append({"id": row[0], "ts": row[1], "status": row[2], "depose": row[4]})
                con.close()
                info["readable"] = True
        # storage statistics (per-version new/modified/removed counts)
        st = s.latest("", "storage_statistics.db")
        stats = {}
        if st and not st[3]:
            try:
                p = s.ensure(st[0]); con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                for r in con.execute("select version_id, total_count, new_count, modify_count, remove_count, source_size, start_time, end_time from source_table"):
                    stats[r[0]] = {"total": r[1], "new": r[2], "mod": r[3], "rem": r[4], "src_size": r[5], "start": r[6], "end": r[7]}
                con.close()
            except Exception as e: info["problems"].append(f"storage statistics: {e}")
        for v in self.versions:
            v["stats"] = stats.get(v["id"])
            v["indexed"] = []   # shares indexed locally for this version
        # shares + which snapshot dbs are available
        self.shares = shares
        for sh in shares:
            ents = s.listdir(f"Config/@Share/{sh}")
            vers = {}
            for n, e in ents.items():
                m = re.match(r"^(\d+)\.db\.(\d+)(" + re.escape(RANSOM_SUFFIX) + r")?$", n)
                if m: vers[int(m.group(1))] = {"name": n, "size": e["size"], "local": e["local"], "locked": bool(m.group(3))}
            info["shares"].append({"name": sh, "snapshots": len(vers), "min": min(vers) if vers else None, "max": max(vers) if vers else None,
                                   "locked": sum(1 for v in vers.values() if v["locked"])})
            setattr(self, "_vers_" + sh, vers)
        for ix in self.src.index_dir.glob("*.sqlite"):
            m = re.match(r"^(.*)__(\d+)\.sqlite$", ix.name)
            if m:
                for v in self.versions:
                    if v["id"] == int(m.group(2)): v["indexed"].append(m.group(1))
        info["versions"] = self.versions
        # annotate versions with counts from derived indexes (more accurate than stats, covers old versions)
        for v in self.versions:
            if v["indexed"]:
                v["counts"] = {}
                for sh in v["indexed"]:
                    meta = self._meta(sh, v["id"])
                    if meta: v["counts"][sh] = meta

    def share_versions(self, share): return getattr(self, "_vers_" + share, {})

    # --- snapshot index
    def index_path(self, share, ver): return self.src.index_dir / f"{share}__{ver}.sqlite"
    def _meta(self, share, ver):
        p = self.index_path(share, ver).with_suffix(".json")
        try: return json.loads(p.read_text())
        except Exception: return None

    def snapshot_info(self, share, ver):
        e = self.share_versions(share).get(ver)
        return {"share": share, "ver": ver, "available": bool(e), "local": e["local"] if e else False,
                "size": e["size"] if e else 0, "locked": e["locked"] if e else False,
                "indexed": self.index_path(share, ver).is_file(), "meta": self._meta(share, ver)}

    def build_index(self, share, ver, job):
        e = self.share_versions(share).get(ver)
        if not e: raise FileNotFoundError(f"no snapshot db for {share} version {ver}")
        if e["locked"]: raise RuntimeError(f"snapshot {e['name']} is ransomware-encrypted")
        out = self.index_path(share, ver)
        if out.is_file(): return str(out)
        src = self.src.ensure(f"Config/@Share/{share}/{e['name']}", job)
        if not is_sqlite(src): raise RuntimeError(f"{e['name']} is not SQLite (encrypted?)")
        job["message"] = "reading snapshot"; job["progress"] = 0.3
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        rows = con.execute("""select hex(name_id_v2), hex(pname_id_v2), file_name, mtime_sec, ctime_sec, size, mode, status,
                               inode, off_virtual_file, dedup_id, version_id, cr_time, nlink from version_list""").fetchall()
        con.close()
        parent = {r[0]: (r[1], r[2]) for r in rows}
        paths = {ROOT_PARENT: ""}
        def resolve(i):
            stack, cur, guard = [], i, 0
            while cur not in paths:
                if cur not in parent or guard > 4096:
                    paths[cur] = ""; break
                stack.append(cur); cur = parent[cur][0]; guard += 1
            base = paths[cur]
            for x in reversed(stack):
                nm = parent[x][1]
                base = f"{base}/{nm}" if base else nm
                paths[x] = base
            return paths[i]
        job["message"] = "resolving paths"; job["progress"] = 0.5
        tmp = out.with_suffix(".tmp")
        if tmp.exists(): tmp.unlink()
        db = sqlite3.connect(tmp)
        db.executescript("""
            create table files(id text primary key, pid text, path text, name text, dir text, ext text, size integer,
                mtime integer, ctime integer, mode integer, is_dir integer, is_exec integer, status text, inode integer,
                off integer, dedup integer, nlink integer, flags text, depth integer);
        """)
        now = time.time()
        batch, n, counts, flagc, total = [], 0, {}, {}, len(rows)
        for r in rows:
            nid, pid, name, mtime, ctime, size, mode, status, inode, off, dedup, vid, crt, nlink = r
            path = resolve(nid)
            d = path.rsplit("/", 1)[0] if "/" in path else ""
            fmt = (mode or 0) & 0o170000
            is_dir = 1 if fmt == 0o040000 else 0
            is_link = fmt == 0o120000
            is_exec = 1 if (not is_dir and (mode or 0) & 0o111) else 0
            ext = name.rsplit(".", 1)[1].lower() if "." in name[1:] else ""
            flags = []
            if name.endswith(RANSOM_SUFFIX) or RANSOM_NOTE.match(name): flags.append("ransom")
            syno_meta = "@eaDir" in path or "@Syno" in name
            if not is_dir and not syno_meta:
                if ext in SCRIPT_EXT: flags.append("script")
                if is_exec and ext == "" and size and name not in (".DS_Store", ".localized", "Icon\r"): flags.append("exec")
                if is_link: flags.append("symlink")
                parts = name.lower().split(".")
                if len(parts) >= 3 and parts[-1] in SCRIPT_EXT and parts[-2] in DOC_EXT: flags.append("double-ext")
                if name in ("authorized_keys", "id_rsa", "id_ed25519", "id_dsa", "known_hosts") or "/.ssh/" in f"/{path}": flags.append("ssh")
                if name in ("crontab", ".bash_history", ".profile", ".bashrc", ".zshrc", "synoinfo.conf", "passwd", "shadow", "sudoers"): flags.append("sensitive")
                if mtime and ctime and mtime > ctime + 2: flags.append("timestomp")
                if mtime and mtime > now + 86400: flags.append("future")
                if status not in ("U",): flags.append("changed")
            if name.startswith(".") and name not in (".DS_Store",) and not name.startswith("._"): flags.append("hidden")
            fl = ",".join(flags)
            for f in flags: flagc[f] = flagc.get(f, 0) + 1
            counts[status] = counts.get(status, 0) + 1
            batch.append((nid, pid, path, name, d, ext, size or 0, mtime, ctime, mode, is_dir, is_exec, status, inode, off, dedup, nlink, fl, path.count("/")))
            n += 1
            if len(batch) >= 5000:
                db.executemany("insert or replace into files values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch); batch = []
                job["progress"] = 0.5 + 0.4 * n / max(total, 1); job["message"] = f"indexing {n:,}/{total:,}"
        if batch: db.executemany("insert or replace into files values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        job["message"] = "creating indexes"; job["progress"] = 0.92
        db.executescript("""
            create index f_path on files(path); create index f_dir on files(dir); create index f_mtime on files(mtime);
            create index f_ctime on files(ctime); create index f_status on files(status); create index f_name on files(name);
            create index f_size on files(size);
        """)
        db.commit(); db.close()
        tmp.rename(out)
        meta = {"share": share, "ver": ver, "rows": total, "status": counts, "flags": flagc,
                "files": sum(1 for r in rows if ((r[6] or 0) & 0o170000) != 0o040000),
                "bytes": sum((r[5] or 0) for r in rows if ((r[6] or 0) & 0o170000) != 0o040000)}
        out.with_suffix(".json").write_text(json.dumps(meta))
        for v in self.versions:
            if v["id"] == ver and share not in v["indexed"]:
                v["indexed"].append(share); v.setdefault("counts", {})[share] = meta
        return str(out)

    def conn(self, share, ver):
        p = self.index_path(share, ver)
        if not p.is_file(): raise FileNotFoundError(f"{share} v{ver} not indexed")
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True); c.row_factory = sqlite3.Row
        return c

    def indexed_shares(self, ver):
        return [sh for sh in self.shares if self.index_path(sh, ver).is_file()]

    # --- queries
    @staticmethod
    def _where(q):
        w, args = [], []
        if q.get("q"):
            for term in q["q"].split():
                neg = term.startswith("-"); term = term.lstrip("-")
                if not term: continue
                if term.startswith("ext:"): w.append(("ext != ?" if neg else "ext = ?")); args.append(term[4:].lower())
                elif term.startswith("flag:"): w.append(("flags not like ?" if neg else "flags like ?")); args.append(f"%{term[5:]}%")
                elif term.startswith("status:"): w.append(("status != ?" if neg else "status = ?")); args.append(term[7:].upper())
                elif term.startswith("path:"): w.append(("path not like ?" if neg else "path like ?")); args.append(f"%{term[5:]}%")
                elif term.startswith("size>"): w.append("size > ?"); args.append(int(term[5:]))
                elif term.startswith("size<"): w.append("size < ?"); args.append(int(term[5:]))
                else: w.append(("name not like ?" if neg else "path like ?")); args.append(f"%{term}%")
        if q.get("flags"):
            for f in q["flags"].split(","): w.append("flags like ?"); args.append(f"%{f}%")
        if q.get("status"): w.append("status = ?"); args.append(q["status"].upper())
        if q.get("dir") is not None and q.get("dir") != "*": w.append("dir = ?"); args.append(q["dir"])
        if q.get("prefix"): w.append("(path = ? or path like ?)"); args.extend([q["prefix"], q["prefix"] + "/%"])
        by = q.get("by") or "mtime"
        if by not in ("mtime", "ctime"): by = "mtime"
        if q.get("from"): w.append(f"{by} >= ?"); args.append(int(q["from"]))
        if q.get("to"): w.append(f"{by} < ?"); args.append(int(q["to"]))
        if q.get("files_only"): w.append("is_dir = 0")
        if q.get("dirs_only"): w.append("is_dir = 1")
        return (" where " + " and ".join(w)) if w else "", args

    def query(self, share, ver, q):
        shares = self.indexed_shares(ver) if share == "*" else [share]
        where, args = self._where(q)
        sort = q.get("sort") or "mtime"
        if sort not in ("mtime", "ctime", "size", "path", "name", "status"): sort = "mtime"
        order = "asc" if q.get("order") == "asc" else "desc"
        limit = min(int(q.get("limit") or 500), 5000); offset = int(q.get("offset") or 0)
        rows, total = [], 0
        for sh in shares:
            c = self.conn(sh, ver)
            total += c.execute(f"select count(*) from files{where}", args).fetchone()[0]
            for r in c.execute(f"select * from files{where} order by {sort} {order} nulls last limit ? offset ?", args + [limit + offset, 0]):
                d = dict(r); d["share"] = sh; rows.append(d)
            c.close()
        rows.sort(key=lambda d: (d[sort] is None, d[sort]), reverse=(order == "desc"))
        return {"total": total, "rows": rows[offset:offset + limit], "shares": shares}

    def children(self, share, ver, d):
        c = self.conn(share, ver)
        rows = [dict(r) for r in c.execute("select * from files where dir = ? order by is_dir desc, name collate nocase", (d,))]
        # child counts for dirs
        for r in rows:
            if r["is_dir"]:
                r["n"] = c.execute("select count(*) from files where dir = ?", (r["path"],)).fetchone()[0]
            r["share"] = share
        c.close(); return rows

    def histogram(self, share, ver, q):
        shares = self.indexed_shares(ver) if share == "*" else [share]
        by = q.get("by") or "mtime"; by = by if by in ("mtime", "ctime") else "mtime"
        bucket = q.get("bucket") or "day"
        fmt = {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y", "hour": "%Y-%m-%d %H"}.get(bucket, "%Y-%m-%d")
        q = dict(q); q["files_only"] = 1
        where, args = self._where(q)
        agg = {}
        for sh in shares:
            c = self.conn(sh, ver)
            for k, n, b in c.execute(f"select strftime('{fmt}', {by}, 'unixepoch') k, count(*), sum(size) from files{where} group by k", args):
                a = agg.setdefault(k, [0, 0]); a[0] += n; a[1] += (b or 0)
            c.close()
        return {"by": by, "bucket": bucket, "buckets": [{"k": k, "n": v[0], "bytes": v[1]} for k, v in sorted(agg.items()) if k]}

    def file_detail(self, share, ver, fid):
        c = self.conn(share, ver)
        r = c.execute("select * from files where id = ?", (fid,)).fetchone()
        c.close()
        if not r: return None
        d = dict(r); d["share"] = share
        d["mode_oct"] = oct(d["mode"] or 0)[2:]
        # presence in other indexed versions of this share
        hist = []
        for v in self.versions:
            if share in v["indexed"]:
                cc = self.conn(share, v["id"])
                rr = cc.execute("select status, size, mtime, ctime from files where path = ?", (d["path"],)).fetchone()
                cc.close()
                hist.append({"ver": v["id"], "ts": v["ts"], "present": bool(rr), **({"status": rr[0], "size": rr[1], "mtime": rr[2], "ctime": rr[3]} if rr else {})})
        d["history"] = hist
        return d

    def diff(self, share, a, b, limit=5000):
        ca, cb = self.conn(share, a), self.conn(share, b)
        A = {r["path"]: r for r in ca.execute("select path,size,mtime,ctime,mode,is_dir,status,flags from files")}
        B = {r["path"]: r for r in cb.execute("select path,size,mtime,ctime,mode,is_dir,status,flags from files")}
        ca.close(); cb.close()
        added = [dict(B[p]) for p in B.keys() - A.keys()]
        removed = [dict(A[p]) for p in A.keys() - B.keys()]
        modified = []
        for p in A.keys() & B.keys():
            x, y = A[p], B[p]
            if x["is_dir"] == 0 and (x["size"] != y["size"] or x["mtime"] != y["mtime"]):
                dd = dict(y); dd["old_size"] = x["size"]; dd["old_mtime"] = x["mtime"]; modified.append(dd)
        for lst in (added, removed, modified): lst.sort(key=lambda d: d["path"])
        return {"share": share, "a": a, "b": b, "added": added[:limit], "removed": removed[:limit], "modified": modified[:limit],
                "n_added": len(added), "n_removed": len(removed), "n_modified": len(modified)}

    # --- pool access
    @property
    def pool(self):
        import hbk_pool
        mp = self.src.index_dir / "pool" / "poolmap.json"
        mt = mp.stat().st_mtime if mp.is_file() else 0
        if getattr(self, "_pool", None) is None or getattr(self, "_pool_mtime", None) != mt:
            self._pool = hbk_pool.PoolReader(self.src, str(self.src.index_dir / "pool")); self._pool_mtime = mt
        return self._pool
    def pool_status(self):
        st = self.pool.status(); st["lz4"] = True
        try: import lz4  # noqa
        except Exception: st["lz4"] = False
        return st
    def build_pool(self, job, max_bucket=None):
        import hbk_pool, hbk_poolmap, hbk_indexfetch
        pool_dir = self.src.index_dir / "pool"; pool_dir.mkdir(parents=True, exist_ok=True)
        log = lambda m: job.__setitem__("message", str(m))
        def stopped(stage):
            job["message"] = f"stopped during {stage} — run Build again to continue"
            return {"stopped": True, "stage": stage}
        if job.get("cancel"): return stopped("start")
        listing = pool_dir / "listing.json"
        if not listing.is_file():
            job["message"] = "listing pool directories"; out = []
            dirs = sorted(int(n) for n, e in self.src.listdir("Pool/0").items() if e["dir"] and n.isdigit())
            for i, d in enumerate(dirs):
                if job.get("cancel"): return stopped("listing")
                job["progress"] = 0.05 * i / max(1, len(dirs))
                for n, e in self.src.listdir(f"Pool/0/{d}", refresh=True).items():
                    m = re.match(r"^(\d+)\.(bucket|index)\.(\d+)$", n)
                    if m: out.append({"b": d * 2048 + int(m.group(1)), "t": m.group(2), "rev": int(m.group(3)), "size": e["size"], "mtime": "", "name": n})
            tmp = listing.with_name(".listing.json.part")
            tmp.write_text(json.dumps(out)); os.replace(tmp, listing)
        if job.get("cancel"): return stopped("listing")
        if not all((pool_dir / f"runs_set{k}.bin").is_file() for k in (1, 2, 3, 4)):
            job["message"] = "parsing chunk records (downloads the 4 file_chunk index sets if needed)"; job["progress"] = 0.1
            hbk_pool.build_runs(self.src, str(pool_dir), log)
        if job.get("cancel"): return stopped("chunk-records")
        job["message"] = "collecting file sizes from loaded snapshots"; job["progress"] = 0.3
        dbs = [(str(p), "index") for p in self.src.index_dir.glob("*__*.sqlite")]
        for sh in self.shares:
            for n, e in self.src.listdir(f"Config/@Share/{sh}").items():
                if e["local"] and re.match(r"^\d+\.db\.\d+$", n): dbs.append((str(self.src.ensure(f"Config/@Share/{sh}/{n}")), "raw"))
        hbk_pool.build_recsizes(self.src, dbs, str(pool_dir), log)
        if job.get("cancel"): return stopped("record-sizes")
        entries = json.loads(listing.read_text())
        # New index bytes go to the app cache. Files already in the source or a mirror are only read.
        fetch = hbk_indexfetch.fetch_bucket_indexes(
            self.src.cache_dir, self.src.remotes, entries, job, readonly_roots=self.src.read_roots())
        self.src._list.clear()
        if fetch.get("stopped") or job.get("cancel"):
            fetch["stopped"] = True
            return fetch
        if fetch.get("failed") or fetch.get("error") or fetch.get("mismatch_count"):
            raise RuntimeError(fetch.get("error") or "bucket index files are missing or the wrong size; mapping was not started")
        if job.get("cancel"): return stopped("before-mapping")
        job["message"] = "mapping buckets to writers"; job["progress"] = 0.7
        def mlog(m):
            job["message"] = str(m)
            mm = re.search(r"mapped (\d+)/(\d+)", str(m))
            if mm: job["progress"] = 0.7 + 0.3 * int(mm.group(1)) / max(1, int(mm.group(2)))
        res, upto = hbk_poolmap.build_poolmap(self.src, str(pool_dir), mlog, max_bucket=max_bucket)
        self._pool = None
        return {"mapped": len(res), "upto": upto}
    def restore_plan(self, items, skip_meta=True):
        """items: [{share, ver, path, is_dir}] -> list of file rows (dicts) to restore."""
        files = []
        def meta(path, name):
            return skip_meta and ("/@eaDir/" in f"/{path}" or name.endswith("@SynoResource") or name.endswith("@SynoEAStream") or name == ".DS_Store")
        for it in items:
            c = self.conn(it["share"], int(it["ver"]))
            if it.get("is_dir"):
                q = "select * from files where is_dir=0 and (mode & 61440)=32768 and (path like ? or ? = '') order by path"
                rows = c.execute(q, (it["path"].rstrip("/") + "/%", it["path"])).fetchall()
            else:
                rows = c.execute("select * from files where path = ? and is_dir=0", (it["path"],)).fetchall()
            for r in rows:
                if meta(r["path"], r["name"]): continue
                d = dict(r); d["share"] = it["share"]; d["ver"] = int(it["ver"]); files.append(d)
            c.close()
        return files
    def restore(self, items, dest, job, overwrite=False, skip_meta=True, start_at=0):
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        jf = JOBS_DIR / f"{job['id']}.json"
        def persist(state="running"):
            jf.write_text(json.dumps({"id": job["id"], "src": self.src.id, "items": items, "dest": dest, "overwrite": overwrite, "skip_meta": skip_meta,
                                      "next_index": job.get("next_index", 0), "done_files": job.get("done_files", 0), "skipped": job.get("skipped", 0),
                                      "errors": job.get("errors", [])[-200:], "total_files": job.get("total_files"), "total_bytes": job.get("total_bytes"),
                                      "done_bytes": job.get("done_bytes", 0), "state": state, "label": job["label"], "started": job["started"], "updated": time.time()}))
        remote_dest = ":" in dest and not dest.startswith("/")    # rclone remote, e.g. nas:GDriveRestore/restore
        if remote_dest:
            import tempfile
            stage = tempfile.mkdtemp(prefix="hbk-stage-")
            existing = set()
            job["message"] = "listing destination"
            r = subprocess.run([RCLONE, "lsjson", "-R", "--files-only", "--no-mimetype", dest], capture_output=True, text=True)
            if r.returncode == 0:
                for e in json.loads(r.stdout or "[]"): existing.add((e["Path"], e["Size"]))
        files = self.restore_plan(items, skip_meta)
        total_bytes = sum(f["size"] for f in files); done_bytes = 0
        job["total_files"] = len(files); job["total_bytes"] = total_bytes; job["done_files"] = 0; job["errors"] = []; job["skipped"] = 0
        pool = self.pool; fh_cache = {}
        # --- bucket prefetch (parallel downloads ahead of the extractor) + LRU eviction of downloaded bucket data
        import concurrent.futures, threading
        cache_limit = int(os.environ.get("HBK_CACHE_GB", "20")) * (1 << 30)
        downloaded = collections.OrderedDict(); dl_lock = threading.Lock(); dl_size = [0]
        def fetch(bk):
            rel = pool.bucket_name(bk, "bucket")
            already = self.src.locate(rel)
            path = self.src.ensure(rel)
            # Only files this job created in the app cache are eligible for eviction.
            if already is None and self.src.in_cache(path) and path.is_file():
                with dl_lock:
                    sz = path.stat().st_size
                    downloaded[bk] = (path, sz); dl_size[0] += sz
        def evict():
            with dl_lock:
                while dl_size[0] > cache_limit and len(downloaded) > 8:
                    bk, (path, sz) = downloaded.popitem(last=False)
                    fh = fh_cache.pop(bk, None)
                    if fh: fh.close()
                    try:
                        if self.src.in_cache(path):
                            path.unlink(); dl_size[0] -= sz
                    except OSError: pass
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=4); pending = {}
        def prefetch_from(i):
            seen = set()
            for f in files[i:i + 40]:
                if f["off"] is None or f["off"] < 0: continue
                try: locs, _ = pool.plan(f["off"])
                except Exception: continue
                for bk, _ in locs:
                    if bk in seen or bk in pending: continue
                    seen.add(bk)
                    if len(pending) < 12: pending[bk] = ex.submit(fetch, bk)
        job["next_index"] = start_at; persist()
        try:
            for i, f in enumerate(files):
                if i < start_at: done_bytes += f["size"]; continue
                if job.get("cancel"):
                    job["message"] = "paused"; persist("paused"); return {"paused": True, "next_index": i, "restored": job["done_files"], "skipped": job["skipped"], "errors": len(job["errors"]), "bytes": done_bytes}
                if i % 10 == 0:
                    for bk in [k for k, fut in pending.items() if fut.done()]: pending.pop(bk)
                    prefetch_from(i); evict()
                if f["off"] is not None and f["off"] >= 0:
                    try:
                        for bk, _ in pool.plan(f["off"])[0]:
                            fut = pending.get(bk)
                            if fut: fut.result()
                            with dl_lock:
                                if bk in downloaded: downloaded.move_to_end(bk)
                    except Exception: pass
                rel = f"{f['share']}/{f['path']}"; out = os.path.join(dest, rel)
                job["message"] = f"{i + 1}/{len(files)}: {rel}"; job["progress"] = done_bytes / total_bytes if total_bytes else (i / max(1, len(files)))
                job["next_index"] = i
                if i % 50 == 0: persist()
                for attempt in range(3):
                  try:
                    if remote_dest:
                        if not overwrite and (rel, f["size"]) in existing:
                            job["skipped"] += 1; job["done_files"] += 1; break
                        out = os.path.join(stage, rel); os.makedirs(os.path.dirname(out), exist_ok=True)
                        pool.extract(f["off"], out, size=f["size"], mtime=f["mtime"], job=None, fh_cache=fh_cache)
                        r = subprocess.run([RCLONE, "moveto", out, f"{dest}/{rel}", "--retries", "5", "--low-level-retries", "20", "--retries-sleep", "5s"], capture_output=True, text=True)
                        if r.returncode != 0: raise RuntimeError("upload failed: " + r.stderr.strip()[-200:])
                        job["done_files"] += 1; break
                    if not overwrite and os.path.isfile(out):
                        st = os.stat(out)
                        if st.st_size == f["size"] and int(st.st_mtime) == f["mtime"]:
                            job["skipped"] += 1; job["done_files"] += 1; break
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    pool.extract(f["off"], out, size=f["size"], mtime=f["mtime"], job=None, fh_cache=fh_cache)
                    job["done_files"] += 1; break
                  except Exception as e:
                    if attempt < 2 and not isinstance(e, KeyError):      # KeyError = chunk not in pool map: retrying will not help
                        job["message"] = f"{rel}: {e} — retrying"; time.sleep([2, 10][attempt]); continue
                    job["errors"].append({"path": rel, "error": str(e)})
                    if len(job["errors"]) > 2000: raise RuntimeError("too many errors, aborting")
                done_bytes += f["size"]; job["done_bytes"] = done_bytes
                if len(fh_cache) > 64:
                    for k in list(fh_cache)[:32]: fh_cache.pop(k).close()
        except Exception:
            persist("error"); raise
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
            for fh in fh_cache.values(): fh.close()
        job["progress"] = 1.0; job["next_index"] = len(files); persist("done")
        return {"files": len(files), "restored": job["done_files"], "skipped": job["skipped"], "errors": len(job["errors"]), "bytes": done_bytes}

    # --- content extraction (small files stored in Pool/file_pool/<n>.file)
    def extract(self, share, ver, fid, job=None):
        c = self.conn(share, ver)
        r = c.execute("select * from files where id = ?", (fid,)).fetchone(); c.close()
        if not r: raise FileNotFoundError("file not in index")
        off = r["off"]
        if r["is_dir"]: raise RuntimeError("directory")
        if off is None or off >= 0:
            import tempfile
            tmpdir = tempfile.mkdtemp(prefix="hbk-"); out = os.path.join(tmpdir, "x")
            try:
                self.pool.extract(off, out, size=r["size"], mtime=r["mtime"]); data = Path(out).read_bytes()
            finally:
                if os.path.exists(out): os.unlink(out)
                os.rmdir(tmpdir)
            return {"raw": data, "name": r["name"], "size": r["size"], "pool_file": "pool chunks"}
        n = -off
        ents = self.src.find("Pool/file_pool", f"{n}.file.*")
        if not ents: raise FileNotFoundError(f"Pool/file_pool/{n}.file.* not present in this source")
        name = sorted(ents, key=lambda x: rev_of(x, f"{n}.file")[0])[-1]
        if name.endswith(RANSOM_SUFFIX): raise RuntimeError("pool file is ransomware-encrypted")
        p = self.src.ensure(f"Pool/file_pool/{name}", job)
        data = p.read_bytes()
        return {"raw": data, "name": r["name"], "size": r["size"], "pool_file": name}

# ----------------------------------------------------------------------------- registry
SOURCES, BACKUPS = {}, {}
def load_sources():
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if CONF_FILE.is_file():
        for s in json.loads(CONF_FILE.read_text()):
            SOURCES[s["id"]] = Source(s["id"], s["name"], s["local"], s.get("remotes") or s.get("remote"), s.get("extra"))
def save_sources():
    CONF_FILE.write_text(json.dumps([{"id": s.id, "name": s.name, "local": str(s.local), "remotes": s.remotes, "extra": [str(x) for x in s.extra]} for s in SOURCES.values()], indent=1))
def add_source(name, local, remote, extra=None):
    sid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or uuid.uuid4().hex[:6]
    while sid in SOURCES: sid += "x"
    SOURCES[sid] = Source(sid, name, local, remote, extra); save_sources(); return SOURCES[sid]
def backup_for(sid, refresh=False):
    if sid not in SOURCES: raise KeyError(f"unknown source {sid}")
    if refresh or sid not in BACKUPS: BACKUPS[sid] = Backup(SOURCES[sid])
    return BACKUPS[sid]

# ----------------------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def _json(self, o, code=200): self._send(code, jdump(o))
    def do_GET(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                page = _bundled("static/index.html")
                if page is None: return self._send(500, b"UI not found", "text/plain; charset=utf-8")
                return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/sources":
                return self._json([s.to_json() for s in SOURCES.values()])
            if u.path == "/api/about":
                try:
                    r = subprocess.run([RCLONE, "version"], capture_output=True, text=True)
                    line = (r.stdout.splitlines() or ["not found"])[0] if r.returncode == 0 else "rclone not found"
                except OSError:
                    line = "rclone not found"
                return self._json({"name": APP_NAME, "tagline": TAGLINE, "notice": NOTICE, "version": VERSION, "rclone": line, "rclone_path": RCLONE, "config": str(CONF_DIR)})
            if u.path == "/api/quit":
                threading.Timer(0.3, _exit_app).start(); return self._json({"ok": True})
            if u.path == "/api/ls":   # directory picker helper
                p = Path(os.path.expanduser(q.get("path") or "~"))
                if not p.is_dir(): p = p.parent
                items = sorted([e.name for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")], key=str.lower)
                return self._json({"path": str(p), "parent": str(p.parent), "dirs": items})
            if u.path == "/api/remotes":
                r = subprocess.run([RCLONE, "listremotes"], capture_output=True, text=True)
                rems = [x.strip() for x in r.stdout.splitlines() if x.strip()]
                out = {"remotes": rems, "dirs": []}
                if q.get("remote"):
                    r2 = subprocess.run([RCLONE, "lsjson", "--no-mimetype", "--dirs-only", q["remote"] + (q.get("path") or "")], capture_output=True, text=True)
                    if r2.returncode == 0: out["dirs"] = [e["Name"] for e in json.loads(r2.stdout or "[]")]
                    else: out["error"] = r2.stderr.strip()[-200:]
                return self._json(out)
            if u.path == "/api/rclone/ls":      # browse a remote (or local path when remote == "")
                rem, path = q.get("remote", ""), q.get("path", "")
                if rem:
                    r = subprocess.run([RCLONE, "lsjson", "--no-mimetype", "--dirs-only", rem + path], capture_output=True, text=True)
                    if r.returncode != 0: return self._json({"error": r.stderr.strip()[-300:]})
                    dirs = sorted(e["Name"] for e in json.loads(r.stdout or "[]"))
                    r2 = subprocess.run([RCLONE, "lsjson", "--no-mimetype", "--files-only", "--max-depth", "1", rem + path], capture_output=True, text=True)
                    files = sorted(e["Name"] for e in json.loads(r2.stdout or "[]")) if r2.returncode == 0 else []
                else:
                    pp = Path(os.path.expanduser(path or "~"))
                    try: dirs = sorted(e.name for e in pp.iterdir() if e.is_dir() and not e.name.startswith(".")); files = sorted(e.name for e in pp.iterdir() if e.is_file())[:50]
                    except OSError as e: return self._json({"error": str(e)})
                    path = str(pp)
                looks_hbk = "Config" in dirs and "Pool" in dirs
                return self._json({"path": path, "dirs": dirs, "files": files[:50], "looks_like_hbk": looks_hbk, "hbk_dirs": [d for d in dirs if d.endswith(".hbk")]})
            if u.path == "/api/rclone/connect":  # start an interactive rclone config (opens the browser for Google)
                name = re.sub(r"[^A-Za-z0-9_-]", "", q.get("name") or "gdrive") or "gdrive"
                kind = q.get("kind", "drive")
                if kind == "drive":
                    args = [RCLONE, "config", "create", name, "drive", "scope=drive.readonly"]
                    if q.get("client_id"): args += [f"client_id={q['client_id']}", f"client_secret={q.get('client_secret', '')}"]
                elif kind == "smb":
                    obs = subprocess.run([RCLONE, "obscure", q.get("password", "")], capture_output=True, text=True).stdout.strip()
                    args = [RCLONE, "config", "create", name, "smb", f"host={q.get('host', '')}", f"user={q.get('user', '')}", f"pass={obs}", "--obscure=false", "--non-interactive"]
                else: return self._json({"error": "unknown kind"}, 400)
                def run(job):
                    job["message"] = "waiting for Google sign-in in your browser…" if kind == "drive" else "creating remote"
                    r = subprocess.run(args, capture_output=True, text=True, timeout=600)
                    if r.returncode != 0: raise RuntimeError(r.stderr.strip()[-400:] or "rclone config failed")
                    t = subprocess.run([RCLONE, "lsd", f"{name}:"], capture_output=True, text=True, timeout=60)
                    if t.returncode != 0: raise RuntimeError("remote created but listing failed: " + t.stderr.strip()[-300:])
                    return {"remote": name + ":"}
                return self._json(start_job(run, f"connect {kind}"))
            if u.path == "/api/jobs/saved":
                out = []
                for jf in sorted(JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
                    try: d = json.loads(jf.read_text())
                    except Exception: continue
                    if d["id"] in JOBS and JOBS[d["id"]]["state"] == "running": d["state"] = "running"
                    out.append({k: d.get(k) for k in ("id", "label", "state", "dest", "items", "next_index", "total_files", "done_files", "skipped", "total_bytes", "done_bytes", "started", "updated", "src")} | {"errors": len(d.get("errors") or [])})
                return self._json(out[:20])
            if u.path == "/api/job/cancel":
                j = JOBS.get(q.get("id"))
                if j: j["cancel"] = True
                return self._json({"ok": bool(j)})
            if u.path == "/api/jobs":
                return self._json([{k: v for k, v in j.items() if k in ("id", "label", "state", "progress", "message", "started", "done_files", "total_files", "done_bytes", "total_bytes")} for j in JOBS.values() if j["state"] == "running" or time.time() - j["started"] < 120])
            if u.path == "/api/job":
                j = JOBS.get(q.get("id")); return self._json(j or {"error": "no such job"})
            if u.path == "/api/open":
                b = backup_for(q["src"], refresh=q.get("refresh") == "1"); return self._json(b.info)
            b = backup_for(q["src"])
            if u.path == "/api/snapshot_info":
                return self._json(b.snapshot_info(q["share"], int(q["ver"])))
            if u.path == "/api/index":
                share, ver = q["share"], int(q["ver"])
                shares = b.shares if share == "*" else [share]
                def run(job):
                    out = []
                    for i, sh in enumerate(shares):
                        job["message"] = f"{sh}: preparing"; job["progress"] = i / len(shares)
                        try: out.append({"share": sh, "ok": True, "path": b.build_index(sh, ver, job)})
                        except Exception as e: out.append({"share": sh, "ok": False, "error": str(e)})
                    return out
                return self._json(start_job(run, f"index v{ver} {share}"))
            if u.path == "/api/query": return self._json(b.query(q.get("share", "*"), int(q["ver"]), q))
            if u.path == "/api/children": return self._json(b.children(q["share"], int(q["ver"]), q.get("dir", "")))
            if u.path == "/api/histogram": return self._json(b.histogram(q.get("share", "*"), int(q["ver"]), q))
            if u.path == "/api/file": return self._json(b.file_detail(q["share"], int(q["ver"]), q["id"]))
            if u.path == "/api/diff": return self._json(b.diff(q["share"], int(q["a"]), int(q["b"])))
            if u.path == "/api/pool": return self._json(b.pool_status())
            if u.path == "/api/pool/build":
                mb = int(q["max_bucket"]) if q.get("max_bucket") else None
                return self._json(start_job(lambda job: b.build_pool(job, mb), "build pool map"))
            if u.path == "/api/restore_plan":
                items = json.loads(q["items"]); files = b.restore_plan(items, q.get("skip_meta", "1") == "1")
                return self._json({"files": len(files), "bytes": sum(f["size"] for f in files), "sample": [f"{f['share']}/{f['path']}" for f in files[:20]]})
            if u.path == "/api/download":
                c = b.conn(q["share"], int(q["ver"])); r = c.execute("select * from files where id = ?", (q["id"],)).fetchone(); c.close()
                if not r: return self._json({"error": "no such file"}, 404)
                import tempfile
                tmpdir = tempfile.mkdtemp(prefix="hbk-"); out = os.path.join(tmpdir, r["name"])
                b.pool.extract(r["off"], out, size=r["size"], mtime=r["mtime"])
                data = Path(out).read_bytes(); os.unlink(out); os.rmdir(tmpdir)
                return self._send(200, data, "application/octet-stream", {"Content-Disposition": f'attachment; filename="{r["name"]}"'})
            if u.path == "/api/extract":
                res = b.extract(q["share"], int(q["ver"]), q["id"])
                raw = res["raw"]
                if q.get("mode") == "download":
                    return self._send(200, raw, "application/octet-stream", {"Content-Disposition": f'attachment; filename="{res["name"]}.pooled"'})
                import hashlib
                return self._json({"name": res["name"], "pool_file": res["pool_file"], "stored_bytes": len(raw), "declared_size": res["size"],
                                   "sha256_stored": hashlib.sha256(raw).hexdigest(), "head_hex": raw[:256].hex(),
                                   "head_text": raw[:2048].decode("utf-8", "replace")})
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e), "trace": traceback.format_exc()}, 500)
    def do_POST(self):
        u = urlparse(self.path); n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if u.path == "/api/sources":
                s = add_source(body["name"], body["local"], body.get("remote"), body.get("extra")); return self._json(s.to_json())
            if u.path == "/api/restore":
                b = backup_for(body["src"]); dest = os.path.expanduser(body["dest"].strip())
                if ":" in dest and not dest.startswith("/"):
                    r = subprocess.run([RCLONE, "mkdir", dest], capture_output=True, text=True)
                    if r.returncode != 0: return self._json({"error": f"cannot use remote destination {dest}: {r.stderr.strip()[-200:]}"}, 400)
                elif not os.path.isdir(dest): return self._json({"error": f"destination folder does not exist: {dest}"}, 400)
                return self._json(start_job(lambda job: b.restore(body["items"], dest, job, body.get("overwrite", False), body.get("skip_meta", True)), "restore"))
            if u.path == "/api/restore/resume":
                d = json.loads((JOBS_DIR / f"{body['id']}.json").read_text()); b = backup_for(d["src"])
                (JOBS_DIR / f"{body['id']}.json").unlink()
                job = start_job(lambda job: b.restore(d["items"], d["dest"], job, d.get("overwrite", False), d.get("skip_meta", True), start_at=d.get("next_index", 0)), "restore (resumed)")
                return self._json(job)
            if u.path == "/api/sources/delete":
                SOURCES.pop(body["id"], None); BACKUPS.pop(body["id"], None); save_sources(); return self._json({"ok": True})
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e), "trace": traceback.format_exc()}, 500)

def _log(msg):
    stream = sys.stdout if sys.stdout is not None else sys.stderr
    if stream is None: return
    try: print(msg, file=stream, flush=True)
    except Exception: pass
APP_NAME = "Callido Restore"                                   # the one place the product name lives (build_app.sh reads it)
TAGLINE = "Restore Synology Hyper Backup (.hbk) files"
NOTICE = "Not affiliated with or endorsed by Synology. Hyper Backup is a trademark of Synology Inc."
def _exit_app(code=0):
    """Stop the whole app: rclone children first (an `rclone lsjson` used to survive Quit), then the process. Restore jobs are
    persisted and can be resumed; the desktop window (if any) closes with the process."""
    if os.name != "nt": subprocess.run(["pkill", "-P", str(os.getpid())], capture_output=True)
    os._exit(code)
def _open_browser(port):
    try:
        import webbrowser; webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception: _log(f"could not open a browser; open http://127.0.0.1:{port}")
def _already_running(port):
    """True when an instance of the app already answers on this port (e.g. the app was opened twice)."""
    try:
        from urllib.request import urlopen
        with urlopen(f"http://127.0.0.1:{port}/api/about", timeout=2) as r: return b'"version"' in r.read()
    except Exception: return False
def run_window(port, smoke=False):
    """Show the UI in a native window (pywebview: WebKit on macOS, Edge WebView2 on Windows, GTK/Qt on Linux). Blocks until the
    window is closed. Must run on the main thread. With smoke=True the window closes itself once the page has loaded and the
    function returns the page title (None when it never loaded), so a build can be checked without a person."""
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True                   # the explorer's single-file download links
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    url = f"http://127.0.0.1:{port}"
    win = webview.create_window(APP_NAME, url, width=1360, height=880, min_size=(900, 600), text_select=True)
    result = {}
    def closing():                                               # warn before closing while a job (e.g. a restore) runs
        running = [j["label"] for j in JOBS.values() if j["state"] == "running"]
        if not running or smoke: return True
        return bool(win.create_confirmation_dialog(APP_NAME, f"{len(running)} job(s) still running: {running[0]}. "
                                                   "Quit anyway? Restore jobs are saved and can be resumed later."))
    win.events.closing += closing
    if smoke:
        guard = threading.Timer(30, lambda: (_log("smoke: window never loaded"), _exit_app(2)))
        def loaded():
            guard.cancel()
            try: result["title"] = win.evaluate_js("document.title")
            finally: _log(f"smoke: page loaded, title={result.get('title')!r}"); win.destroy()
        win.events.loaded += loaded
        guard.start()
    webview.start()
    return result.get("title")
def main():
    ap = argparse.ArgumentParser(description=f"{APP_NAME}: explore a Synology Hyper Backup and restore files from it")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", "--no-window", dest="no_window", action="store_true", help="server only (no window, no browser)")
    ap.add_argument("--browser", action="store_true", help="use the system browser instead of the app window")
    ap.add_argument("--smoke", action="store_true", help="open the window, wait for the page, close it and exit (build check)")
    a = ap.parse_args()
    if getattr(sys, "frozen", False) and not a.smoke:      # .app bundle: no terminal, so keep a log file next to the settings (smoke: stdout)
        try:
            CONF_DIR.mkdir(parents=True, exist_ok=True)
            sys.stdout = sys.stderr = open(CONF_DIR / "app.log", "a", buffering=1)
        except OSError as e: _log(f"warning: cannot write {CONF_DIR / 'app.log'} ({e}); logging to the console instead")
    _verify_frozen()
    load_sources()
    windowed = not a.no_window and not a.browser
    try: srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    except OSError as e:
        if _already_running(a.port):
            _log(f"already running on port {a.port}; showing the running instance")
            if windowed: run_window(a.port, smoke=a.smoke)
            elif not a.no_window: _open_browser(a.port)
            return
        raise SystemExit(f"cannot listen on 127.0.0.1:{a.port}: {e} (another program uses this port; try --port)")
    _log(f"{APP_NAME} {VERSION} on http://127.0.0.1:{a.port}  (config: {CONF_DIR})")
    if not windowed:
        if not a.no_window: threading.Timer(0.6, lambda: _open_browser(a.port)).start()
        try: srv.serve_forever()
        except KeyboardInterrupt: pass
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try: title = run_window(a.port, smoke=a.smoke)
    except Exception as e:
        if a.smoke: _log(f"smoke: window failed: {e}"); _exit_app(2)
        _log(f"no app window available ({e}); falling back to the browser")
        _open_browser(a.port)
        try: threading.Event().wait()                        # the server thread keeps serving until Ctrl-C / Quit
        except KeyboardInterrupt: pass
        return
    if a.smoke:
        ok = bool(title) and APP_NAME in title
        _log(f"smoke: {'ok' if ok else 'FAILED'} (title {title!r})")
        _exit_app(0 if ok else 2)
    _log("window closed; stopping")
    _exit_app(0)

if __name__ == "__main__": main()
