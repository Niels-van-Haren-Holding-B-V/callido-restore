"""One rclone for the whole app (H21). A fresh ``rclone`` process per call pays for start-up, config and token refresh every time
(1.3-5 s per Google Drive listing); a persistent ``rclone rcd`` daemon answers the same listing in 0.3 s. ``start()`` launches the
daemon on a random 127.0.0.1 port with random credentials (no remote access, no other local process can drive it); every helper
here uses it when it runs and otherwise falls back to the same ``rclone`` command line as before, so tests that mock
``subprocess.run`` and a machine where the daemon fails to start keep working. Copies and config changes still run as separate
rclone processes; the daemon shares the config file with them."""
import base64, json, secrets, socket, subprocess, threading, time, urllib.error, urllib.request
from callido_restore.paths import RCLONE


class RcloneError(RuntimeError):
    """rclone refused or failed a request; the message is rclone's own text."""


class _Daemon:
    def __init__(self):
        self.proc = None; self.url = None; self.auth = None; self.lock = threading.Lock(); self.failed = ""

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout=10.0):
        """Start ``rclone rcd`` once; True when it answers ``core/version``. A failure is remembered and not retried."""
        with self.lock:
            if self.running: return True
            if self.failed or not RCLONE: return False
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
            user, pw = secrets.token_hex(8), secrets.token_hex(16)
            try:
                self.proc = subprocess.Popen([RCLONE, "rcd", "--rc-addr", f"127.0.0.1:{port}", "--rc-user", user, "--rc-pass", pw,
                                              "--rc-serve=false", "--rc-web-gui=false", "--log-level", "ERROR"],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            except OSError as e:
                self.failed = str(e); self.proc = None; return False
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
        """One remote-control call; raises RcloneError with rclone's message."""
        if not self.running: raise RcloneError("rclone daemon is not running")
        return self._call(method, params, timeout=timeout)


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


def split_path(path):
    """'gdrive:Synology/x' -> ('gdrive:', 'Synology/x'); a local path -> (path, '')."""
    if ":" in path and not path.startswith("/"):
        name, _, rest = path.partition(":")
        return name + ":", rest
    return path, ""


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
    if DAEMON.running:
        fs, remote = split_path(path)
        opt = {"noMimeType": True, "dirsOnly": bool(dirs_only), "filesOnly": bool(files_only), "recurse": bool(recurse), "noModTime": bool(no_modtime)}
        params = {"fs": fs, "remote": remote, "opt": opt}
        if max_depth is not None: params["_config"] = {"MaxDepth": int(max_depth)}
        return DAEMON.rc("operations/list", timeout=timeout, **params).get("list") or []
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
    if DAEMON.running:
        names = DAEMON.rc("config/listremotes").get("remotes") or []
        dump = DAEMON.rc("config/dump")
        return [(n + ":", (dump.get(n) or {}).get("type", "")) for n in names]
    out = _run(["listremotes", "--long"])
    res = []
    for line in out.splitlines():                         # "name: type"
        if ":" in line:
            n, _, t = line.partition(":"); res.append((n.strip() + ":", t.strip()))
    return res


def version():
    """'rclone v1.75.2' (first line of ``rclone version``)."""
    if DAEMON.running:
        v = DAEMON.rc("core/version").get("version", ""); return f"rclone {v}" if v else ""
    return _run(["version"]).splitlines()[0].strip()
