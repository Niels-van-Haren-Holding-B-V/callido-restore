"""The JSON API and the static UI, served on 127.0.0.1 only (``ThreadingHTTPServer(("127.0.0.1", port), H)``). Handlers call into
``sources`` and ``restore``; long operations go through ``restore.jobs``."""
import json, os, re, subprocess, sys, tempfile, threading, time, traceback, uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from callido_restore import paths
from callido_restore.about import APP_NAME, TAGLINE, NOTICE, VERSION
from callido_restore.paths import RCLONE, _bundled
from callido_restore.restore.backup import Backup
from callido_restore.restore.destination import restore_target_error
from callido_restore.restore.jobs import JOBS, start_job
from callido_restore.sources import google, rclone
from callido_restore.sources.source import SOURCES, add_source, save_sources

def jdump(o): return json.dumps(o, ensure_ascii=False).encode("utf-8")

def human(n):
    for u in ["B","KB","MB","GB","TB"]:
        if n < 1024: return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"

def _exit_app(code=0):
    """Stop the whole app: rclone children first (an `rclone lsjson` used to survive Quit), then the process. Restore jobs are
    persisted and can be resumed; the desktop window (if any) closes with the process."""
    rclone.stop()
    try:
        from callido_restore.pool import poolmap
        poolmap.kill_running()                                     # the pool mapper and its scan workers, as a group
    except Exception: pass
    if os.name != "nt": subprocess.run(["pkill", "-P", str(os.getpid())], capture_output=True)
    os._exit(code)

JOB_ID = re.compile(r"[0-9a-f]{8,64}")             # job ids are uuid4 hex (10 chars); the id names a file under paths.JOBS_DIR, so nothing else may pass

def job_file(jid):
    """Path of a persisted restore job, or None when the id is not a job id (never a path)."""
    return paths.JOBS_DIR / f"{jid}.json" if isinstance(jid, str) and JOB_ID.fullmatch(jid) else None

BACKUPS = {}

# The pool preparation is not optional (every restore needs the map), so it starts by itself: after the backup is opened and
# after every share load, as soon as a raw snapshot is local, unless a map exists, a preparation runs, or the user paused it.
AUTOPREPARE = os.environ.get("HBK_NO_AUTOPREPARE") != "1"
POOL_LOCK = threading.Lock()                     # check-and-start of a preparation is atomic: open, share loads and Resume all race

def pool_job(sid):
    """The running preparation job of a source, or None."""
    return next((j for j in JOBS.values() if j["state"] == "running" and j["label"] == "build pool map" and j.get("src") == sid), None)

def start_pool_job(sid, b, max_bucket=None):
    """The running preparation of the source, or a new one (lifting a pause). Never two for one source."""
    with POOL_LOCK:
        j = pool_job(sid)
        if j: return j
        b.set_pool_paused(False)
        return start_job(lambda job: b.build_pool(job, max_bucket), "build pool map", src=sid)

def maybe_autoprepare(sid):
    """Start the preparation when it can run and has not finished; None when nothing was started (and why is not an error)."""
    if not AUTOPREPARE or sid not in BACKUPS: return None
    b = BACKUPS[sid]
    try:                                                   # the slow checks (a share listing may go to the remote) run outside the lock
        if pool_job(sid) or b.pool_paused() or b.pool_status().get("complete") or not b.raw_snapshots(): return None
    except Exception: return None
    with POOL_LOCK:                                        # the cheap checks again, atomically with the start
        if pool_job(sid) or b.pool_paused(): return None
        b.set_pool_paused(False)
        return start_job(lambda job: b.build_pool(job, None), "build pool map", src=sid)

def backup_for(sid, refresh=False, log=None):
    if sid not in SOURCES: raise KeyError(f"unknown source {sid}")
    if refresh: SOURCES[sid].forget_listings()                   # the ↻ button: read the backup's folders from the remote again
    if refresh or sid not in BACKUPS: BACKUPS[sid] = Backup(SOURCES[sid], log)
    return BACKUPS[sid]

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
                try: line = rclone.version() or "rclone not found"
                except (rclone.RcloneError, OSError): line = "rclone not found"
                return self._json({"name": APP_NAME, "tagline": TAGLINE, "notice": NOTICE, "version": VERSION, "rclone": line, "rclone_path": RCLONE, "config": str(paths.CONF_DIR)})
            if u.path == "/api/quit":
                threading.Timer(0.3, _exit_app).start(); return self._json({"ok": True})
            if u.path == "/api/ls":   # directory picker helper
                p = Path(os.path.expanduser(q.get("path") or "~"))
                if not p.is_dir(): p = p.parent
                items = sorted([e.name for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")], key=str.lower)
                return self._json({"path": str(p), "parent": str(p.parent), "dirs": items})
            if u.path == "/api/dest/check":   # restore destination: is it inside the backup? does it exist? is it empty?
                dest = (q.get("dest") or "").strip(); src = SOURCES.get(q.get("src") or "")     # no backup scan needed for this
                shares = json.loads(q.get("shares") or "[]")                                    # selected shares: the restore writes dest/<share>/...
                if q.get("root"): conflict = src.contains_root(dest) if src and dest else ""      # dest is itself a written root (H8 mapping row)
                else: conflict = src.contains_dest(dest, shares) if src and dest else ""
                out = {"dest": dest, "inside_source": bool(conflict), "conflict": conflict or None, "exists": False, "empty": None, "files": 0}
                if not dest: return self._json(out | {"error": "no destination"})
                if ":" in dest and not dest.startswith("/"):
                    try:
                        ents = rclone.ls(dest, max_depth=1, timeout=120); out["exists"] = True; out["files"] = len(ents); out["empty"] = not ents
                    except rclone.RcloneError as e:
                        err = str(e)[-200:]
                        if "directory not found" in err: out["exists"] = False      # rclone's wording for a missing folder; "remote not found" etc. are errors
                        else: out["error"] = err
                else:
                    pp = Path(os.path.expanduser(dest))
                    out["exists"] = pp.is_dir()
                    if out["exists"]:
                        try: ents = [e for e in pp.iterdir() if e.name != ".DS_Store"]; out["files"] = len(ents); out["empty"] = not ents
                        except OSError as e: out["error"] = str(e)
                    elif pp.exists(): out["error"] = "destination exists but is not a folder"
                    elif not pp.parent.is_dir(): out["error"] = f"the folder above it does not exist: {pp.parent} (is the share mounted?)"
                return self._json(out)
            if u.path == "/api/remotes":
                try: pairs = rclone.remotes(cache=True)
                except rclone.RcloneError: pairs = []
                try: keys = {n: i["own_key"] for n, i in rclone.remote_info(cache=True).items()}
                except rclone.RcloneError: keys = {}
                out = {"remotes": [n for n, _ in pairs], "types": dict(pairs), "keys": keys, "dirs": []}
                if q.get("remote"):
                    try: out["dirs"] = [e["Name"] for e in rclone.ls(q["remote"] + (q.get("path") or ""), dirs_only=True, cache=True)]
                    except rclone.RcloneError as e: out["error"] = str(e)[-200:]
                return self._json(out)
            if u.path == "/api/rclone/ls":      # browse a remote (or local path when remote == "")
                rem, path = q.get("remote", ""), q.get("path", "")
                if rem:
                    # one listing per click through the shared rclone (daemon when it runs, 30 s cache for repeats); split on IsDir here
                    try: ents = rclone.ls(rem + path, max_depth=1, cache=True)
                    except rclone.RcloneError as e: return self._json({"error": str(e)[-300:]})
                    dirs = sorted(e["Name"] for e in ents if e.get("IsDir")); files = sorted(e["Name"] for e in ents if not e.get("IsDir"))
                else:
                    pp = Path(os.path.expanduser(path or "~"))
                    try: dirs = sorted(e.name for e in pp.iterdir() if e.is_dir() and not e.name.startswith(".")); files = sorted(e.name for e in pp.iterdir() if e.is_file())[:50]
                    except OSError as e: return self._json({"error": str(e)})
                    path = str(pp)
                looks_hbk = "Config" in dirs and "Pool" in dirs
                return self._json({"path": path, "dirs": dirs, "files": files[:50], "looks_like_hbk": looks_hbk, "hbk_dirs": [d for d in dirs if d.endswith(".hbk")]})
            if u.path == "/api/rclone/connect":  # secrets (password, client secret) go in a POST body, never in the query string
                return self._json({"error": "use POST for /api/rclone/connect"}, 405)
            if u.path == "/api/jobs/saved":
                out = []
                for jf in sorted(paths.JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
                    try: d = json.loads(jf.read_text())
                    except Exception: continue
                    if d["id"] in JOBS and JOBS[d["id"]]["state"] == "running": d["state"] = "running"
                    out.append({k: d.get(k) for k in ("id", "label", "state", "dest", "roots", "items", "next_index", "total_files", "done_files", "skipped", "total_bytes", "done_bytes", "started", "updated", "src", "verified", "identical_copy")} | {"errors": len(d.get("errors") or []), "unverified": len(d.get("unverified") or [])})
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
                if q.get("job") == "1":          # the first open lists and fetches over rclone: run it as a job with step messages; the UI then GETs the info
                    sid, refresh = q["src"], q.get("refresh") == "1"
                    if sid not in SOURCES: return self._json({"error": f"unknown source {sid}"}, 404)
                    def run(job):
                        job["message"] = "Opening the backup…"
                        backup_for(sid, refresh=refresh, log=lambda m: job.__setitem__("message", m))
                        maybe_autoprepare(sid); return {"ok": True}
                    return self._json(start_job(run, "open backup"))
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
                    maybe_autoprepare(q["src"])                        # a raw snapshot is local now: the preparation can run
                    return out
                return self._json(start_job(run, f"index v{ver} {share}"))
            if u.path == "/api/query": return self._json(b.query(q.get("share", "*"), int(q["ver"]), q))
            if u.path == "/api/children": return self._json(b.children(q["share"], int(q["ver"]), q.get("dir", "")))
            if u.path == "/api/histogram": return self._json(b.histogram(q.get("share", "*"), int(q["ver"]), q))
            if u.path == "/api/file": return self._json(b.file_detail(q["share"], int(q["ver"]), q["id"]))
            if u.path == "/api/diff": return self._json(b.diff(q["share"], int(q["a"]), int(q["b"])))
            if u.path == "/api/pool/build":                       # an explicit start also lifts a pause
                mb = int(q["max_bucket"]) if q.get("max_bucket") else None
                return self._json(start_pool_job(q["src"], b, mb))
            if u.path == "/api/pool":
                st = b.pool_status(); j = pool_job(q["src"]); st["running"] = j["id"] if j else None
                return self._json(st)
            if u.path == "/api/restore_count":
                return self._json(b.restore_count(json.loads(q["items"]), q.get("skip_meta", "1") == "1"))
            if u.path == "/api/restore_plan":
                items = json.loads(q["items"])
                return self._json(b.plan_mappings(items, (q.get("dest") or "").strip(), q.get("skip_meta", "1") == "1"))
            if u.path == "/api/download":
                c = b.conn(q["share"], int(q["ver"])); r = c.execute("select * from files where id = ?", (q["id"],)).fetchone(); c.close()
                if not r: return self._json({"error": "no such file"}, 404)
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
            if u.path in ("/api/pool/pause", "/api/pool/resume"):   # the user's pause survives share loads and restarts
                sid = body.get("src")
                if sid not in SOURCES: return self._json({"error": f"unknown source {sid}"}, 404)
                b = backup_for(sid)
                if u.path.endswith("pause"):
                    b.set_pool_paused(True); j = pool_job(sid)
                    if j: j["cancel"] = True
                    return self._json({"paused": True, "cancelled": j["id"] if j else None})
                b.set_pool_paused(False)
                if b.pool_status().get("complete") and not pool_job(sid):   # nothing to resume: say so instead of a full rebuild
                    st = b.pool_status(); st["running"] = None; return self._json(st)
                return self._json(start_pool_job(sid, b))
            if u.path == "/api/rclone/connect":  # start an rclone config (opens the browser for Google); body carries the secrets
                name = re.sub(r"[^A-Za-z0-9_-]", "", body.get("name") or "gdrive") or "gdrive"
                kind = body.get("kind", "drive")
                if kind not in ("drive", "smb"): return self._json({"error": "unknown kind"}, 400)
                # A name that already belongs to a connection of another type is refused before anything runs: the SMB restore
                # target must never be replaced by a Drive section, nor a Drive connection by an SMB one. A known Drive remote
                # is updated, not re-created, so options set outside the app (root folder, shared drive, ...) survive.
                try: existing = dict(rclone.remotes()).get(name + ":")
                except rclone.RcloneError: existing = None
                if existing and existing != kind:
                    want = "Google Drive" if kind == "drive" else "an SMB share"
                    return self._json({"error": f"“{name}” is a {existing} connection, not {want}. Pick that connection, or use a new name."}, 400)
                if kind == "drive":
                    cid, csec = (body.get("client_id") or "").strip(), (body.get("client_secret") or "").strip()
                    if cid and google.client_id_error(cid): return self._json({"error": google.client_id_error(cid)}, 400)
                    if cid and not csec: return self._json({"error": "the client secret is missing"}, 400)
                    args = None
                elif kind == "smb":
                    # the password is handed to rclone over stdin ("-"), so it never appears in a process list; the obscured
                    # form that lands in rclone.conf is the only thing passed as an argument
                    pw = (body.get("password") or "").replace("\n", "")
                    obs = subprocess.run([RCLONE, "obscure", "-"], input=pw + "\n", capture_output=True, text=True).stdout.strip()
                    if not obs: return self._json({"error": "rclone could not prepare the password"}, 500)
                    args = [RCLONE, "config", "create", name, "smb", f"host={body.get('host', '')}", f"user={body.get('user', '')}", f"pass={obs}", "--obscure=false", "--non-interactive"]
                def run(job):
                    if kind == "drive":
                        # The sign-in runs through `rclone authorize` with the app's own landing page (static/oauth.html) instead of
                        # rclone's plain one; the token it prints goes into the remote with `config create`, which then asks nothing.
                        if cid:                                             # a wrong pair would only show up as Google's error page mid-flow
                            job["message"] = "Checking the key with Google…"
                            err = google.client_check(cid, csec)
                            if err: raise RuntimeError(err)
                        job["message"] = "Signing in… a browser window opens for the Google sign-in. When it says “Signed in”, close it and come back to this window."
                        import webbrowser
                        try: token = rclone.authorize_drive(cid, csec, on_url=lambda u: webbrowser.open(u), template=_bundled("static/oauth.html"))
                        except rclone.RcloneError as e: raise RuntimeError("Google sign-in failed: " + str(e))
                        params = {"scope": "drive.readonly", "token": token}
                        if cid: params.update(client_id=cid, client_secret=csec)
                        try:
                            if existing == "drive": rclone.config_update(name, params)
                            else: rclone.config_create(name, "drive", params)
                        except rclone.RcloneError as e: raise RuntimeError(str(e))
                    else:
                        job["message"] = "Connecting…"
                        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
                        if r.returncode != 0: raise RuntimeError(r.stderr.strip()[-400:] or "rclone config failed")
                    rclone.forget(); rclone.fscache_clear()                     # new or changed connection: cached listings and backends are stale
                    job["message"] = "Listing your Drive…" if kind == "drive" else "Listing the shares…"     # the root listing can take a while
                    try: shares = [e["Name"] for e in rclone.ls(f"{name}:", dirs_only=True, timeout=60)]
                    except rclone.RcloneError as e: raise RuntimeError("remote created but listing failed: " + str(e)[-300:])
                    return {"remote": name + ":", "shares": shares}
                return self._json(start_job(run, f"connect {kind}"))

            if u.path == "/api/restore":
                b = backup_for(body["src"]); dest = os.path.expanduser((body.get("dest") or "").strip())
                items = []
                for it in body["items"]:                                           # H8: an item may carry its own target
                    it = dict(it); d = os.path.expanduser((it.get("dest") or "").strip()).rstrip("/")
                    if d: it["dest"] = d
                    else: it.pop("dest", None)
                    items.append(it)
                err = restore_target_error(b.src, items, dest, create=True)
                if err: return self._json({"error": err}, 400)
                return self._json(start_job(lambda job: b.restore(items, dest, job, body.get("skip_meta", True)), "restore"))
            if u.path == "/api/restore/resume":
                jf = job_file(body.get("id"))
                if jf is None: return self._json({"error": "not a job id"}, 400)
                if not jf.is_file(): return self._json({"error": "no such saved job"}, 404)
                d = json.loads(jf.read_text()); b = backup_for(d["src"]); dest = d.get("dest") or ""
                err = restore_target_error(b.src, d["items"], dest, create=False)     # source check again, roots still there
                if err: return self._json({"error": err}, 400)                        # the job file stays, so it can be resumed later
                jf.unlink()
                job = start_job(lambda job: b.restore(d["items"], dest, job, d.get("skip_meta", True), start_at=d.get("next_index", 0)), "restore (resumed)")
                return self._json(job)
            if u.path == "/api/sources/delete":
                SOURCES.pop(body["id"], None); BACKUPS.pop(body["id"], None); save_sources(); return self._json({"ok": True})
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e), "trace": traceback.format_exc()}, 500)
