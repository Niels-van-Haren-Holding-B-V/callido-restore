"""``Backup``: one opened .hbk (discovery of task/shares/versions, derived SQLite snapshot index, queries, pool access, restore).
The restore methods are the engine behind the wizard; the query methods serve the explorer tabs."""
import collections, json, os, re, shutil, sqlite3, subprocess, tempfile, threading, time
from callido_restore.sources import transfer
from pathlib import Path
from callido_restore import paths
from callido_restore.core.hbkmeta import DOC_EXT
from callido_restore.core.hbkmeta import RANSOM_NOTE
from callido_restore.core.hbkmeta import RANSOM_SUFFIX
from callido_restore.core.hbkmeta import ROOT_PARENT
from callido_restore.core.hbkmeta import SCRIPT_EXT
from callido_restore.core.hbkmeta import is_sqlite
from callido_restore.core.hbkmeta import rev_of
from callido_restore.oscompat import popen_kw
from callido_restore.restore.jobs import log
from callido_restore.paths import RCLONE, ui_path
from callido_restore.restore.destination import file_target
from callido_restore.restore.destination import destination_sizes
from callido_restore.restore.destination import delete_own
from callido_restore.restore.destination import list_destination
from callido_restore.restore.destination import move_no_replace
from callido_restore.restore.destination import remote_stat
from callido_restore.restore.destination import place_file
from callido_restore.restore.destination import restore_roots
from callido_restore.sources.source import Source
from callido_restore.sources.source import is_remote_path

UNVERIFIED_MAX = 2000        # unverified files listed per job (like errors); the counts are complete

def mapper_phase(line):
    """(message for the user, job progress or None) for one line the pool mapper logs. The mapper's lines are for the log:
    counters, stats dicts, writer numbers. The user sees what is happening and how far it is: the scan is 70-95 % of the
    job, the placement 95-100 %."""
    s = line.strip()
    m = re.search(r"scanned ([\d,]+)/([\d,]+)", s)
    if m:
        i, n = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
        return f"reading the index ({i:,} of {n:,})", 0.7 + 0.25 * i / max(1, n)
    m = re.search(r"placing writer (\d): ([\d,]+)/([\d,]+)", s)
    if m:
        w, k, n = int(m.group(1)), int(m.group(2).replace(",", "")), int(m.group(3).replace(",", ""))
        return f"building the map (part {w + 1} of 4, {k:,} of {n:,})", 0.96 + 0.04 * (w + k / max(1, n)) / 4
    m = re.search(r"resolving copies: pass (\d)/3, ([\d,]+)/([\d,]+)", s)
    if m: return f"resolving duplicate files (pass {m.group(1)} of 3, {m.group(2)} of {m.group(3)})", 0.955
    if s.startswith("loading the record tables"): return "loading the record tables", 0.7
    if s.startswith("writer") and "records=" in s: return "loading the record tables", 0.7
    if s.startswith(("anchor scan (", "scanning the bucket indexes")): return "reading the index", 0.7
    if s.startswith(("anchor scan done", "placing records", "owners from anchors", "placement:", "phase2:")): return "building the map", 0.955
    m = re.search(r"mapped ([\d,]+)/([\d,]+) buckets \(anchor-first\)", s)
    if m: return "writing the map", 0.995
    m = re.search(r"mapped (\d+)/(\d+)", s)                       # the v1 search, kept for HBK_MAPPER=v1
    if m: return f"building the map ({m.group(1)} of {m.group(2)})", 0.7 + 0.3 * int(m.group(1)) / max(1, int(m.group(2)))
    if s.startswith("record meta:"): return "collecting file tags", None
    if "{" in s or "=" in s: return "building the map", None       # a stats line: not for the user
    return s, None


UPLOAD_STALL_S = 600          # an upload whose transferred bytes do not move for this long is ended and retried
UPLOAD_STALLED, UPLOAD_CANCELLED = -999, -998

UPLOAD_WORKERS = 8                                         # files uploaded to a remote target at once (each its own rclone process and SMB session)
STAGE_MAX = int(float(os.environ.get("HBK_STAGE_GB", "4")) * (1 << 30))   # extracted files waiting for their upload, bytes
_UNIT = {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50}
_LIVE = set(); _LIVE_LOCK = threading.Lock()           # running upload processes, ended by kill_uploads() when the app quits

CACHE_GB = 5                                      # downloaded backup data kept on this computer during a restore (HBK_CACHE_GB)
PART_SUFFIX = ".callido-restore.part"             # an upload's name on the target until it is complete
CHECK_ROUNDS = 2                                  # the final check restores missing files again this many times
CHECK_WAIT_S = 120                                # and first waits this long when an upload failed (the NAS had the file in use)
PARTIAL_RE = re.compile(r"^(.*)\.[0-9a-f]{8}\.partial$")   # rclone's temporary name while it uploads <name>

def local_size(path):
    """size of the file at path; None for a directory, a symlink or nothing"""
    try: return None if os.path.islink(path) or os.path.isdir(path) else os.lstat(path).st_size
    except OSError: return None

def differs_msg(have, want):
    if have is None: return "something that is not a file is at the target under this name; it was kept"
    return (f"a file of {have:,} bytes is at the target, the backup's is {want:,} bytes (perhaps the start of an interrupted copy); "
            "it was not written over: check it, delete it if it is incomplete, and restore again")

def _bytes(txt):
    """'12.3 MiB' / '512 B' / '1.5GiB' -> bytes (rclone's stats units)"""
    m = re.match(r"\s*([\d.]+)\s*([KMGTP]?)i?B", txt or "")
    return int(float(m.group(1)) * _UNIT[m.group(2)]) if m else 0

def kill_uploads():
    """End every running upload process (Quit: on Windows the app's children are not ended with it). Returns how many."""
    with _LIVE_LOCK: ps = list(_LIVE)
    for p in ps:
        try: p.kill()
        except Exception: pass
    return len(ps)

def upload_watched(argv, cancelled=None, stall_s=None, poll_s=10, progress=None):
    """Run an rclone upload; returns (returncode, stderr tail). rclone reports its progress every ``poll_s`` seconds; when the
    transferred bytes do not change for ``stall_s`` (a connection that stays open but carries nothing; seen over a VPN for two
    hours) the process is ended and UPLOAD_STALLED is returned so the caller retries. ``cancelled()`` true (Stop) ends it at
    once: UPLOAD_CANCELLED. ``progress(done_bytes, total_bytes, bytes_per_s)`` follows rclone's stats lines."""
    stall_s = stall_s or UPLOAD_STALL_S
    p = subprocess.Popen(argv + ["--stats", f"{poll_s}s", "--stats-one-line", "--stats-log-level", "NOTICE"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, text=True, **popen_kw())
    with _LIVE_LOCK: _LIVE.add(p)
    last = [time.time()]; seen = [None]; tail = []
    def read():
        for line in p.stderr:
            tail.append(line); del tail[:-20]
            m = re.search(r"([\d.]+ ?[KMGTP]?i?B) / ([\d.]+ ?[KMGTP]?i?B)(?:, \d+%)?(?:, ([\d.]+ ?[KMGTP]?i?B)/s)?", line)
            if m:
                if m.group(1) != seen[0]: seen[0] = m.group(1); last[0] = time.time()
                if progress:
                    try: progress(_bytes(m.group(1)), _bytes(m.group(2)), _bytes(m.group(3) or ""))
                    except Exception: pass
    t = threading.Thread(target=read, daemon=True); t.start()
    try:
        while p.poll() is None:
            time.sleep(min(poll_s, 1))
            if cancelled and cancelled(): p.kill(); p.wait(); t.join(2); return UPLOAD_CANCELLED, "".join(tail)
            if time.time() - last[0] > stall_s: p.kill(); p.wait(); t.join(2); return UPLOAD_STALLED, "".join(tail)
        t.join(2)
        return p.returncode, "".join(tail)
    finally:
        with _LIVE_LOCK: _LIVE.discard(p)

class Backup:
    def __init__(self, src: Source, log=None):
        """log(message) is called at every step of the scan, so a job can show progress (the first open of a remote backup
        lists folders and fetches small files over rclone; each step can take seconds)."""
        self.src = src
        self.info = {"source": src.to_json(), "ransomware": {"locked_files": 0, "notes": [], "examples": []},
                     "task": {}, "shares": [], "versions": [], "readable": False, "problems": []}
        self.versions = []
        self._snap_cache = {}
        self.scan(log)

    # --- discovery
    def scan(self, log=None):
        s = self.src; info = self.info; log = log or (lambda m: None)
        log("Listing the backup folder…")
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
        for i, sh in enumerate(shares):
            log(f"Listing shares {i + 1}/{len(shares)}…"); ents = s.listdir(f"Config/@Share/{sh}"); tally(ents)
            ll = getattr(s, "last_listing", None)
            if ll and ll.get("rel", "").endswith(sh): log(f"Listing shares {i + 1}/{len(shares)} · {len(ents):,} entries in {ll['seconds']:.0f} s")
        info["ransomware"] = {"locked_files": locked, "notes": notes, "examples": ex}
        info["layout"] = {"top": sorted(top), "config": sorted(cfg), "pool_present": "Pool" in top, "control_present": "Control" in top}
        # task config
        if "_Syno_TaskConfig" in top:
            log("Reading the task info…")
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
        log("Reading the version list…")
        vi = s.latest("Config", "version_info.db")
        if not vi: info["problems"].append(f"This folder is not a Hyper Backup root (no Config/version_info.db in {s.remote or s.local}). Expected something like <folder>/<name>.hbk: remove this backup and add it again, picking the folder whose name ends in .hbk."); 
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
            log("Reading the backup statistics…")
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
        for i, sh in enumerate(shares):
            log(f"Reading the snapshot list of share {i + 1}/{len(shares)}…")
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
        job["message"] = "reading the file list"; job["progress"] = 0.3
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
        job["message"] = "resolving folder paths"; job["progress"] = 0.5
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
        job["message"] = "creating the index"; job["progress"] = 0.92
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
        from callido_restore.core import pool
        mp = self.src.index_dir / "pool" / "poolmap.json"
        mt = mp.stat().st_mtime if mp.is_file() else 0
        if getattr(self, "_pool", None) is None or getattr(self, "_pool_mtime", None) != mt:
            self._pool = pool.PoolReader(self.src, str(self.src.index_dir / "pool")); self._pool_mtime = mt
        return self._pool
    def pool_status(self):
        st = self.pool.status(); st["lz4"] = True
        try: import lz4  # noqa
        except Exception: st["lz4"] = False
        # Ready for restores: the backup carries its own chunk table (Pool/chunk_index locates every chunk exactly, no map is
        # needed), or a preparation of this mapper version finished (the inferred map, for backups without the table).
        st["complete"] = bool(st.get("chunk_index")) or self.pool_prepared()
        st["paused"] = self.pool_paused()
        return st
    # --- preparation state the UI and the auto-start read (persisted next to the pool files): finished by which mapper version,
    # paused by the user. A leftover map from a stopped run, a test run or an older mapper does not count as finished.
    def _prepared_file(self): return self.src.index_dir / "pool" / "prepared.json"
    def pool_prepared(self):
        from callido_restore.pool import poolmap
        try: return json.loads(self._prepared_file().read_text()).get("mapper") == poolmap.MAPPER_VERSION
        except (OSError, ValueError): return False
    def mark_prepared(self, mapped, total):
        from callido_restore.pool import poolmap
        p = self._prepared_file(); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"mapper": poolmap.MAPPER_VERSION, "finished": time.time(), "mapped": mapped, "total": total}))
    def _pause_file(self): return self.src.index_dir / "pool" / "paused"
    def pool_paused(self): return self._pause_file().is_file()
    def set_pool_paused(self, on):
        p = self._pause_file()
        if on: p.parent.mkdir(parents=True, exist_ok=True); p.write_text("paused by the user\n")
        elif p.exists(): p.unlink()
    def raw_snapshots(self):
        """Local raw <ver>.db snapshot files (one per share whose file list was loaded): the record tags come from these."""
        out = []
        for sh in self.shares:
            for n, e in self.src.listdir(f"Config/@Share/{sh}").items():
                if e["local"] and re.match(r"^\d+\.db\.\d+$", n): out.append(str(self.src.ensure(f"Config/@Share/{sh}/{n}")))
        return out
    def build_pool(self, job, max_bucket=None):
        from callido_restore.pool import poolmap, indexfetch
        from callido_restore.core import pool
        pool_dir = self.src.index_dir / "pool"; pool_dir.mkdir(parents=True, exist_ok=True)
        if self.pool.ci.available():
            # Pool/chunk_index answers "where is chunk g" for every chunk; a restore fetches only the table slices and the
            # bucket indexes it touches. Nothing to prepare (the inferred map is the fallback for backups without the table).
            job["message"] = "nothing to prepare: the backup has its own chunk table"; job["progress"] = 1.0
            return {"chunk_index": True, "mapped": 0, "upto": 0}
        try: self._prepared_file().unlink()                        # a run in progress is not a finished one
        except OSError: pass
        log = lambda m: job.__setitem__("message", str(m))
        def stopped(stage):
            job["message"] = f"stopped during {stage} — run Build again to continue"
            return {"stopped": True, "stage": stage}
        if job.get("cancel"): return stopped("start")
        listing = pool_dir / "listing.json"
        if not listing.is_file():
            job["message"] = "listing the backup folders"; out = []
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
            job["message"] = "reading the file records (downloads the 4 record sets if needed)"; job["progress"] = 0.1
            def report(message, done, total):                    # bytes of the set's slices fetched so far: the UI shows speed
                job["message"] = message; job["done_bytes"] = done; job["total_bytes"] = total
            pool.build_runs(self.src, str(pool_dir), log, cancelled=lambda: bool(job.get("cancel")),
                            progress=lambda f: job.__setitem__("progress", 0.1 + 0.2 * f), report=report)   # this phase spans 10-30 % of the job
        if job.get("cancel"): return stopped("chunk-records")
        job["message"] = "collecting file sizes"; job["progress"] = 0.3
        dbs = [(str(p), "index") for p in self.src.index_dir.glob("*__*.sqlite")] + [(p, "raw") for p in self.raw_snapshots()]
        pool.build_recsizes(self.src, dbs, str(pool_dir), log, cancelled=lambda: bool(job.get("cancel")))
        if job.get("cancel"): return stopped("record-sizes")
        # The anchor-first mapper (T1) identifies records by the file tags of the raw snapshots: recmeta_set<s>.json.
        job["message"] = "collecting file tags"; job["progress"] = 0.32
        raw = [p for p, kind in dbs if kind == "raw"]
        if pool.build_recmeta(self.src, raw, str(pool_dir), log, cancelled=lambda: bool(job.get("cancel"))) is None: return stopped("record-tags")
        entries = json.loads(listing.read_text())
        # New index bytes go to the app cache. Files already in the source or a mirror are only read.
        fetch = indexfetch.fetch_bucket_indexes(
            self.src.cache_dir, self.src.remotes, entries, job, readonly_roots=self.src.read_roots())
        self.src._list.clear()
        if fetch.get("stopped") or job.get("cancel"):
            fetch["stopped"] = True
            return fetch
        if fetch.get("failed") or fetch.get("error") or fetch.get("mismatch_count"):
            raise RuntimeError(fetch.get("error") or "bucket index files are missing or the wrong size; mapping was not started")
        if job.get("cancel"): return stopped("before-mapping")
        # The anchor-first mapper anchors records by the tags that only raw <ver>.db snapshots carry (recmeta_set<s>.json,
        # built above from the snapshots on this Mac): without any, the map would silently have no anchors at all.
        if not [p for p, kind in dbs if kind == "raw"]:
            raise RuntimeError("no snapshot database is on this computer yet: load the file list of at least one share for the chosen run (the chips above the tabs), then run the preparation again")
        job["message"] = "building the map"; job["progress"] = 0.7
        def mlog(m):
            # The mapper's lines carry counters and stats for the log; the job shows a plain phrase (mapper_phase) and takes
            # its progress from the counters. The raw line stays on the job as "detail" (not shown, there for support).
            job["detail"] = str(m)
            text, frac = mapper_phase(str(m))
            job["message"] = text
            if frac is not None: job["progress"] = frac
        # In a child process: the app stays responsive while the search runs at full CPU, and Stop ends it at once.
        res, upto = poolmap.build_poolmap_detached(self.src, str(pool_dir), mlog, max_bucket=max_bucket, cancelled=lambda: bool(job.get("cancel")))
        self._pool = None
        if job.get("cancel"): return stopped("mapping")
        if max_bucket is None: self.mark_prepared(len(res), self.pool_status().get("total_buckets", 0))   # finished: the auto-start leaves it alone (a max_bucket test run is not a finished preparation)
        return {"mapped": len(res), "upto": upto}
    def restore_plan(self, items, skip_meta=True):
        """items: [{share, ver, path, is_dir}] -> list of file rows (dicts) to restore."""
        files = []
        def meta(path, name):
            return skip_meta and ("/@eaDir/" in f"/{path}" or name.endswith("@SynoResource") or name.endswith("@SynoEAStream") or name == ".DS_Store")
        for idx, it in enumerate(items):
            c = self.conn(it["share"], int(it["ver"]))
            if it.get("is_dir"):
                prefix = it["path"].rstrip("/") + "/"          # substr is exact and case-sensitive; LIKE would match Foto_2024 -> FotoX2024, Photos -> photos
                q = "select * from files where is_dir=0 and (mode & 61440)=32768 and (? = '' or substr(path, 1, ?) = ?) order by path"
                rows = c.execute(q, (it["path"], len(prefix), prefix)).fetchall()
            else:
                rows = c.execute("select * from files where path = ? and is_dir=0", (it["path"],)).fetchall()
            for r in rows:
                if meta(r["path"], r["name"]): continue
                d = dict(r); d["share"] = it["share"]; d["ver"] = int(it["ver"]); d["item"] = idx; files.append(d)
            c.close()
        return files

    def restore_count(self, items, skip_meta=True):
        """Files and bytes the selection would restore, counted in SQL (no row materialisation; H13 shows it live)."""
        files = bytes_ = 0
        # instr/substr are exact and case-sensitive, unlike LIKE (case-insensitive for ASCII, with _ and % as wildcards)
        meta = " and instr('/' || path, '/@eaDir/') = 0 and substr(name, -13) != '@SynoResource' and substr(name, -13) != '@SynoEAStream' and name != '.DS_Store'" if skip_meta else ""
        for it in items:
            c = self.conn(it["share"], int(it["ver"]))
            if it.get("is_dir"):
                prefix = it["path"].rstrip("/") + "/"
                q = f"select count(*), coalesce(sum(size), 0) from files where is_dir=0 and (mode & 61440)=32768 and (? = '' or substr(path, 1, ?) = ?){meta}"
                n, b = c.execute(q, (it["path"], len(prefix), prefix)).fetchone()
            else:
                n, b = c.execute(f"select count(*), coalesce(sum(size), 0) from files where path = ? and is_dir=0{meta}", (it["path"],)).fetchone()
            files += n; bytes_ += b; c.close()
        return {"files": files, "bytes": bytes_}

    def plan_mappings(self, items, dest, skip_meta=True):
        """Per shared folder: where its selected files go, with one sample file (the first) so the user can check the
        final path (H8). Rows follow the order in which the shares first appear in items."""
        files = self.restore_plan(items, skip_meta); out = []; rows = {}
        for idx, it in enumerate(items):
            key = it["share"]
            if key not in rows:
                rows[key] = {"share": key, "dest": (it.get("dest") or dest or "").rstrip("/"), "paths": [], "files": 0, "bytes": 0, "sample_from": None, "sample_to": None}
                out.append(rows[key])
            m = rows[key]; m["paths"].append(it["path"])
            mine = [f for f in files if f.get("item") == idx]
            m["files"] += len(mine); m["bytes"] += sum(f["size"] for f in mine)
            if mine and m["sample_from"] is None:
                root, rel = file_target(mine[0], items, dest)
                m["sample_from"] = f"{mine[0]['share']}/{mine[0]['path']}"; m["sample_to"] = (f"{root}/{rel}" if is_remote_path(root) else ui_path(f"{root}/{rel}")) if root else rel   # Windows: one separator
        return {"files": len(files), "bytes": sum(f["size"] for f in files), "mappings": out, "sample": [f"{f['share']}/{f['path']}" for f in files[:20]]}
    def restore(self, items, dest, job, skip_meta=True, start_at=0):
        """Restore the selected items. An item with its own 'dest' gets its contents there (H8: one target per folder);
        the others go under dest as <share>/<path>. A file already at a destination is kept, whatever its date: with the
        backup's size it is skipped, with another size it is reported as an error. Nothing at a destination is ever written over
        or deleted (T8); the only deletions are the app's own temporary upload names (<name>.callido-restore.part). A final
        check lists the targets and restores missing files again."""
        paths.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        jf = paths.JOBS_DIR / f"{job['id']}.json"
        def persist(state="running"):
            jf.write_text(json.dumps({"id": job["id"], "src": self.src.id, "items": items, "dest": dest, "roots": roots, "skip_meta": skip_meta,
                                      "next_index": job.get("next_index", 0), "done_files": job.get("done_files", 0), "skipped": job.get("skipped", 0),
                                      "errors": job.get("errors", [])[-200:], "errors_total": job.get("errors_total", len(job.get("errors", []))), "total_files": job.get("total_files"), "total_bytes": job.get("total_bytes"),
                                      "verified": job.get("verified", 0), "identical_copy": job.get("identical_copy", 0), "unverified": job.get("unverified", [])[:UNVERIFIED_MAX], "unverified_total": job.get("unverified_total", len(job.get("unverified", []))),
                                      "done_bytes": job.get("done_bytes", 0), "check": job.get("check"), "state": state,
                                      "done_ahead": sorted(k for k in (finished | done_ahead) if k >= job.get("next_index", 0)), "label": job["label"], "started": job["started"], "updated": time.time()}))
        roots = restore_roots(items, dest)                       # every folder written into, local paths or rclone remotes
        existing = {}                                            # remote root -> {path: size} already there (never written over)
        for root in roots:
            if not is_remote_path(root): continue
            job["message"] = f"listing destination {root}"
            try: existing[root] = destination_sizes(root)
            except Exception:
                persist("error"); raise
        files = self.restore_plan(items, skip_meta)
        total_bytes = sum(f["size"] for f in files); done_bytes = 0
        job["total_files"] = len(files); job["total_bytes"] = total_bytes
        # A resumed job (start_at > 0) keeps the counts and the error list seeded from its saved file; a fresh one starts at zero.
        for k, zero in (("done_files", 0), ("errors", []), ("skipped", 0), ("verified", 0), ("identical_copy", 0), ("unverified", [])):
            if not start_at or k not in job: job[k] = zero if not isinstance(zero, list) else list(zero)
        if not start_at or "errors_total" not in job: job["errors_total"] = len(job["errors"])        # a seed without the counter: the list is all we know
        if not start_at or "unverified_total" not in job: job["unverified_total"] = len(job["unverified"])
        lock = threading.RLock()                                # counters and lists below are shared with the upload threads
        tx = {}; job["transfers"] = []                          # every running download / upload: what the progress bars show
        def publish_tx():
            # rates: bytes/s now, summed per direction: "download" from the backup source, "upload" to a NAS target. The bar's own
            # speed is files handled (skipped ones too), so it can be far above what reaches the NAS.
            job["transfers"] = list(tx.values())
            job["rates"] = {k: sum(t["rate"] for t in tx.values() if t["kind"] == k) for k in ("download", "upload")}
        def tx_set(key, kind, name, done, total, rate):
            with lock:
                tx[key] = {"kind": kind, "name": name, "done": int(done or 0), "total": int(total or 0), "rate": int(rate or 0)}
                publish_tx()
        def tx_done(key):
            with lock:
                if tx.pop(key, None) is not None: publish_tx()
        def add_error(f, msg):
            with lock:
                job["errors"].append({"path": f"{f['share']}/{f['path']}", "error": str(msg)}); job["errors_total"] += 1   # the list is capped in the saved file, the total is not
                if job["errors_total"] <= 100 or job["errors_total"] % 100 == 0:          # app.log: the first 100, then every 100th
                    log(f"restore error {job['errors_total']}: {f['share']}/{f['path']}: {msg}")
                if job["errors_total"] > 2000: fatal.append("too many errors, aborting")
        fatal = []
        # verified / identical_copy / unverified: per restored file, how its placement was established
        pool = self.pool; fh_cache = {}; counted = set()
        def restored(f):
            """count the file as restored by how the map established its placement (PoolReader.verification): "verified" = the
            placement of every record the file reads from matched the file sizes when the map was built and each chunk's md5 was
            checked on read (not a file-level checksum); "identical_copy" = restored from an identical record's entries;
            "unverified" = listed with the reason"""
            with lock:
                if id(f) in counted: return                    # restored again by the final check: counted once
                counted.add(id(f)); job["done_files"] += 1
            st, why = ("verified", "") if f["off"] is not None and f["off"] < 0 else ("unverified", "the pool map gives no status")
            if f["off"] is not None and f["off"] >= 0 and hasattr(pool, "verification"):
                try: st, why = pool.verification(f["off"])
                except Exception as e: st, why = "unverified", f"status check failed: {e}"
            with lock:
                if st == "verified": job["verified"] += 1
                elif st == "identical_copy": job["identical_copy"] += 1
                else:
                    job["unverified_total"] += 1                                          # counted always; listed up to the cap
                    if len(job["unverified"]) < UNVERIFIED_MAX: job["unverified"].append({"path": f"{f['share']}/{f['path']}", "reason": why})
        def already_there(f, have):
            """a file is at the target under this name (kept as is, whatever its date): with the backup's size it counts as
            skipped; with another size, or not a file, it is an error (perhaps the start of an interrupted copy). Never written over (T8)."""
            if have == f["size"]:
                with lock: job["skipped"] += 1; job["done_files"] += 1
            else: add_error(f, differs_msg(have, f["size"]))
        def result(**extra):
            if job.get("check"): extra = {**extra, "check": job["check"]}
            return {**extra, "restored": job["done_files"] - job["skipped"], "skipped": job["skipped"], "errors": job["errors_total"], "verified": job["verified"],
                    "identical_copy": job["identical_copy"], "unverified": job["unverified_total"]}
        # --- bucket prefetch (parallel downloads ahead of the extractor) + eviction of downloaded bucket data. The cache is a
        # passage, not a store: a bucket no file in the look-ahead window needs is deleted as soon as the cache holds more than
        # cache_limit, oldest first; buckets left in the app cache by earlier runs count too; a finished restore deletes them all.
        # Only bucket files in the app cache are ever deleted, never anything in a source folder.
        import concurrent.futures
        cache_limit = int(float(os.environ.get("HBK_CACHE_GB", str(CACHE_GB))) * (1 << 30))
        downloaded = collections.OrderedDict(); dl_lock = threading.Lock(); dl_size = [0]; upcoming = set()
        cdir = getattr(self.src, "cache_dir", None)
        for bf in sorted(Path(cdir, "Pool").glob("*/*/*.bucket.*") if isinstance(cdir, Path) else [], key=lambda q: q.stat().st_mtime if q.exists() else 0):
            m = re.fullmatch(r"(\d+)\.bucket\.\d+", bf.name)
            if m and bf.parent.name.isdigit() and bf.is_file() and not bf.is_symlink():
                bk = int(bf.parent.name) * 2048 + int(m.group(1))
                if bk not in downloaded: sz = bf.stat().st_size; downloaded[bk] = (bf, sz); dl_size[0] += sz
        def fetch(bk):
            rel = pool.bucket_name(bk, "bucket")
            already = self.src.locate(rel)
            key = ("dl", bk); total = 0
            if already is None:
                try: total = (self.src.listdir(os.path.dirname(rel)).get(os.path.basename(rel)) or {}).get("size") or 0
                except Exception: total = 0
            try: path = self.src.ensure(rel, progress=lambda done, speed, note="": tx_set(key, "download", os.path.basename(rel), done, total, speed))
            finally: tx_done(key)
            # Only files in the app cache are eligible for eviction (also ones an earlier run downloaded), never a source folder.
            if self.src.in_cache(path) and path.is_file():
                with dl_lock:
                    if bk not in downloaded:
                        sz = path.stat().st_size; downloaded[bk] = (path, sz); dl_size[0] += sz
        def evict(limit=None):
            """delete cached buckets, oldest first, until the cache holds at most limit bytes; buckets the next files need or
            that are being downloaded stay (limit 0 at the end of a restore: everything goes)"""
            limit = cache_limit if limit is None else limit
            with dl_lock:
                for bk in list(downloaded):
                    if dl_size[0] <= limit: break
                    if limit and (bk in upcoming or bk in pending): continue
                    path, sz = downloaded.pop(bk)
                    fh = fh_cache.pop(bk, None)
                    if fh: fh.close()
                    try:
                        if self.src.in_cache(path): path.unlink()
                    except OSError: pass
                    dl_size[0] -= sz
            job["cache_bytes"] = dl_size[0]
        prof = transfer.profile("restore")                     # sources/transfer.py: 8 bucket files in flight, 16 queued ahead
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=prof.workers); pending = {}
        def prefetch_from(i):
            seen = set()
            for f in files[i:i + 40]:
                if f["off"] is None or f["off"] < 0: continue
                try: locs, _ = pool.plan(f["off"])
                except Exception: continue
                for bk, _ in locs:
                    if bk in seen: continue
                    seen.add(bk)
                    if bk not in pending and len(pending) < prof.prefetch: pending[bk] = ex.submit(fetch, bk)
            with dl_lock: upcoming.clear(); upcoming.update(seen)          # what eviction keeps
        # Uploads to a remote target run UPLOAD_WORKERS at a time (each rclone process opens its own SMB session; one file at a
        # time spent most of its time on that set-up). Reading the backup stays in this thread (the reader's file handles are
        # not shared). The saved position is the oldest file whose upload has not finished, so a resume never skips one.
        up_ex = concurrent.futures.ThreadPoolExecutor(max_workers=UPLOAD_WORKERS); inflight = {}; up_cancelled = set(); staged = {}
        job.pop("cancel_uploads", None)
        finished = set()                                        # plan indexes whose upload ended (restored, skipped or error)
        done_ahead = set(job.get("done_ahead") or [])          # a resumed job: files after next_index that finished before the Stop
        stopping = lambda: bool(job.get("cancel") or job.get("cancel_uploads"))   # Stop, or the restore ends (an abort does not wait for 8 uploads)
        bytes_done = [done_bytes]
        def upload_task(i, f, out, root, rel):
            """upload one staged file. It goes to <rel>.callido-restore.part on the target first and is renamed to its name only
            when complete (on the target, never over a file there): a killed upload (Stop, stall, Quit, a dropped connection)
            leaves only that temporary name, never a cut-off file under the real name (some rclone backends write in place)."""
            key = ("up", i); name = f"{f['share']}/{f['path']}"; part = f"{root}/{rel}{PART_SUFFIX}"
            try:
                for attempt in range(3):
                    if stopping(): return "cancelled"
                    rc, err = upload_watched([RCLONE, "moveto", out, part, "--retries", "5", "--low-level-retries", "20",
                                              "--retries-sleep", "5s"], cancelled=stopping,
                                             progress=lambda d, t, r: tx_set(key, "upload", name, d, t or f["size"], r))
                    if rc == UPLOAD_CANCELLED: return "cancelled"               # Stop: this file is uploaded again on resume
                    if rc == 0: break
                    if rc == UPLOAD_STALLED: err = f"upload stalled: no progress for {UPLOAD_STALL_S // 60} min (network or NAS not answering)"
                    else: err = "upload failed: " + err.strip()[-200:]
                    if attempt < 2:
                        job["message"] = f"{name}: {err} — retrying"; time.sleep([2, 10][attempt]); continue
                    add_error(f, err); return "done"
                for attempt in range(3):                                       # the complete file is at the temporary name now
                    try: moved = move_no_replace(part, f"{root}/{rel}"); break
                    except Exception as e:
                        if attempt < 2: time.sleep([2, 10][attempt]); continue
                        add_error(f, f"upload failed: the file is on the target as {rel}{PART_SUFFIX} but could not be renamed: {e}"); return "done"
                if moved: restored(f); finished.add(i); return "done"
                try: delete_own(part)                                          # a file with this name appeared meanwhile: ours goes
                except Exception: pass
                already_there(f, (remote_stat(f"{root}/{rel}") or {}).get("Size")); finished.add(i); return "done"
            except Exception as e:
                add_error(f, e); return "done"
            finally:
                tx_done(key)
                with lock: bytes_done[0] += f["size"]; job["done_bytes"] = bytes_done[0]
                if os.path.exists(out):
                    try: os.unlink(out)
                    except OSError: pass
        def reap(block=False):
            """collect finished uploads; with block, wait for at least one"""
            if block and inflight: concurrent.futures.wait(list(inflight.values()), return_when=concurrent.futures.FIRST_COMPLETED)
            for k in [k for k, fut in inflight.items() if fut.done()]:
                fut = inflight.pop(k); staged.pop(k, None)
                try:
                    if fut.result() == "cancelled": up_cancelled.add(k)
                except Exception: pass
        def drain():
            while inflight: reap(block=True)
        def resume_point(i):
            """the oldest file not finished: in flight or cancelled, else the current one"""
            return min(list(inflight) + list(up_cancelled) + [i])
        chk_retried = [0]
        def check_targets():
            """compare the plan with what is at the targets: {"ok", "missing": [(i, f)], "differs": [(f, size)], "partial": n},
            or {"error"} when a target cannot be listed"""
            sizes = {}
            for root in roots:
                if not is_remote_path(root): continue
                try: sizes[root] = destination_sizes(root)
                except Exception as e: return {"error": str(e)}
            ok = 0; missing = []; differs = []; planned = set(); partial = 0
            for i, f in enumerate(files):
                root, rel = file_target(f, items, dest); planned.add((root, rel))
                if is_remote_path(root): have = sizes[root].get(rel)
                else:
                    try: have = os.lstat(os.path.join(root, rel)).st_size
                    except OSError: have = None
                if have is None: missing.append((i, f))
                elif have != f["size"]: differs.append((f, have))
                else: ok += 1
            for root, listing in sizes.items():
                for p in listing:
                    if p.endswith(PART_SUFFIX) and (root, p[:-len(PART_SUFFIX)]) in planned:
                        try: delete_own(f"{root}/{p}")            # this app's temporary name of an interrupted upload: ours, removed
                        except Exception: pass
                        continue
                    m = PARTIAL_RE.match(p)                     # rclone's own leftovers (<name>.<hash>.partial): counted, not touched
                    if m and (root, m.group(1).removesuffix(PART_SUFFIX)) in planned: partial += 1
            return {"ok": ok, "missing": missing, "differs": differs, "partial": partial}
        def finish_check(chk):
            """the check result in the job; the error list keeps one entry per file that is still wrong"""
            if chk.get("error"):
                job["check"] = {"error": chk["error"]}; log(f"restore check failed: {chk['error']}"); return
            # a file at the target with another size than the backup's is an error (kept, never written over)
            wrong = {f"{f['share']}/{f['path']}" for _, f in chk["missing"]} | {f"{f['share']}/{f['path']}" for f, _ in chk["differs"]}
            with lock:
                last = {}
                for e in job["errors"]:
                    if e["path"] in wrong: last[e["path"]] = e                    # the latest error per file that is still wrong
                errs = list(last.values())
                for _, f in chk["missing"]:
                    pth = f"{f['share']}/{f['path']}"
                    if pth not in last: errs.append({"path": pth, "error": "not at the target after the restore (found by the final check)"})
                for f, have in chk["differs"]:
                    pth = f"{f['share']}/{f['path']}"
                    if pth not in last: errs.append({"path": pth, "error": differs_msg(have, f["size"])})
                job["errors_total"] = len(errs); job["errors"] = errs                 # the check counts every file: exact, also beyond the saved list
                job["check"] = {"ok": chk["ok"], "missing": len(chk["missing"]), "differs": len(chk["differs"]), "partial": chk["partial"],
                                "restored_again": chk_retried[0], "files": len(files)}
            log(f"restore check: {job['check']}")
        # Every file is extracted into a staging folder and then moved into place. Each local root gets its own folder inside
        # that root (same filesystem, so place_file can hard-link atomically); it is new (mkdtemp) and removed in the finally
        # below, nothing else under the root is touched. If the app is killed mid-restore the folder stays behind; it carries the
        # job id, so a resume of the same job removes its own leftover in every root and nothing else. Remote roots share one
        # temporary folder on this machine (rclone moveto uploads from it).
        own = f".hbk-restore-{job['id']}-"; stage = {}; remote_stage = None
        for root in roots:
            if is_remote_path(root):
                if remote_stage is None: remote_stage = tempfile.mkdtemp(prefix="hbk-stage-")
                stage[root] = os.path.join(remote_stage, str(roots.index(root)))
            else:
                for left in Path(root).glob(own + "*"):
                    if left.is_dir() and not left.is_symlink(): shutil.rmtree(left, ignore_errors=True)
                stage[root] = tempfile.mkdtemp(prefix=own, dir=root)
        def process(i, f, again=None):
            """restore one file of the plan (index i): extract it and place it, or hand it to the upload threads. again =
            (k, n): the k-th of n files the final check found missing; the saved position then stays at the end of the plan
            (a Stop during the check resumes with the check)"""
            if f["off"] is not None and f["off"] >= 0:
                try:
                    for bk, _ in pool.plan(f["off"])[0]:
                        fut = pending.get(bk)
                        if fut: fut.result()
                        with dl_lock:
                            if bk in downloaded: downloaded.move_to_end(bk)
                except Exception: pass
            root, rel = file_target(f, items, dest); remote_dest = is_remote_path(root); out = os.path.join(root, rel)
            if again:
                job["message"] = f"restoring again {again[0] + 1}/{again[1]}: {f['share']}/{f['path']}"; job["progress"] = again[0] / again[1]
            else:
                job["message"] = f"{i + 1}/{len(files)}: {f['share']}/{f['path']}"; job["progress"] = bytes_done[0] / total_bytes if total_bytes else (i / max(1, len(files)))
                job["next_index"] = resume_point(i)
                if i % 50 == 0: persist()
            if remote_dest and rel not in existing[root]:
                # room in the staging folder first: at most UPLOAD_WORKERS * 2 files and STAGE_MAX bytes wait for their upload
                # (outside the bucket cache limit); one file always goes, however large
                while inflight and (len(inflight) >= UPLOAD_WORKERS * 2 or sum(staged.values()) + f["size"] > STAGE_MAX): reap(block=True)
                out = os.path.join(stage[root], rel); os.makedirs(os.path.dirname(out), exist_ok=True); ok = False
                for attempt in range(3):
                    try: pool.extract(f["off"], out, size=f["size"], mtime=f["mtime"], job=None, fh_cache=fh_cache); ok = True; break
                    except Exception as e:
                        if attempt < 2 and not isinstance(e, KeyError):  # KeyError = chunk not in pool map: retrying will not help
                            job["message"] = f"{f['share']}/{f['path']}: {e} — retrying"; time.sleep([2, 10][attempt]); continue
                        add_error(f, e); break
                if ok:
                    staged[i] = f["size"]; inflight[i] = up_ex.submit(upload_task, i, f, out, root, rel)
                else:
                    with lock: bytes_done[0] += f["size"]; job["done_bytes"] = bytes_done[0]
                if len(fh_cache) > 64:
                    for k in list(fh_cache)[:32]: fh_cache.pop(k).close()
                return
            for attempt in range(3):
              try:
                if remote_dest or os.path.lexists(out):   # (remote files not yet at the target went to the upload threads above)
                    already_there(f, existing[root].get(rel) if remote_dest else local_size(out)); break
                tmp = os.path.join(stage[root], rel); os.makedirs(os.path.dirname(tmp), exist_ok=True)
                pool.extract(f["off"], tmp, size=f["size"], mtime=f["mtime"], job=None, fh_cache=fh_cache)
                if not place_file(tmp, out):     # appeared since the check above: kept
                    already_there(f, local_size(out)); break
                restored(f); break
              except Exception as e:
                if attempt < 2 and not isinstance(e, KeyError):      # KeyError = chunk not in pool map: retrying will not help
                    job["message"] = f"{f['share']}/{f['path']}: {e} — retrying"; time.sleep([2, 10][attempt]); continue
                add_error(f, e)
                if fatal: raise RuntimeError(fatal[0])
                break                                              # recorded once; without this a non-retried error was logged on every attempt
            with lock: bytes_done[0] += f["size"]; job["done_bytes"] = bytes_done[0]
            if len(fh_cache) > 64:
                for k in list(fh_cache)[:32]: fh_cache.pop(k).close()
        job["next_index"] = start_at; persist()
        try:
            for i, f in enumerate(files):
                if i < start_at or i in done_ahead:
                    with lock: bytes_done[0] += f["size"]
                    continue
                if fatal: raise RuntimeError(fatal[0])
                reap()
                if job.get("cancel"):
                    drain(); r = resume_point(i)                             # running uploads end within a second (cancelled)
                    job["next_index"] = r; job["message"] = "paused"; persist("paused"); return result(paused=True, next_index=r, bytes=bytes_done[0])
                if i % 10 == 0:
                    for bk in [k for k, fut in pending.items() if fut.done()]: pending.pop(bk)
                    prefetch_from(i); evict()
                process(i, f)
            drain()
            if fatal: raise RuntimeError(fatal[0])
            if job.get("cancel") or up_cancelled:
                r = resume_point(len(files)); job["next_index"] = r; job["message"] = "paused"; persist("paused")
                return result(paused=True, next_index=r, bytes=bytes_done[0])
            # Final check (T8 still holds: nothing is written over): every file of the plan must be at its target with the size the
            # backup gives. Missing ones are restored again, up to CHECK_ROUNDS times; what stays wrong is listed as an error.
            # A Stop here pauses at the end of the plan, so Resume runs the check again.
            job["next_index"] = len(files); bytes_at_end = bytes_done[0]
            for rnd in range(CHECK_ROUNDS + 1):
                job["message"] = "checking the restored files at the target"; job["progress"] = 1.0 if rnd else 0.99
                chk = check_targets()
                if chk.get("error") or not chk["missing"] or rnd == CHECK_ROUNDS: break
                log(f"restore check round {rnd + 1}: {len(chk['missing'])} missing, restoring them again")
                with lock: failed_up = any("upload" in e["error"] for e in job["errors"])
                if failed_up:                     # an upload refused by the NAS (a file in use there: "share access flags are
                    t_end = time.monotonic() + CHECK_WAIT_S   # incompatible") lasts minutes, not seconds: wait before trying again
                    while time.monotonic() < t_end and not job.get("cancel"):
                        job["message"] = f"{len(chk['missing'])} file(s) not at the target yet: trying again in {int(t_end - time.monotonic()) + 1} s"
                        threading.Event().wait(1)
                for root in existing: existing[root] = destination_sizes(root)
                for k, (i, f) in enumerate(chk["missing"]):
                    if fatal: raise RuntimeError(fatal[0])
                    reap()
                    if job.get("cancel"): break
                    process(i, f, again=(k, len(chk["missing"])))
                drain()
                if fatal: raise RuntimeError(fatal[0])
                if job.get("cancel") or up_cancelled:
                    up_cancelled.clear(); job["message"] = "paused"; persist("paused")
                    return result(paused=True, next_index=len(files), bytes=bytes_at_end)
                chk_retried[0] += len(chk["missing"])
            bytes_done[0] = bytes_at_end; job["done_bytes"] = bytes_at_end
            finish_check(chk)
        except Exception:
            persist("error"); raise
        finally:
            job["cancel_uploads"] = True
            up_ex.shutdown(wait=True, cancel_futures=True)              # running uploads end with the cancel flag or finish
            with lock: tx.clear(); publish_tx()
            ex.shutdown(wait=False, cancel_futures=True)
            for fh in fh_cache.values(): fh.close()
            for root, st in stage.items():               # only this job's staging folders; never anything else at a destination
                if not is_remote_path(root): shutil.rmtree(st, ignore_errors=True)
            if remote_stage: shutil.rmtree(remote_stage, ignore_errors=True)
        evict(0)                                         # finished: the downloaded buckets are not kept (a paused job keeps them)
        job["progress"] = 1.0; job["next_index"] = len(files); persist("done")
        return result(files=len(files), bytes=bytes_done[0])

    # --- content extraction (small files stored in Pool/file_pool/<n>.file)
    def extract(self, share, ver, fid, job=None):
        c = self.conn(share, ver)
        r = c.execute("select * from files where id = ?", (fid,)).fetchone(); c.close()
        if not r: raise FileNotFoundError("file not in index")
        off = r["off"]
        if r["is_dir"]: raise RuntimeError("directory")
        if off is None or off >= 0:
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
