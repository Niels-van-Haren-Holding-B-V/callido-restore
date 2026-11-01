"""Windows step of the auto-update: fetch the release's MSI into the app cache, check it against the release's signed checksum, then
hand over to a detached helper that waits for the app to exit, runs msiexec (the MSI has a fixed UpgradeCode, so a newer
version upgrades the installed one in place; msiexec asks for the UAC elevation itself) and starts the app again.

The release check, the signature check, the UI and the "not while a restore runs" rule live in ``callido_restore/update.py``
(hbk-2); this module adds the MSI install in place and the restart."""
import os, subprocess, sys, time
from pathlib import Path
from callido_restore import oscompat, paths

MSI_NAME = "CallidoRestore-x64.msi"
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class UpdateError(RuntimeError):
    pass


def download(release, progress=None, cancelled=None):
    """Path of the release's MSI in cache/updates/<version>/, kept only when its SHA-256 matches the release's *signed*
    checksum (``CallidoRestore-x64.msi.sha256`` + ``.sig``, Ed25519 under ``about.UPDATE_PUBLIC_KEYS``). All checks (newer
    version, this platform's asset, signature, GitHub hosts, hash) are the shared ``update.download_verified``; a failure
    raises RuntimeError with a readable message and leaves no file behind. ``progress(done, total)``; ``cancelled()``."""
    from callido_restore import update
    return update.download_verified(release, MSI_NAME, progress=progress, cancelled=cancelled)


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
