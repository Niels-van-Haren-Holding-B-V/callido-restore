"""Windows step of the auto-update: fetch the release's MSI into the app cache, check it against the release's .sha256, then
hand over to a detached helper that waits for the app to exit, runs msiexec (the MSI has a fixed UpgradeCode, so a newer
version upgrades the installed one in place; msiexec asks for the UAC elevation itself) and starts the app again.

The release check, the UI and the "not while a restore runs" rule live in the shared update code; this module only needs
the release JSON from https://api.github.com/repos/<owner>/<repo>/releases/<...> (``tag_name`` and ``assets``)."""
import hashlib, os, re, subprocess, sys, time, urllib.request
from pathlib import Path
from callido_restore import oscompat, paths

MSI_NAME = "CallidoRestore-x64.msi"
SUM_NAME = MSI_NAME + ".sha256"
ALLOWED_HOSTS = ("https://github.com/", "https://objects.githubusercontent.com/", "https://release-assets.githubusercontent.com/")
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class UpdateError(RuntimeError):
    pass


def _version(release):
    v = (release.get("tag_name") or "").lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", v): raise UpdateError(f"release has no x.y.z version: {release.get('tag_name')!r}")
    return v


def _asset_url(release, name):
    for a in release.get("assets") or []:
        if a.get("name") == name:
            url = a.get("browser_download_url") or ""
            if not url.startswith(ALLOWED_HOSTS[0]): raise UpdateError(f"{name} is not served from github.com: {url}")
            return url, int(a.get("size") or 0)
    raise UpdateError(f"the release has no {name}")


def _open(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "callido-restore-updater"})
    r = urllib.request.urlopen(req, timeout=timeout)
    if not r.geturl().startswith(ALLOWED_HOSTS):            # redirects from github.com go to GitHub's own asset hosts only
        r.close(); raise UpdateError(f"unexpected download host: {r.geturl()}")
    return r


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()


def update_dir(version):
    return paths.CACHE_DIR / "updates" / version


def download(release, progress=None, cancelled=None):
    """Path of the release's MSI in the app cache, its sha256 checked against the release's .sha256 file. A file already
    there with the right sum is reused. ``progress(done_bytes, total_bytes)``; ``cancelled()`` true stops (UpdateError).
    A file whose sum does not match is deleted and never returned."""
    version = _version(release)
    msi_url, size = _asset_url(release, MSI_NAME)
    sum_url, _ = _asset_url(release, SUM_NAME)
    with _open(sum_url) as r: text = r.read(4096).decode("utf-8", "replace")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m: raise UpdateError(f"{SUM_NAME} holds no sha256")
    want = m.group(1).lower()
    d = update_dir(version); d.mkdir(parents=True, exist_ok=True)
    out = d / MSI_NAME
    if out.is_file() and _sha256_of(out) == want: return out
    part = d / (MSI_NAME + ".part")
    h = hashlib.sha256(); done = 0
    try:
        with _open(msi_url, timeout=120) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or size or 0)
            while True:
                if cancelled and cancelled(): raise UpdateError("update download cancelled")
                b = r.read(1 << 20)
                if not b: break
                f.write(b); h.update(b); done += len(b)
                if progress: progress(done, total)
        if h.hexdigest() != want: raise UpdateError(f"{MSI_NAME} does not match its published sha256; not installing it")
        os.replace(part, out)
        return out
    finally:
        if part.exists(): part.unlink()


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def helper_command(msi, app_exe, pid, log):
    """The detached helper: wait for the app (pid) to exit, install the MSI with a progress bar and no questions besides
    UAC, then start the app again. If the install fails or is declined, the old version is still installed and starts."""
    script = (
        f"Wait-Process -Id {int(pid)} -Timeout 60 -ErrorAction SilentlyContinue; "
        f"$p = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i', {_ps_quote(msi)}, '/passive', '/norestart', '/l*v', {_ps_quote(log)}) "
        f"-Wait -PassThru; "
        f"Add-Content -LiteralPath {_ps_quote(str(log) + '.result')} -Value (\"msiexec exit \" + $p.ExitCode); "
        f"Start-Process -FilePath {_ps_quote(app_exe)}"
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script]


def install_and_relaunch(msi, exit_app=None):
    """Start the helper and quit the app (``exit_app(0)``, by default the server's ``_exit_app``, which also stops rclone, so
    no file of the installed app is in use when msiexec replaces it). Only from the installed, packaged app."""
    if not oscompat.WINDOWS: raise UpdateError("the MSI update runs on Windows only")
    if not getattr(sys, "frozen", False): raise UpdateError("updates install only into the packaged app (this runs from source)")
    msi = Path(msi)
    if not msi.is_file(): raise UpdateError(f"missing {msi}")
    log = paths.CONF_DIR / f"update-{msi.parent.name}.log"
    cmd = helper_command(msi, sys.executable, os.getpid(), log)
    subprocess.Popen(cmd, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if exit_app is None:
        from callido_restore.server.api import _exit_app as exit_app
    time.sleep(0.5)                                          # let the helper start before this process goes away
    exit_app(0)
