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
from callido_restore.paths import RCLONE, ui_path
from callido_restore.restore.destination import file_target
from callido_restore.restore.destination import list_destination
from callido_restore.restore.destination import place_file
from callido_restore.restore.destination import restore_roots
from callido_restore.sources.source import Source
from callido_restore.sources.source import is_remote_path

UNVERIFIED_MAX = 2000        # unverified files listed per job (like errors); the counts are complete

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
        for i, sh in enumerate(shares): log(f"Listing shares {i + 1}/{len(shares)}…"); tally(s.listdir(f"Config/@Share/{sh}"))
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
        st["complete"] = self.pool_prepared()           # a run of this mapper version finished (partial maps are its honest result)
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
        try: self._prepared_file().unlink()                        # a run in progress is not a finished one
        except OSError: pass
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
            def report(message, done, total):                    # bytes of the set's slices fetched so far: the UI shows speed
                job["message"] = message; job["done_bytes"] = done; job["total_bytes"] = total
            pool.build_runs(self.src, str(pool_dir), log, cancelled=lambda: bool(job.get("cancel")),
                            progress=lambda f: job.__setitem__("progress", 0.1 + 0.2 * f), report=report)   # this phase spans 10-30 % of the job
        if job.get("cancel"): return stopped("chunk-records")
        job["message"] = "collecting file sizes from loaded snapshots"; job["progress"] = 0.3
        dbs = [(str(p), "index") for p in self.src.index_dir.glob("*__*.sqlite")] + [(p, "raw") for p in self.raw_snapshots()]
        pool.build_recsizes(self.src, dbs, str(pool_dir), log, cancelled=lambda: bool(job.get("cancel")))
        if job.get("cancel"): return stopped("record-sizes")
        # The anchor-first mapper (T1) identifies records by the file tags of the raw snapshots: recmeta_set<s>.json.
        job["message"] = "collecting file tags from loaded snapshots"; job["progress"] = 0.32
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
            raise RuntimeError("no snapshot database is on this Mac yet: load the file list of at least one share for the chosen run (the chips above the tabs), then run the preparation again")
        job["message"] = "mapping buckets to writers"; job["progress"] = 0.7
        def mlog(m):
            job["message"] = str(m)
            mm = re.search(r"scanned (\d+)/(\d+)", str(m))                 # anchor scan: 70-95 % of the job
            if mm: job["progress"] = 0.7 + 0.25 * int(mm.group(1)) / max(1, int(mm.group(2))); return
            mm = re.search(r"placing writer (\d): ([\d,]+)/([\d,]+)", str(m))      # placement: 96-100 %, four writers in turn
            if mm: job["progress"] = 0.96 + 0.04 * (int(mm.group(1)) + int(mm.group(2).replace(",", "")) / max(1, int(mm.group(3).replace(",", "")))) / 4; return
            if str(m).startswith(("owners from anchors", "placement", "phase2", "  resolving copies")): job["progress"] = 0.955; return
            mm = re.search(r"mapped (\d+)/(\d+)", str(m))                  # v1 search, or the anchor-first summary line
            if mm: job["progress"] = 0.7 + 0.3 * int(mm.group(1)) / max(1, int(mm.group(2)))
        # In a child process: the app stays responsive while the search runs at full CPU, and Stop ends it at once.
        res, upto = poolmap.build_poolmap_detached(self.src, str(pool_dir), mlog, max_bucket=max_bucket, cancelled=lambda: bool(job.get("cancel")))
        self._pool = None
        if job.get("cancel"): return stopped("mapping")
        self.mark_prepared(len(res), self.pool_status().get("total_buckets", 0))     # finished: the auto-start leaves it alone now
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
        the others go under dest as <share>/<path>. Files that already exist at a destination are skipped, whatever their
        size or date; nothing at a destination is ever written over or deleted (T8)."""
        paths.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        jf = paths.JOBS_DIR / f"{job['id']}.json"
        def persist(state="running"):
            jf.write_text(json.dumps({"id": job["id"], "src": self.src.id, "items": items, "dest": dest, "roots": roots, "skip_meta": skip_meta,
                                      "next_index": job.get("next_index", 0), "done_files": job.get("done_files", 0), "skipped": job.get("skipped", 0),
                                      "errors": job.get("errors", [])[-200:], "total_files": job.get("total_files"), "total_bytes": job.get("total_bytes"),
                                      "verified": job.get("verified", 0), "identical_copy": job.get("identical_copy", 0), "unverified": job.get("unverified", [])[:UNVERIFIED_MAX],
                                      "done_bytes": job.get("done_bytes", 0), "state": state, "label": job["label"], "started": job["started"], "updated": time.time()}))
        roots = restore_roots(items, dest)                       # every folder written into, local paths or rclone remotes
        existing = {}                                            # remote root -> files already there (never written over)
        for root in roots:
            if not is_remote_path(root): continue
            job["message"] = f"listing destination {root}"
            try: existing[root] = list_destination(root)
            except Exception:
                persist("error"); raise
        files = self.restore_plan(items, skip_meta)
        total_bytes = sum(f["size"] for f in files); done_bytes = 0
        job["total_files"] = len(files); job["total_bytes"] = total_bytes; job["done_files"] = 0; job["errors"] = []; job["skipped"] = 0
        job["verified"] = 0; job["identical_copy"] = 0; job["unverified"] = []      # per restored file: how its placement was established
        pool = self.pool; fh_cache = {}
        def restored(f):
            """count the file as restored by how the map established its placement (PoolReader.verification): "verified" = the
            placement of every record the file reads from matched the file sizes when the map was built and each chunk's md5 was
            checked on read (not a file-level checksum); "identical_copy" = restored from an identical record's entries;
            "unverified" = listed with the reason"""
            job["done_files"] += 1
            st, why = ("verified", "") if f["off"] is not None and f["off"] < 0 else ("unverified", "the pool map gives no status")
            if f["off"] is not None and f["off"] >= 0 and hasattr(pool, "verification"):
                try: st, why = pool.verification(f["off"])
                except Exception as e: st, why = "unverified", f"status check failed: {e}"
            if st == "verified": job["verified"] += 1
            elif st == "identical_copy": job["identical_copy"] += 1
            elif len(job["unverified"]) < UNVERIFIED_MAX: job["unverified"].append({"path": f"{f['share']}/{f['path']}", "reason": why})
        def result(**extra):
            return {**extra, "restored": job["done_files"], "skipped": job["skipped"], "errors": len(job["errors"]), "verified": job["verified"],
                    "identical_copy": job["identical_copy"], "unverified": len(job["unverified"])}
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
        prof = transfer.profile("restore")                     # sources/transfer.py: 8 bucket files in flight, 16 queued ahead
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=prof.workers); pending = {}
        def prefetch_from(i):
            seen = set()
            for f in files[i:i + 40]:
                if f["off"] is None or f["off"] < 0: continue
                try: locs, _ = pool.plan(f["off"])
                except Exception: continue
                for bk, _ in locs:
                    if bk in seen or bk in pending: continue
                    seen.add(bk)
                    if len(pending) < prof.prefetch: pending[bk] = ex.submit(fetch, bk)
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
        job["next_index"] = start_at; persist()
        try:
            for i, f in enumerate(files):
                if i < start_at: done_bytes += f["size"]; continue
                if job.get("cancel"):
                    job["message"] = "paused"; persist("paused"); return result(paused=True, next_index=i, bytes=done_bytes)
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
                root, rel = file_target(f, items, dest); remote_dest = is_remote_path(root); out = os.path.join(root, rel)
                job["message"] = f"{i + 1}/{len(files)}: {f['share']}/{f['path']}"; job["progress"] = done_bytes / total_bytes if total_bytes else (i / max(1, len(files)))
                job["next_index"] = i
                if i % 50 == 0: persist()
                for attempt in range(3):
                  try:
                    if remote_dest:
                        if rel in existing[root]:
                            job["skipped"] += 1; job["done_files"] += 1; break
                        out = os.path.join(stage[root], rel); os.makedirs(os.path.dirname(out), exist_ok=True)
                        pool.extract(f["off"], out, size=f["size"], mtime=f["mtime"], job=None, fh_cache=fh_cache)
                        # --ignore-existing: if the file appeared at the target since the listing, rclone leaves it alone
                        r = subprocess.run([RCLONE, "moveto", out, f"{root}/{rel}", "--ignore-existing", "--retries", "5", "--low-level-retries", "20", "--retries-sleep", "5s"], capture_output=True, text=True, **popen_kw())
                        if r.returncode != 0: raise RuntimeError("upload failed: " + r.stderr.strip()[-200:])
                        if os.path.exists(out):      # not moved: the target already had it
                            os.unlink(out); job["skipped"] += 1; job["done_files"] += 1; break
                        restored(f); break
                    if os.path.lexists(out):         # anything already there is kept as is, whatever its size or date
                        job["skipped"] += 1; job["done_files"] += 1; break
                    tmp = os.path.join(stage[root], rel); os.makedirs(os.path.dirname(tmp), exist_ok=True)
                    pool.extract(f["off"], tmp, size=f["size"], mtime=f["mtime"], job=None, fh_cache=fh_cache)
                    if not place_file(tmp, out):     # appeared since the check above: kept, counted as skipped
                        job["skipped"] += 1; job["done_files"] += 1; break
                    restored(f); break
                  except Exception as e:
                    if attempt < 2 and not isinstance(e, KeyError):      # KeyError = chunk not in pool map: retrying will not help
                        job["message"] = f"{f['share']}/{f['path']}: {e} — retrying"; time.sleep([2, 10][attempt]); continue
                    job["errors"].append({"path": f"{f['share']}/{f['path']}", "error": str(e)})
                    if len(job["errors"]) > 2000: raise RuntimeError("too many errors, aborting")
                done_bytes += f["size"]; job["done_bytes"] = done_bytes
                if len(fh_cache) > 64:
                    for k in list(fh_cache)[:32]: fh_cache.pop(k).close()
        except Exception:
            persist("error"); raise
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
            for fh in fh_cache.values(): fh.close()
            for root, st in stage.items():               # only this job's staging folders; never anything else at a destination
                if not is_remote_path(root): shutil.rmtree(st, ignore_errors=True)
            if remote_stage: shutil.rmtree(remote_stage, ignore_errors=True)
        job["progress"] = 1.0; job["next_index"] = len(files); persist("done")
        return result(files=len(files), bytes=done_bytes)

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
