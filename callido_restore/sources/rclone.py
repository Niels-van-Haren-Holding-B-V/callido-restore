"""One rclone for the whole app (H21). A fresh ``rclone`` process per call pays for start-up, config and token refresh every time
(1.3-5 s per Google Drive listing); a persistent ``rclone rcd`` daemon answers the same listing in 0.3 s. ``start()`` launches the
daemon on a random 127.0.0.1 port with random credentials (no remote access, no other local process can drive it); every helper
here uses it when it runs and otherwise falls back to the same ``rclone`` command line as before, so tests that mock
``subprocess.run`` and a machine where the daemon fails to start keep working. Downloads go through the daemon too (``copyfile``,
an async job polled for bytes and speed); config changes still run as separate rclone processes, which share the config file."""
import base64, json, os, re, secrets, socket, subprocess, threading, time, urllib.error, urllib.request
from callido_restore import paths
from callido_restore.about import APP_NAME
from callido_restore import oscompat
from callido_restore.oscompat import popen_kw
from callido_restore.paths import RCLONE, is_remote_path
from callido_restore.sources import transfer


class RcloneError(RuntimeError):
    """rclone refused or failed a request; the message is rclone's own text."""


class DaemonGone(RcloneError):
    """The daemon is not running or stopped answering; helpers fall back to a plain rclone process."""


class Cancelled(RcloneError):
    """``copyfile`` was stopped through its ``cancelled()`` callback; the partial file may remain."""


# Resilience and Drive pacing live in sources/transfer.py, one place for the daemon and every rclone process.
THROTTLE = list(transfer.DAEMON)


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
            kill_stale(); kill_orphans()
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
            user, pw = secrets.token_hex(8), secrets.token_hex(16)
            try:
                self.proc = subprocess.Popen([RCLONE, "rcd", "--rc-addr", f"127.0.0.1:{port}", "--rc-user", user, "--rc-pass", pw,
                                              "--rc-serve=false", "--rc-web-gui=false", "--log-level", "ERROR", *THROTTLE],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **popen_kw())
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
            # A slow answer (an SMB listing that takes longer than the caller's timeout) is not a dead daemon: the daemon keeps
            # running and the caller gets rclone's silence as an error. Only a refused or broken connection means it is gone
            # (then the subprocess fallback and a restart a minute later make sense).
            reason = getattr(e, "reason", e)
            if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(e).lower():
                raise RcloneError(f"no answer from rclone within {timeout:.0f} s" if timeout else "no answer from rclone") from None
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
    """True when a process with this pid exists (patched in tests). Windows has no signal 0 (it is CTRL_C_EVENT there)."""
    if oscompat.WINDOWS: return oscompat.pid_alive(pid)
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
    except OSError: return True

def _terminate(pid, grace=2.0):
    """SIGTERM, and SIGKILL when the process is still there after ``grace`` seconds: an rclone whose app died with its disk image
    (force-unmounted volume) kept spinning at full CPU for hours and ignored a plain terminate. True when a signal was sent.
    On Windows any signal number is TerminateProcess, so the first call already ends it."""
    try: os.kill(pid, 15)
    except OSError: return False
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _alive(pid): return True
        time.sleep(0.1)
    try: os.kill(pid, 9)
    except OSError: pass
    return True

def _ps():
    """[(pid, ppid, executable)] of every process (patched in tests); [] where ``ps`` is not available. ``comm`` is the
    executable path, not the command line: a process that merely names our rclone in an argument must not match."""
    if os.name == "nt": return []
    try: r = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True)
    except OSError: return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)                                 # the executable path may contain spaces (the app name does)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit(): rows.append((int(parts[0]), int(parts[1]), parts[2].strip()))
    return rows

def kill_orphans():
    """Stop rclone processes of this app that outlived it: processes whose executable is the bundled rclone
    (``<app>.app/Contents/Frameworks/rclone``, any install location) and whose parent is gone (re-parented to pid 1). A crashed app leaves its daemon and the copy it was running
    behind; they keep using the user's Google Drive quota, so the live app's downloads crawl. Processes with a living parent
    (another running copy of the app) and rclone binaries from elsewhere are never touched. Returns the number stopped.
    Windows: a deliberate no-op for now (``_ps`` returns [] there; ppid 1 has no meaning). Follow-up in docs/TASKS.md T14."""
    marker = f"/{APP_NAME}.app/Contents/Frameworks/rclone"
    stopped = 0
    for pid, ppid, exe in _ps():
        if ppid != 1 or pid == os.getpid() or not exe.endswith(marker): continue
        if _terminate(pid): stopped += 1
    return stopped

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
        if _terminate(pid): stopped += 1
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


AUTH_URL = re.compile(r"https?://127\.0\.0\.1:\d+/auth\?state=\S+")

def authorize_drive(client_id="", client_secret="", on_url=None, template=None, timeout=600, scope="drive.readonly"):
    """Run the Google sign-in with ``rclone authorize`` and return the token JSON rclone prints. The consent asks for ``scope``
    only (read-only by default: the app never writes to the Drive). rclone listens on 127.0.0.1 for
    Google's redirect and renders ``template`` (a Go HTML template, our own page instead of rclone's plain one) when it arrives;
    the browser is not opened by rclone but by ``on_url(url)``, so the app decides how. Without a client id rclone's shared
    one is used. Raises RcloneError with rclone's last lines when the sign-in fails or the user never completes it."""
    args = [RCLONE, "authorize", "drive"]
    if client_id: args += [client_id, client_secret or ""]
    args += ["--drive-scope", scope, "--auth-no-open-browser"]
    if template: args += ["--template", str(template)]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, **popen_kw())
    lines, token, grab, opened = [], [], False, False
    timer = threading.Timer(timeout, p.kill); timer.start()
    try:
        for line in p.stdout:
            line = line.rstrip("\n"); lines.append(line)
            if not opened:
                m = AUTH_URL.search(line)
                if m:
                    opened = True
                    if on_url: on_url(m.group(0))
            if line.startswith("<---"): grab = False
            if grab: token.append(line)
            if line.endswith("--->"): grab = True
        p.wait()
    finally:
        timer.cancel()
    tok = "\n".join(token).strip()
    if p.returncode != 0 or not tok:
        tail = "\n".join(l for l in lines[-6:] if l.strip())
        raise RcloneError(("the sign-in did not finish within %d s" % timeout) if p.returncode == -9 else (tail[-400:] or "rclone authorize failed"))
    return tok


def config_create(name, typ, params, timeout=120):
    """``rclone config create`` without questions: the backend's remaining questions (for drive: replace the token? shared
    drive?) are answered with their 'no' through rclone's --continue state machine, so no prompt and no browser ever appears."""
    _config(["create", name, typ], params, timeout)

def config_update(name, params, timeout=120):
    """``rclone config update``: only the given keys change, everything else the user set on that remote (root folder, shared
    drive, ...) stays; questions are answered like in ``config_create``."""
    _config(["update", name], params, timeout)

def _config(verb, params, timeout):
    base = [RCLONE, "config", *verb, *[f"{k}={v}" for k, v in params.items()], "--non-interactive"]
    state = None
    for _ in range(10):
        args = base if state is None else base + ["--continue", "--state", state, "--result", "false"]
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **popen_kw())
        if r.returncode != 0: raise RcloneError(r.stderr.strip()[-400:] or f"rclone config {verb[0]} {verb[1]} failed")
        try: out = json.loads(r.stdout or "{}")
        except ValueError: out = {}
        state = (out.get("State") or "") if isinstance(out, dict) else ""
        if not state: return
    raise RcloneError(f"rclone config {verb[0]} {verb[1]}: too many questions")


def fscache_clear():
    """Drop the daemon's cached backends. After a connection's config changed (a new Google key, a new token) every later call
    must open the remote afresh; otherwise the daemon keeps using the backend it built with the old settings."""
    if not DAEMON.running: return
    try: DAEMON.rc("fscache/clear")
    except RcloneError: pass


def start_in_background():
    """Start the daemon without blocking the caller (the app does this right after its server is up)."""
    threading.Thread(target=DAEMON.start, daemon=True, name="rclone-rcd").start()


def _run(args, timeout=None, **kw):
    if timeout is not None: kw["timeout"] = timeout
    r = subprocess.run([RCLONE, *args], capture_output=True, text=True, **{**popen_kw(), **kw})
    if r.returncode != 0: raise RcloneError(r.stderr.strip()[-300:] or f"rclone {args[0]} failed")
    return r.stdout


def _split(path):
    """("remote:", "folder/inside") for an rclone path, (path, "") for a local one (a Windows drive path "D:\\x" is local)."""
    m = re.match(r"^([A-Za-z0-9_-]+):(.*)$", path)
    if not m or not paths.is_remote_path(path): return path, ""
    return m.group(1) + ":", m.group(2).strip("/")

# rclone config keys a caller may set per call (``ls(config=...)``) and their command-line form for the subprocess fallback.
_CONFIG_FLAGS = {"ConnectTimeout": "--contimeout", "Timeout": "--timeout", "LowLevelRetries": "--low-level-retries", "Retries": "--retries"}


def ls(path, dirs_only=False, files_only=False, recurse=False, max_depth=None, no_modtime=False, timeout=None, cache=False, config=None):
    """Entries of ``path`` as ``rclone lsjson`` returns them (dicts with Path, Name, Size, IsDir, ModTime). Raises RcloneError.
    ``cache=True`` serves a repeat of the same listing within CACHE_TTL seconds without asking rclone (UI renders only)."""
    if cache:
        key = ("ls", path, dirs_only, files_only, recurse, max_depth, no_modtime)
        return _cached(key, lambda: ls(path, dirs_only, files_only, recurse, max_depth, no_modtime, timeout, config=config))
    if DAEMON.available():
        # One backend per connection: fs is the remote root ("nas:") and the folder is the remote part, so the daemon reuses
        # its SMB session and Drive's directory cache for every folder (fs = full path opened a new backend, and on SMB a new
        # session, per listed folder: minutes for a few share listings). Paths come back relative to the root, so the folder
        # prefix is stripped: every Path is relative to the listed folder exactly like ``rclone lsjson <path>``, which
        # list_destination's skip check relies on. A local path has no remote root and stays the fs itself.
        fs, rel = _split(path)
        opt = {"noMimeType": True, "dirsOnly": bool(dirs_only), "filesOnly": bool(files_only), "recurse": bool(recurse), "noModTime": bool(no_modtime)}
        cfg = dict(config or {})
        if max_depth is not None: cfg["MaxDepth"] = int(max_depth)
        params = {"fs": fs, "remote": rel, "opt": opt}
        if cfg: params["_config"] = cfg
        try:
            out = DAEMON.rc("operations/list", timeout=timeout, **params).get("list") or []
            if rel and not _strip_prefix(out, rel):
                # an entry did not carry the folder prefix (a case or separator difference on a case-insensitive SMB share):
                # list the folder as its own fs, the old way, so every Path is relative to it for sure
                params["fs"], params["remote"] = path, ""
                out = DAEMON.rc("operations/list", timeout=timeout, **params).get("list") or []
            return out
        except DaemonGone: pass                                 # killed or crashed daemon: plain rclone below
    args = ["lsjson", "--no-mimetype"]
    for k, v in (config or {}).items():
        if k in _CONFIG_FLAGS: args += [_CONFIG_FLAGS[k], str(v)]
    if no_modtime: args.append("--no-modtime")
    if dirs_only: args.append("--dirs-only")
    if files_only: args.append("--files-only")
    if recurse: args.append("-R")
    if max_depth is not None: args += ["--max-depth", str(max_depth)]
    return json.loads(_run(args + [path], timeout=timeout) or "[]")


def stat(path, timeout=60):
    """The lsjson entry of one file (Path, Name, Size, IsDir), or None when nothing is there. Raises RcloneError otherwise."""
    if DAEMON.available():
        fs, rel = _split(path)
        try: return DAEMON.rc("operations/stat", timeout=timeout, fs=fs, remote=rel, opt={"noModTime": True, "noMimeType": True}).get("item")
        except DaemonGone: pass
    try: return json.loads(_run(["lsjson", "--stat", "--no-mimetype", "--no-modtime", path], timeout=timeout) or "null")
    except RcloneError as e:
        if "not found" in str(e).lower(): return None
        raise


def move_no_replace(src, dst, timeout=300):
    """Rename src to dst on the same remote (server-side on SMB and WebDAV), never replacing a file at dst: True when moved,
    False when dst already existed (dst untouched; src is left for the caller). Raises RcloneError."""
    if stat(dst, timeout=timeout) is not None: return False
    if DAEMON.available():
        sfs, srel = _split(src); dfs, drel = _split(dst)
        try:
            DAEMON.rc("operations/movefile", timeout=timeout, srcFs=sfs, srcRemote=srel, dstFs=dfs, dstRemote=drel, _config={"IgnoreExisting": True})
            return True
        except DaemonGone: pass
    _run(["moveto", "--ignore-existing", src, dst], timeout=timeout)
    return True


def deletefile(path, timeout=120):
    """Delete one file (callers only pass names the app itself created)."""
    if DAEMON.available():
        fs, rel = _split(path)
        try: DAEMON.rc("operations/deletefile", timeout=timeout, fs=fs, remote=rel); return
        except DaemonGone: pass
    _run(["deletefile", path], timeout=timeout)


def _strip_prefix(entries, rel):
    """Strip "<rel>/" from every entry's Path (compared without case and with separators normalised, as a case-insensitive
    SMB share may answer in another spelling). False when an entry does not start with it."""
    pre = rel.replace("\\", "/").strip("/").lower() + "/"
    for e in entries:
        p = e.get("Path", "")
        if p.replace("\\", "/").lower().startswith(pre): e["Path"] = p[len(pre):]
        else: return False
    return True

def smb_host(raw):
    """The NAS host from what a person types: "nas", "192.0.2.10", or the Windows / Finder forms "\\\\nas", "\\\\nas\\share",
    "//nas/share", "smb://nas/share" (Windows users copy the address from File Explorer). rclone's smb backend wants the bare host;
    the share is picked afterwards from the listing. "" when nothing usable is left."""
    h = (raw or "").strip()
    if h.lower().startswith("smb://"): h = h[6:]
    h = h.lstrip("\\/")
    for sep in ("\\", "/"): h = h.split(sep, 1)[0]
    if "@" in h: h = h.rsplit("@", 1)[1]                  # smb://user@nas: the user has its own field
    return h.strip()

def smb_address(raw):
    """(host, port or None) from what a person types: ``smb_host`` plus a port ("nas:445", "[fe80::1]:445") split off, because
    rclone's smb backend takes the port as its own option. A bare IPv6 address ("fe80::1") is a host without a port."""
    h = smb_host(raw)
    if h.startswith("["):                                   # [v6] or [v6]:port
        host, _, rest = h[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        return host, (port if port.isdigit() else None)
    if h.count(":") == 1:
        host, port = h.split(":")
        return host, (port if port.isdigit() else None)
    return h, None

def smb_host_hint(host):
    """A sentence when ``host`` is a public (globally routable) IPv4 address, which a NAS at home, in the office or behind a
    VPN such as Tailscale never has: usually a typo (192.158.x.x for 192.168.x.x). '' otherwise (names, IPv6, local addresses)."""
    import ipaddress
    try: ip = ipaddress.IPv4Address(host)
    except ValueError: return ""
    if not ip.is_global: return ""                          # local, link-local, and VPN/CGNAT (Tailscale 100.64/10) addresses
    a, b, c, d = str(ip).split(".")
    guess = f" Did you mean {'.'.join(['192', '168', c, d])}?" if a == "192" and b != "168" else ""
    return f"{host} is not an address on a local network; a NAS normally has one starting with 192.168., 10. or 172.16-31. (or a VPN address such as Tailscale's 100.x).{guess}"

# rclone / go-smb2 / Windows socket wording -> what a person can act on. First match wins.
_SMB_ERRORS = [
    (("no such host", "server misbehaving", "getaddrinfo", "name resolution"),
     "The name {host} could not be found on this network. Use the NAS's IP address instead (DSM → Control Panel → Network)."),
    (("connection refused", "actively refused"),
     "{host} refused the SMB connection. Turn on SMB in DSM (Control Panel → File Services → SMB) and check the port."),
    (("logon failure", "status_logon_failure", "account restriction", "account_disabled", "password_expired", "wrong password"),
     "The NAS rejected the user name or password for {user}."),
    (("access denied", "status_access_denied"),
     "The NAS refused access for {user}. Check the user's permissions in DSM (Control Panel → User & Group)."),
    (("network is unreachable", "no route to host", "host is unreachable", "unreachable network"),
     "This computer has no network route to {host}. Check that it is on the same network as the NAS (VPN, VM network mode)."),
    (("i/o timeout", "deadline exceeded", "timed out", "no answer from rclone within", "timeout"),
     "{host} did not answer on the SMB port ({port}) within 20 seconds. Check the address, that the NAS is on, and that SMB is enabled in DSM."),
    (("negotiate", "dialect", "smb1"),
     "{host} answered but does not speak SMB2/3. In DSM set the minimum SMB protocol to SMB2 (File Services → SMB → Advanced)."),
]

def smb_error_message(raw, host, user="", port=None):
    """A plain message for a failed SMB connect, after the typo hint (``smb_host_hint``) and rclone's own text as
    "Details:" for support. Unknown errors keep rclone's text as the message."""
    low = (raw or "").lower()
    msg = next((m for keys, m in _SMB_ERRORS if any(k in low for k in keys)), "")
    msg = msg.format(host=host, user=user or "this user", port=port or 445) if msg else f"Could not connect to {host}."
    hint = smb_host_hint(host)
    detail = (raw or "").strip()[-300:]
    return " ".join(x for x in (hint, msg, f"Details: {detail}" if detail else "") if x)     # a likely typo first

def with_elapsed(job, message, fn, every=1.0):
    """Run ``fn()`` while ``job["message"]`` shows ``message`` plus the seconds so far ("Listing the shares on nas… 7 s"), so a
    slow first rclone start (a clean Windows install scans the exe) or a slow NAS never looks like a hang. Returns fn's result."""
    t0 = time.time(); done = threading.Event()
    def tick():
        while not done.wait(every): job["message"] = f"{message} {int(time.time() - t0)} s"
    job["message"] = message
    threading.Thread(target=tick, daemon=True).start()
    try: return fn()
    finally: done.set()

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


def remote_info(cache=False):
    """{name_with_colon: {"type": ..., "own_key": bool}} of the configured remotes; own_key is true when the remote carries its
    own OAuth client id (a Google key of the user's). Secrets and tokens are never returned."""
    if cache: return _cached(("remote_info",), lambda: remote_info())
    if DAEMON.available():
        try:
            out = {}
            for n in DAEMON.rc("config/listremotes").get("remotes") or []:
                try: cfg = DAEMON.rc("config/get", name=n) or {}
                except DaemonGone: raise
                except RcloneError: cfg = {}
                out[n + ":"] = _info(cfg)
            return out
        except DaemonGone: pass
    try: dump = json.loads(_run(["config", "dump"]) or "{}")
    except ValueError: dump = {}
    return {n + ":": _info(c or {}) for n, c in dump.items()}


def drive_key(name):
    """(client_id, client_secret) of a Google Drive connection's own key, or ("", "") when it has none. Server side only: used to
    sign in again with the same key (Settings → Google Drive → Sign in again); never sent to the UI."""
    name = name.rstrip(":")
    if DAEMON.available():
        try: cfg = DAEMON.rc("config/get", name=name) or {}
        except (DaemonGone, RcloneError): cfg = None
        if cfg is not None: return cfg.get("client_id", ""), cfg.get("client_secret", "")
    try: cfg = (json.loads(_run(["config", "dump"]) or "{}") or {}).get(name) or {}
    except ValueError: cfg = {}
    return cfg.get("client_id", ""), cfg.get("client_secret", "")


def _info(cfg):
    """What the UI may know about one connection: its type, whether it has an own Google key, and for an SMB connection the
    address and user it connects with (for the Edit form). Never a password, secret or token."""
    out = {"type": cfg.get("type", ""), "own_key": bool(cfg.get("client_id"))}
    if out["type"] == "smb": out.update(host=cfg.get("host", ""), port=cfg.get("port", ""), user=cfg.get("user", ""))
    return out


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
    _run(["copyto", src, str(dst), *transfer.COMMON], timeout=timeout)


def copyfile(src, dst, progress=None, cancelled=None, poll=0.5, timeout=None):
    """Download one remote file ``src`` ("remote:path/file") to the local path ``dst`` through the daemon when it runs
    (``operations/copyfile`` as an async job; ``core/stats`` of its group gives bytes, speed and rclone's retry state, reported
    through ``progress(bytes_done, speed_bytes_per_s, note)`` where note is '' or 'rate-limited, retrying'), otherwise as one
    rclone process. ``cancelled()`` true stops the job and raises Cancelled. Raises RcloneError when rclone fails."""
    dst = str(dst)
    if not DAEMON.available():
        return _fallback_copy(src, dst, timeout)
    if is_remote_path(src): fs, remote = src.split(":", 1)[0] + ":", src.split(":", 1)[1]
    else: fs, remote = os.path.split(src)                        # a local file: its folder is the fs, the name the remote part
    dst_dir, dst_name = os.path.split(dst)
    group = f"job/{secrets.token_hex(4)}"
    try:
        started = DAEMON.rc("operations/copyfile", srcFs=fs, srcRemote=remote, dstFs=dst_dir or ".", dstRemote=dst_name, _async=True, _group=group)
    except DaemonGone:
        return _fallback_copy(src, dst, timeout)
    jobid = started.get("jobid")
    t0 = time.time()
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
                raise Cancelled("cancelled")
            if timeout and time.time() - t0 > timeout:
                try: DAEMON.rc("job/stop", jobid=jobid)
                except RcloneError: pass
                raise RcloneError(f"copy of {src} timed out after {timeout:.0f} s")
            if progress is not None:
                try: stats = DAEMON.rc("core/stats", group=group)
                except RcloneError: stats = {}
                done = int(stats.get("bytes") or 0)              # counts the in-flight bytes already; transferring[].bytes would double them
                speed = float(stats.get("speed") or 0.0)
                err = str(stats.get("lastError") or "")
                note = "rate-limited, retrying" if (stats.get("retryError") or any(w in err for w in RATE_LIMIT_WORDS)) and err else ""
                progress(done, speed, note)
            time.sleep(poll)
    finally:
        try: DAEMON.rc("core/stats-delete", group=group)
        except RcloneError: pass
    if progress is not None: progress(os.path.getsize(dst) if os.path.exists(dst) else 0, 0.0, "")
