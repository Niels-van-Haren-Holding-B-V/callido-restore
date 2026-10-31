"""One rclone for the whole app (H21). A fresh ``rclone`` process per call pays for start-up, config and token refresh every time
(1.3-5 s per Google Drive listing); a persistent ``rclone rcd`` daemon answers the same listing in 0.3 s. ``start()`` launches the
daemon on a random 127.0.0.1 port with random credentials (no remote access, no other local process can drive it); every helper
here uses it when it runs and otherwise falls back to the same ``rclone`` command line as before, so tests that mock
``subprocess.run`` and a machine where the daemon fails to start keep working. Copies and config changes still run as separate
rclone processes; the daemon shares the config file with them."""
import base64, json, os, secrets, socket, subprocess, threading, time, urllib.error, urllib.request
from callido_restore import paths
from callido_restore.paths import RCLONE


class RcloneError(RuntimeError):
    """rclone refused or failed a request; the message is rclone's own text."""


class DaemonGone(RcloneError):
    """The daemon is not running or stopped answering; helpers fall back to a plain rclone process."""


class _Daemon:
    def __init__(self):
        self.proc = None; self.url = None; self.auth = None; self.lock = threading.Lock(); self.failed = ""
        self.last_start = 0.0                                  # a daemon that died is restarted, but not more than once a minute

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout=10.0):
        """Start ``rclone rcd`` once; True when it answers ``core/version``. A failure is remembered and not retried.
        A daemon left behind by a previous run that was killed (pid file in the settings folder) is stopped first."""
        with self.lock:
            if self.running: return True
            if self.failed or not RCLONE: return False
            self.last_start = time.time()
            kill_stale()
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
            user, pw = secrets.token_hex(8), secrets.token_hex(16)
            try:
                self.proc = subprocess.Popen([RCLONE, "rcd", "--rc-addr", f"127.0.0.1:{port}", "--rc-user", user, "--rc-pass", pw,
                                              "--rc-serve=false", "--rc-web-gui=false", "--log-level", "ERROR"],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            except OSError as e:
                self.failed = str(e); self.proc = None; return False
            try:
                paths.CONF_DIR.mkdir(parents=True, exist_ok=True)
                PIDFILE().write_text(json.dumps({"pid": self.proc.pid, "parent": os.getpid()}))
            except OSError: pass
            self.url = f"http://127.0.0.1:{port}/"
            self.auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self.proc.poll() is not None: break
                try:
                    self._call("core/version", {}, timeout=1.0); return True
                except Exception: time.sleep(0.1)
            self.failed = "rclone rcd did not start"; self.stop(); return False

    def stop(self):
        p = self.proc; self.proc = None
        if p is not None and p.poll() is None:
            p.terminate()
            try: p.wait(3)
            except subprocess.TimeoutExpired: p.kill()
        try: PIDFILE().unlink()
        except OSError: pass

    def _call(self, method, params, timeout=None):
        req = urllib.request.Request(self.url + method, data=json.dumps(params).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try: msg = json.loads(e.read() or b"{}").get("error") or str(e)
            except Exception: msg = str(e)
            raise RcloneError(msg) from None

    def rc(self, method, timeout=None, **params):
        """One remote-control call; raises RcloneError with rclone's message, DaemonGone when the daemon cannot be reached
        (it was killed or crashed): the caller then falls back to a plain rclone process and the next call restarts it."""
        if not self.running: raise DaemonGone("rclone daemon is not running")
        try: return self._call(method, params, timeout=timeout)
        except urllib.error.HTTPError: raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            self.stop()
            raise DaemonGone(f"rclone daemon unreachable: {e}") from None

    def available(self):
        """True when the daemon runs; restarts a daemon that died (not one that failed to start), at most once a minute."""
        if self.running: return True
        if self.failed or not RCLONE or self.last_start == 0.0: return False
        if time.time() - self.last_start < 60: return False
        return self.start(timeout=5.0)


def PIDFILE():
    """One pid file per app instance (named by this process's pid): a second copy of the app or a dev server next to it
    must never stop another instance's daemon or drop its pid file."""
    return paths.CONF_DIR / f"rclone-rcd-{os.getpid()}.pid"

def _cmdline(pid):
    """The command line of a process, '' when it does not exist (patched in tests)."""
    if os.name == "nt": return ""
    r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""

def _alive(pid):
    """True when a process with this pid exists (patched in tests)."""
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except OSError: return True

def kill_stale():
    """Stop daemons left behind by app runs that did not exit cleanly (force quit, crash): every ``rclone-rcd-*.pid`` whose
    recorded parent process is gone names one; it is killed only when that pid still runs an ``rclone rcd``. Files of running
    instances are left alone. Returns the number of daemons stopped."""
    stopped = 0
    for f in paths.CONF_DIR.glob("rclone-rcd-*.pid") if paths.CONF_DIR.is_dir() else []:
        try: d = json.loads(f.read_text())
        except (OSError, ValueError):
            d = None
        if not isinstance(d, dict):
            try: f.unlink()                                  # an old-format or unreadable file: forget it
            except OSError: pass
            continue
        pid, parent = int(d.get("pid") or 0), int(d.get("parent") or 0)
        if parent and parent != os.getpid() and _alive(parent): continue    # another instance is still running it
        try: f.unlink()
        except OSError: pass
        if pid <= 1 or "rclone" not in _cmdline(pid) or " rcd" not in _cmdline(pid): continue
        try: os.kill(pid, 15); stopped += 1
        except OSError: pass
    return stopped


DAEMON = _Daemon()
start = DAEMON.start
stop = DAEMON.stop

# Short-lived cache for listings the UI re-requests on every render (connections, NAS shares, wizard folders). Only listings
# the user asked for again within CACHE_TTL seconds come from here; anything that changes a remote calls forget().
CACHE_TTL = 30.0
_cache = {}; _cache_lock = threading.Lock()

def _cached(key, compute, ttl=None):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now: return hit[1]
    value = compute()
    with _cache_lock: _cache[key] = (now + (CACHE_TTL if ttl is None else ttl), value)
    return value

def forget(prefix=None):
    """Drop cached listings (all, or those whose path starts with ``prefix``) after a connection was added or a remote changed."""
    with _cache_lock:
        for k in [k for k in _cache if prefix is None or (isinstance(k, tuple) and str(k[1]).startswith(prefix))]: _cache.pop(k, None)


def start_in_background():
    """Start the daemon without blocking the caller (the app does this right after its server is up)."""
    threading.Thread(target=DAEMON.start, daemon=True, name="rclone-rcd").start()


def _run(args, timeout=None, **kw):
    if timeout is not None: kw["timeout"] = timeout
    r = subprocess.run([RCLONE, *args], capture_output=True, text=True, **kw)
    if r.returncode != 0: raise RcloneError(r.stderr.strip()[-300:] or f"rclone {args[0]} failed")
    return r.stdout


def ls(path, dirs_only=False, files_only=False, recurse=False, max_depth=None, no_modtime=False, timeout=None, cache=False):
    """Entries of ``path`` as ``rclone lsjson`` returns them (dicts with Path, Name, Size, IsDir, ModTime). Raises RcloneError.
    ``cache=True`` serves a repeat of the same listing within CACHE_TTL seconds without asking rclone (UI renders only)."""
    if cache:
        key = ("ls", path, dirs_only, files_only, recurse, max_depth, no_modtime)
        return _cached(key, lambda: ls(path, dirs_only, files_only, recurse, max_depth, no_modtime, timeout))
    if DAEMON.available():
        # The listed folder itself is the "fs" and remote is "", so every Path comes back relative to that folder exactly like
        # ``rclone lsjson <path>`` (with fs="nas:" remote="x" rclone would return "x/..." and list_destination's skip check would miss).
        opt = {"noMimeType": True, "dirsOnly": bool(dirs_only), "filesOnly": bool(files_only), "recurse": bool(recurse), "noModTime": bool(no_modtime)}
        params = {"fs": path, "remote": "", "opt": opt}
        if max_depth is not None: params["_config"] = {"MaxDepth": int(max_depth)}
        try: return DAEMON.rc("operations/list", timeout=timeout, **params).get("list") or []
        except DaemonGone: pass                                 # killed or crashed daemon: plain rclone below
    args = ["lsjson", "--no-mimetype"]
    if no_modtime: args.append("--no-modtime")
    if dirs_only: args.append("--dirs-only")
    if files_only: args.append("--files-only")
    if recurse: args.append("-R")
    if max_depth is not None: args += ["--max-depth", str(max_depth)]
    return json.loads(_run(args + [path], timeout=timeout) or "[]")


def remotes(cache=False):
    """[(name_with_colon, type)] of the configured remotes. ``cache=True``: a repeat within CACHE_TTL seconds is free."""
    if cache: return _cached(("remotes",), lambda: remotes())
    if DAEMON.available():
        try:
            names = DAEMON.rc("config/listremotes").get("remotes") or []
            out = []
            for n in names:                               # config/get per remote: only its type, not the whole config with tokens
                try: typ = (DAEMON.rc("config/get", name=n) or {}).get("type", "")
                except DaemonGone: raise
                except RcloneError: typ = ""
                out.append((n + ":", typ))
            return out
        except DaemonGone: pass
    out = _run(["listremotes", "--long"])
    res = []
    for line in out.splitlines():                         # "name: type"
        if ":" in line:
            n, _, t = line.partition(":"); res.append((n.strip() + ":", t.strip()))
    return res


def version():
    """'rclone v1.75.2' (first line of ``rclone version``)."""
    if DAEMON.available():
        try:
            v = DAEMON.rc("core/version").get("version", ""); return f"rclone {v}" if v else ""
        except DaemonGone: pass
    return _run(["version"]).splitlines()[0].strip()


# ----------------------------------------------------------------------------- downloads
RATE_LIMIT_WORDS = ("rateLimitExceeded", "userRateLimitExceeded", "429", "quota", "Too Many Requests", "403")

def _fallback_copy(src, dst, timeout=None):
    """One ``rclone copyto`` process (the pre-daemon way): throttled, retried by rclone itself."""
    _run(["copyto", src, str(dst), "--retries", "5", "--low-level-retries", "20", "--retries-sleep", "5s", "--tpslimit", "8",
          "--drive-pacer-min-sleep", "100ms"], timeout=timeout)


def copyfile(src, dst, progress=None, cancelled=None, poll=0.5, timeout=None):
    """Download one remote file ``src`` ("remote:path/file") to the local path ``dst`` through the daemon when it runs
    (``operations/copyfile`` as an async job; ``core/stats`` of its group gives bytes, speed and rclone's retry state, reported
    through ``progress(bytes_done, speed_bytes_per_s, note)`` where note is '' or 'rate-limited, retrying'), otherwise as one
    rclone process. ``cancelled()`` true stops the job. Raises RcloneError when rclone fails."""
    dst = str(dst)
    if not DAEMON.available():
        return _fallback_copy(src, dst, timeout)
    if ":" in src and not src.startswith("/"): fs, remote = src.split(":", 1)[0] + ":", src.split(":", 1)[1]
    else: fs, remote = os.path.split(src)                        # a local file: its folder is the fs, the name the remote part
    dst_dir, dst_name = os.path.split(dst)
    group = f"job/{secrets.token_hex(4)}"
    try:
        started = DAEMON.rc("operations/copyfile", srcFs=fs, srcRemote=remote, dstFs=dst_dir or ".", dstRemote=dst_name, _async=True, _group=group)
    except DaemonGone:
        return _fallback_copy(src, dst, timeout)
    jobid = started.get("jobid")
    t0 = time.time(); last_note = ""
    try:
        while True:
            try: st = DAEMON.rc("job/status", jobid=jobid)
            except DaemonGone:
                return _fallback_copy(src, dst, timeout)          # the daemon died mid-copy: finish the old way
            if st.get("finished"):
                if st.get("success"): break
                raise RcloneError(st.get("error") or "copy failed")
            if cancelled is not None and cancelled():
                try: DAEMON.rc("job/stop", jobid=jobid)
                except RcloneError: pass
                raise RcloneError("cancelled")
            if timeout and time.time() - t0 > timeout:
                try: DAEMON.rc("job/stop", jobid=jobid)
                except RcloneError: pass
                raise RcloneError(f"copy of {src} timed out after {timeout:.0f} s")
            if progress is not None:
                try: stats = DAEMON.rc("core/stats", group=group)
                except RcloneError: stats = {}
                done = sum(int(t.get("bytes") or 0) for t in (stats.get("transferring") or [])) + int(stats.get("bytes") or 0)
                speed = float(stats.get("speed") or 0.0)
                err = str(stats.get("lastError") or "")
                note = "rate-limited, retrying" if (stats.get("retryError") or any(w in err for w in RATE_LIMIT_WORDS)) and err else ""
                progress(done, speed, note); last_note = note
            time.sleep(poll)
    finally:
        try: DAEMON.rc("core/stats-delete", group=group)
        except RcloneError: pass
    if progress is not None: progress(os.path.getsize(dst) if os.path.exists(dst) else 0, 0.0, "")
