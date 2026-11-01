"""Updates: is a newer release published, and fetching, verifying and installing it.

Security model (docs/HANDOVER.md, "Release signing"):
* The release workflow signs the checksum lists (``SHA256SUMS`` for the disk images, ``CallidoRestore-x64.msi.sha256`` for the
  installer) with an Ed25519 key that exists only as the GitHub Actions secret ``RELEASE_SIGNING_KEY``; the signatures are
  attached as ``<list>.sig``. The app carries the public key(s) in ``about.UPDATE_PUBLIC_KEYS``.
* An update is installed only when: the list's signature verifies under one of those keys (``ed25519.verify``), the downloaded
  file's SHA-256 matches its line in the signed list, the release tag is strictly newer than ``about.VERSION`` (no downgrade or
  replay of an old signed release), and the file name is this platform's. Anything else stops the update and deletes the
  download. Without a public key in the app, updates are only offered as a link to the release page.
* HTTPS only, certificates verified (Python's default context), to ``api.github.com`` for the release and to GitHub's
  release-asset hosts for the files; the repository is ``about.REPO``.

``status()`` answers at once from a state file and checks in the background at most once a day; network errors are silent. The
setting (``set_mode``) is ``"mention"`` (the default: check daily, show it, install on a click), ``"auto"`` (also install a
newer signed release by itself, through the same checks, once no restore or preparation runs: ``auto_due`` plus the server's
``auto_update_tick``) or ``"off"`` (no check; also ``HBK_NO_UPDATE_CHECK=1``). An automatic install that fails is not
retried for that release (``auto_failed``); it stays offered for a click. ``install(job, exit_app)`` downloads, verifies and
installs: on macOS it replaces the app bundle in place (files this app downloads carry no quarantine flag) and restarts through
a helper that waits for this process to end; when the bundle is not writable or anything fails it opens the verified disk image
for a manual drag. On Windows it hands over to ``update_windows`` (msiexec upgrade in place, then restart).
"""
import hashlib, json, os, platform, re, shutil, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request
from callido_restore import ed25519, paths
from callido_restore.about import REPO, UPDATE_PUBLIC_KEYS, VERSION

API = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 24 * 3600
ASSET_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
UA = {"User-Agent": f"CallidoRestore/{VERSION}"}
_lock = threading.Lock(); _checking = [False]


def log(msg):
    """One line in app.log (stdout of the frozen app) for every update step."""
    try: print(f"update: {msg}", flush=True)
    except Exception: pass


def _state_file(): return paths.CONF_DIR / "update.json"


def _read():
    try: return json.loads(_state_file().read_text())
    except (OSError, ValueError): return {}


def _write(d):
    try:
        paths.CONF_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _state_file().with_suffix(".tmp"); tmp.write_text(json.dumps(d)); os.replace(tmp, _state_file())
    except OSError: pass


# ----------------------------------------------------------------------------- versions and settings
def version_tuple(v):
    """'v1.2.10' -> (1, 2, 10, 1); a suffix after the numbers (a pre-release) sorts before the plain version."""
    m = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$", (v or "").strip())
    if not m: return (0, 0, 0, 0)
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), 0 if m.group(4) else 1)


def newer(tag, current=VERSION): return version_tuple(tag) > version_tuple(current)


MODES = ("auto", "mention", "off")


def mode():
    """The update setting; a state file from before the setting (only ``check``) reads as mention or off."""
    if os.environ.get("HBK_NO_UPDATE_CHECK") == "1": return "off"
    d = _read()
    if d.get("mode") in MODES: return d["mode"]
    return "mention" if d.get("check", True) else "off"


def check_enabled(): return mode() != "off"


def set_mode(m):
    if m not in MODES: raise ValueError(f"unknown update setting {m!r} (one of {', '.join(MODES)})")
    d = _read(); d["mode"] = m; d["check"] = m != "off"; _write(d); log(f"setting: {m}"); return status()


def set_check(on):
    """The older on/off switch: on keeps an automatic setting, off turns checking off."""
    return set_mode(("auto" if mode() == "auto" else "mention") if on else "off")


AUTO_WARNING = 60                                                  # seconds between "ready" and an automatic restart
_later = set()                                                     # versions put off with "Later" in this app run


class Later(Exception):
    """The update job was cancelled ("Later") during an automatic install: not a failure."""


class Postponed(RuntimeError):
    """An automatic install waits: a restore or preparation started while the update downloaded (the download is kept)."""


def auto_due(st=None):
    """An automatic install should start now: the setting is auto, a newer signed release is installable, and it has not
    failed automatically before. Whether a job runs is the caller's (the server's) to check."""
    st = st or status()
    return (st["mode"] == "auto" and st["installable"] and _read().get("auto_failed") != st["latest"]
            and st["latest"] not in _later)


def platform_asset():
    """This computer's release file: the MSI on Windows, the DMG for this Mac's architecture; None elsewhere."""
    if paths.WINDOWS: return "CallidoRestore-x64.msi"
    if sys.platform == "darwin": return "CallidoRestore-arm64.dmg" if platform.machine() == "arm64" else "CallidoRestore-amd64.dmg"
    return None


def checksum_list(asset):
    """The signed checksum list that covers ``asset``."""
    return "CallidoRestore-x64.msi.sha256" if asset.endswith(".msi") else "SHA256SUMS"


# ----------------------------------------------------------------------------- network (HTTPS, GitHub only)
def _check_url(url, hosts):
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname not in hosts: raise RuntimeError(f"refusing to fetch {u.scheme}://{u.hostname}: not a GitHub release host")


def _open(url, hosts, timeout):
    _check_url(url, hosts)
    r = urllib.request.urlopen(urllib.request.Request(url, headers={**UA, "Accept": "application/octet-stream"}), timeout=timeout)
    _check_url(r.geturl(), hosts)                                     # redirects must stay on GitHub too
    return r


def latest_release(timeout=10):
    """The latest published release as GitHub returns it (tag_name, name, body, html_url, assets[name, browser_download_url])."""
    _check_url(API, {"api.github.com"})
    req = urllib.request.Request(API, headers={**UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read())


def _asset(rel, name):
    for a in rel.get("assets") or []:
        if a.get("name") == name:
            url = a.get("browser_download_url") or ""
            if not url.startswith(f"https://github.com/{REPO}/releases/download/"): raise RuntimeError(f"{name}: not a release file of {REPO}")
            return url, int(a.get("size") or 0)
    raise RuntimeError(f"the release has no {name}")


def _fetch_small(rel, name, timeout=30):
    url, _ = _asset(rel, name)
    with _open(url, ASSET_HOSTS, timeout) as r: return r.read(1 << 20)


# ----------------------------------------------------------------------------- the check
def _check_now():
    try:
        rel = latest_release()
        d = _read(); d.update(checked=time.time(), release={k: rel.get(k) for k in ("tag_name", "name", "body", "html_url", "assets")}, error=None)
        _write(d); log(f"latest release {rel.get('tag_name')} (running {VERSION})")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError, RuntimeError) as e:
        d = _read(); d.update(checked=time.time(), error=str(e)[:200]); _write(d); log(f"check failed: {str(e)[:200]}")
    finally:
        _checking[0] = False


def status(force=False):
    """What the UI shows. Never waits for the network: a due check (or ``force``) runs in the background."""
    d = _read(); due = time.time() - d.get("checked", 0) > CHECK_EVERY
    with _lock:
        if (force or (due and check_enabled())) and not _checking[0]:
            _checking[0] = True; threading.Thread(target=_check_now, daemon=True, name="update-check").start()
    rel = d.get("release") or {}; tag = rel.get("tag_name") or ""; avail = bool(tag) and newer(tag); asset = platform_asset()
    return {"current": VERSION, "check": check_enabled(), "mode": mode(), "auto": mode() == "auto", "checking": _checking[0], "checked": d.get("checked"),
            "available": avail, "latest": tag.lstrip("v"), "name": rel.get("name") or tag, "notes": rel.get("body") or "",
            "url": rel.get("html_url") or f"https://github.com/{REPO}/releases/latest", "asset": asset,
            "signed_updates": bool(UPDATE_PUBLIC_KEYS),
            # Windows: msiexec replaces the installed app, which exists only in the packaged app (from source: the release page)
            "installable": avail and asset is not None and bool(UPDATE_PUBLIC_KEYS) and (not paths.WINDOWS or _windows_ready())}


def _windows_ready():
    """The Windows install step (update_windows, hbk-5) is present and this is the packaged app."""
    if not getattr(sys, "frozen", False): return False
    try: import callido_restore.update_windows  # noqa: F401
    except ImportError: return False
    return True


# ----------------------------------------------------------------------------- verification
def signed_message(tag, text):
    """What the release workflow signs: the release tag bound to the checksum list, so a validly signed list of an old release
    cannot be re-published under a newer tag (a downgrade by someone who can edit releases but has no key)."""
    return f"callido-restore {tag}\n".encode() + text


def verify_signed(text, sig, keys=None):
    """True when ``sig`` is a valid Ed25519 signature of ``text`` (bytes) under one of the app's release keys."""
    for k in (UPDATE_PUBLIC_KEYS if keys is None else keys):
        try:
            if ed25519.verify(bytes.fromhex(k), text, sig): return True
        except ValueError: continue
    return False


def expected_sha(text, name):
    """The SHA-256 of ``name`` in a checksum list ("<hex>  <name>" lines; a single bare hex for a one-file list)."""
    lines = [l.strip().split() for l in (text or "").splitlines() if l.strip()]
    for parts in lines:
        if len(parts) >= 2 and parts[-1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]): return parts[0].lower()
    if len(lines) == 1 and len(lines[0]) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", lines[0][0]): return lines[0][0].lower()
    return None


def verified_sha(rel, asset, keys=None):
    """The SHA-256 of ``asset`` from the release's signed checksum list; RuntimeError when the list or its signature is missing
    or does not verify, or when it does not list the asset. Shared by the macOS path and update_windows."""
    if not (UPDATE_PUBLIC_KEYS if keys is None else keys): raise RuntimeError("this version has no release key; install the update from the release page")
    lst = checksum_list(asset); tag = rel.get("tag_name") or ""
    text, sig = _fetch_small(rel, lst), _fetch_small(rel, lst + ".sig")
    if not verify_signed(signed_message(tag, text), sig, keys):
        raise RuntimeError(f"the signature of {lst} does not verify for {tag}: the release was not signed with the Callido release key (or not for this version); nothing was installed")
    want = expected_sha(text.decode("utf-8", "replace"), asset)
    if not want: raise RuntimeError(f"{lst} lists no checksum for {asset}")
    log(f"{lst}: signature verified, {asset} sha256 {want[:16]}…")
    return want


def sha256_of(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()


def download_verified(rel, asset, progress=None, cancelled=None, keys=None):
    """``asset`` of the release in cache/updates/<version>/, kept only when its SHA-256 matches the signed list."""
    tag = rel.get("tag_name") or ""
    if not newer(tag): raise RuntimeError(f"{tag or 'the release'} is not newer than {VERSION}; nothing to install")
    if asset != platform_asset(): raise RuntimeError(f"{asset} is not this computer's installer ({platform_asset()})")
    want = verified_sha(rel, asset, keys)
    folder = paths.CACHE_DIR / "updates" / tag.lstrip("v"); folder.mkdir(parents=True, exist_ok=True)
    dest = folder / asset
    if dest.is_file() and sha256_of(dest) == want: log(f"{asset} already downloaded and verified"); return dest
    url, size = _asset(rel, asset); tmp = dest.with_name(dest.name + ".part")
    try:
        with _open(url, ASSET_HOSTS, 60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or size or 0); done = 0
            while True:
                if cancelled and cancelled(): raise RuntimeError("update download stopped")
                b = r.read(1 << 20)
                if not b: break
                f.write(b); done += len(b)
                if progress: progress(done, total)
        got = sha256_of(tmp)
        if got != want: raise RuntimeError(f"the downloaded {asset} does not match the signed checksum; it was deleted and nothing was installed")
        os.replace(tmp, dest); log(f"{asset} downloaded and verified ({sha256_of(dest)[:16]}…)")
        return dest
    except BaseException:
        for p in (tmp, dest):
            try: p.unlink()
            except OSError: pass
        raise


# ----------------------------------------------------------------------------- install
def current_bundle():
    """The .app this process runs from (frozen macOS app), else None."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False): return None
    p = os.path.realpath(sys.executable)
    i = p.find(".app/Contents/MacOS/")
    return p[:i + 4] if i > 0 else None


def mac_swap_script(app, new_app, pid):
    """The helper that waits for this process to end, swaps the bundles and starts the new app (a rename on one volume)."""
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"
    old = app + ".old"
    # mv onto an existing directory would move the bundle INTO it, so every target is checked to be absent first; any failure
    # leaves the old app where it was (or puts it back) and opens it.
    return (f"while kill -0 {int(pid)} 2>/dev/null; do sleep 0.3; done; "
            f"rm -rf {q(old)}; "
            f"if [ ! -e {q(old)} ] && mv {q(app)} {q(old)}; then "
            f"if [ ! -e {q(app)} ] && mv {q(new_app)} {q(app)}; then rm -rf {q(old)}; "
            f"else rm -rf {q(app)}; [ ! -e {q(app)} ] && mv {q(old)} {q(app)}; fi; fi; "
            f"open {q(app)}")


def install_mac(dmg, exit_app, open_fallback=True):
    """Replace this app with the verified disk image's app and restart; returns {'opened': dmg} when it falls back to a
    manual drag (not the frozen app, the bundle's folder not writable, or a step failed). Without ``open_fallback`` (an
    automatic install) the disk image is not opened unasked: {'manual': dmg}."""
    def fallback():
        if not open_fallback: return {"manual": str(dmg)}
        subprocess.run(["open", str(dmg)], capture_output=True); return {"opened": str(dmg)}
    app = current_bundle()
    if not app or not os.access(os.path.dirname(app), os.W_OK):
        log("in-place update not possible here; " + ("opening the verified disk image instead" if open_fallback else "leaving it for a manual install"))
        return fallback()
    mnt = tempfile.mkdtemp(prefix="callido-update-"); new_app = os.path.join(os.path.dirname(app), "." + os.path.basename(app) + ".new")
    try:
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mnt, str(dmg)], check=True, capture_output=True, timeout=120)
        src = os.path.join(mnt, os.path.basename(app))
        if not os.path.isdir(src): raise RuntimeError(f"the disk image has no {os.path.basename(app)}")
        shutil.rmtree(new_app, ignore_errors=True)
        subprocess.run(["ditto", src, new_app], check=True, capture_output=True, timeout=600)
    except (subprocess.SubprocessError, OSError, RuntimeError) as e:
        log(f"in-place update failed ({str(e)[:200]})")
        shutil.rmtree(new_app, ignore_errors=True); return fallback()
    finally:
        subprocess.run(["hdiutil", "detach", mnt], capture_output=True); shutil.rmtree(mnt, ignore_errors=True)
    log(f"new app staged next to {app}; restarting")
    subprocess.Popen(["/bin/sh", "-c", mac_swap_script(app, new_app, os.getpid())], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    exit_app(0)
    return {"restarting": app}


def install(job, exit_app, auto=False, may_install=None):
    """A job: fetch, verify and install the known newer release on this computer. ``auto``: started by the automatic setting,
    without a click; ``may_install()`` is asked again right before the app closes (Postponed when a job started meanwhile).
    An automatic install that fails is remembered, so it is not retried for that release. Before an automatic restart the job
    counts down ``AUTO_WARNING`` seconds (``restart_at``, ``version`` on the job); cancelling the job then, or during the
    download, is "Later": no automatic install of that version until the app starts again (the verified download is kept,
    and a click in About still installs it)."""
    if not auto: return _install(job, exit_app, False, may_install)
    tag = ((_read().get("release") or {}).get("tag_name") or "").lstrip("v")
    try:
        out = _install(job, exit_app, True, may_install)
    except Postponed as e:
        log(str(e)); raise
    except Later:
        _later.add(tag); job["message"] = f"version {tag} installs the next time the app starts, or now from About"
        log(f"automatic install of {tag} put off until the next start"); return {"later": tag}
    except RuntimeError:
        if job.get("cancel"):                                         # Later during the download
            _later.add(tag); log(f"automatic install of {tag} put off during the download"); return {"later": tag}
        d = _read(); d["auto_failed"] = tag; _write(d); raise
    except BaseException as e:
        d = _read(); d["auto_failed"] = tag; _write(d); log(f"automatic install of {tag} failed ({str(e)[:200]}); offered for a click instead"); raise
    if "manual" in out:
        d = _read(); d["auto_failed"] = tag; _write(d)
    return out


def _install(job, exit_app, auto, may_install):
    def check_may():
        if may_install and not may_install(): raise Postponed("update downloaded; it installs when the restore or preparation has finished")
    def countdown(version):
        """The automatic path's warning: the UI shows "restarts in N s · Later" from ``restart_at``."""
        if not auto: return
        job["version"] = version; job["restart_at"] = time.time() + AUTO_WARNING; job["progress"] = 1.0
        while (left := job["restart_at"] - time.time()) > 0:
            if job.get("cancel"): raise Later()
            check_may()
            job["message"] = f"Version {version} is ready: the app restarts in {int(left + 0.999)} s"
            time.sleep(min(0.5, left))
        if job.get("cancel"): raise Later()
        check_may()
    rel = _read().get("release")
    if not rel or not newer(rel.get("tag_name", "")): raise RuntimeError("no newer version is known; check for updates first")
    asset = platform_asset()
    if not asset: raise RuntimeError("updates are installed from the release page on this system")
    prog = lambda done, total: (job.__setitem__("done_bytes", done), job.__setitem__("total_bytes", total or None),
                                job.__setitem__("progress", done / total if total else 0.0))
    cancelled = lambda: bool(job.get("cancel"))
    job["message"] = f"downloading version {rel['tag_name'].lstrip('v')}"; log(f"install {rel['tag_name']} ({asset}){' automatically' if auto else ''}")
    if paths.WINDOWS:
        from callido_restore import update_windows                    # hbk-5: msiexec upgrade in place and restart
        msi = update_windows.download(rel, progress=prog, cancelled=cancelled)
        check_may(); countdown(rel["tag_name"].lstrip("v"))
        job["message"] = "installing; the app closes and starts again"
        update_windows.install_and_relaunch(msi)
        return {"installing": str(msi)}
    dmg = download_verified(rel, asset, progress=prog, cancelled=cancelled)
    check_may(); countdown(rel["tag_name"].lstrip("v"))
    job["message"] = "installing; the app closes and starts again"
    out = install_mac(dmg, exit_app, open_fallback=not auto)
    if "manual" in out: job["message"] = "this app cannot replace itself here: install the new version with the button in About"
    if "opened" in out: job["message"] = "opened the new version: quit this app, drag Callido Restore to Applications, then open it"
    return out
