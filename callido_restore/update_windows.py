"""Windows step of the auto-update: fetch the release's MSI into the app cache, check it against the release's signed checksum, then
hand over to a detached helper that waits for the app to exit, runs msiexec (the MSI has a fixed UpgradeCode, so a newer
version upgrades the installed one in place; msiexec asks for the UAC elevation itself) and starts the app again.

The release check, the signature check, the UI and the "not while a restore runs" rule live in ``callido_restore/update.py``
(hbk-2); this module adds the MSI install in place and the restart."""
import hashlib, os, re, subprocess, sys, time
from pathlib import Path
from callido_restore import oscompat, paths

MSI_NAME = "CallidoRestore-x64.msi"
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
HELPER_START_WAIT = 20                                       # seconds for the helper to say "helper started" before the app quits


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


def helper_command(msi, app_exe, pid, log, sha256):
    """The detached helper: wait for the app (pid) to exit, check the MSI's SHA-256 once more, install it with a progress bar
    and no questions besides UAC, then start the app again. If the install fails, is declined, or the file changed since it
    was verified, the old version is still installed and starts.

    The re-check matters because the MSI waits in the user-writable cache between the app's verification and msiexec (the
    app's exit plus up to 60 s), and UAC would elevate whatever file is there by then."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or ""): raise UpdateError("helper needs the verified SHA-256 of the MSI")
    res = _ps_quote(str(log) + ".result")
    # One argument string with the paths in double quotes: Start-Process does not quote array elements, so a path with a space
    # (a Windows user name like "Jan de Vries" in the cache path) would split. Windows paths cannot contain double quotes.
    msi_args = _ps_quote(f'/i "{msi}" /passive /norestart /l*v "{log}"')
    script = (
        f"$ErrorActionPreference = 'Stop'; "
        f"try {{ Add-Content -LiteralPath {res} -Value ('helper started ' + (Get-Date -Format o)); "
        f"Wait-Process -Id {int(pid)} -Timeout 60 -ErrorAction SilentlyContinue; "
        # .NET, not Get-FileHash: that cmdlet is missing when powershell.exe inherits a PowerShell 7 module path
        f"$s = [IO.File]::OpenRead({_ps_quote(msi)}); try {{ $h = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($s)).Replace('-', '') }} finally {{ $s.Dispose() }}; "
        f"if ($h -ne '{sha256.upper()}') {{ throw ('the MSI changed after it was verified (sha256 ' + $h + '); not installing it') }}; "
        f"$p = Start-Process -FilePath 'msiexec.exe' -ArgumentList {msi_args} "
        f"-Wait -PassThru; "
        f"Add-Content -LiteralPath {res} -Value ('msiexec exit ' + $p.ExitCode) }} "
        f"catch {{ Add-Content -LiteralPath {res} -Value ('helper error: ' + $_) }} "
        f"finally {{ Start-Process -FilePath {_ps_quote(app_exe)} }}"     # the app always comes back, updated or not
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script]


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()


def _log(msg):
    try:
        from callido_restore import update
        update.log(msg)
    except Exception: pass


def start_helper(cmd, result, wait=HELPER_START_WAIT):
    """Start the helper hidden and return its process once it has written "helper started" to ``result``. Raises UpdateError
    (and ends the helper) when it exits or stays silent: then the app must not quit, or nothing would bring it back.

    CREATE_NO_WINDOW, not DETACHED_PROCESS: powershell.exe is a console program and gets a hidden console this way. Started
    detached from the windowed app (v1.2.0), the helper never wrote its first line on Niels's PC, and the app quit anyway;
    the CI test had started it attached. Out of the app's job, when there is one that allows it, so it survives the app."""
    result = Path(result)
    try: result.unlink()
    except OSError: pass
    kw = dict(close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try: proc = subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError: proc = subprocess.Popen(cmd, creationflags=flags, **kw)     # a job that forbids breaking away
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            if "helper started" in result.read_text(errors="replace"): return proc
        except OSError: pass
        if proc.poll() is not None:
            raise UpdateError(f"the update helper (PowerShell) ended at once with code {proc.returncode}; nothing was installed. "
                              "Install the new version from the release page.")
        time.sleep(0.2)
    try: proc.kill()
    except OSError: pass
    raise UpdateError(f"the update helper (PowerShell) did not start within {wait} s; a virus scanner may have blocked it. Nothing was "
                      "installed: install the new version from the release page.")


def helper_smoke(result):
    """``--helper-smoke <file>`` (packaging/windows/smoke_windows.py): from the windowed exe, start a hidden PowerShell exactly
    like the update helper, confirm it runs, and exit at once; the helper writes "helper done" 3 s later, so the smoke test
    sees that it outlives the app. Exit code 0 when it started, 1 with the reason in ``result`` otherwise."""
    q = _ps_quote(result)
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command",
           f"Add-Content -LiteralPath {q} -Value 'helper started'; Start-Sleep -Seconds 3; Add-Content -LiteralPath {q} -Value 'helper done'"]
    try: start_helper(cmd, result)
    except UpdateError as e:
        Path(result).write_text(f"failed: {e}\n"); return 1
    return 0


def _allow_foreground():
    """Let the helper's windows (msiexec's progress, the UAC prompt) come to the front after the app has gone: a prompt a
    background process asks for otherwise only blinks in the taskbar, and the app looks closed for good."""
    try:
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)          # ASFW_ANY
    except Exception: pass


def install_and_relaunch(msi, exit_app=None, sha256=None):
    """Start the helper, wait until it runs, then quit the app (``exit_app(0)``, by default the server's ``_exit_app``, which also
    stops rclone, so no file of the installed app is in use when msiexec replaces it). Only from the installed, packaged app.
    When the helper does not start, UpdateError is raised and the app stays open."""
    if not oscompat.WINDOWS: raise UpdateError("the MSI update runs on Windows only")
    if not getattr(sys, "frozen", False): raise UpdateError("updates install only into the packaged app (this runs from source)")
    msi = Path(msi)
    if not msi.is_file(): raise UpdateError(f"missing {msi}")
    log = paths.CONF_DIR / f"update-{msi.parent.name}.log"
    # ``sha256``: the value download() verified against the signed list; otherwise the file is hashed now, right after that
    # verification, and the helper refuses to install anything else.
    cmd = helper_command(msi, sys.executable, os.getpid(), log, sha256 or _sha256_of(msi))
    _log(f"starting the install helper (result in {log}.result)"); t0 = time.time()
    try: proc = start_helper(cmd, str(log) + ".result")
    except UpdateError as e:
        _log(f"install helper failed: {e}"); raise
    _log(f"install helper running (pid {proc.pid}, started in {time.time() - t0:.1f} s); the app quits for msiexec")
    _allow_foreground()
    if exit_app is None:
        from callido_restore.server.api import _exit_app as exit_app
    exit_app(0)
