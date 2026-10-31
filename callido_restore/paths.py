"""Where things live: the user's settings folder and the bundled resources (static UI, rclone). Bottom layer, imports nothing
from the package. Code reads these through the module (``paths.INDEX_DIR``), so tests can point them at a temp folder."""
import os, re, sys
from pathlib import Path

WINDOWS = os.name == "nt"
_DRIVE = re.compile(r"[A-Za-z]:(?:[\\/]|$)")

def is_local_abs(p, windows=None):
    """An absolute local path: "/..." everywhere; on Windows also a drive ("C:\\x", "C:/x", "C:") or a UNC path ("\\\\nas\\share").
    rclone reads a single letter before ":" as a drive on Windows too, so a remote can never be named like that there."""
    w = WINDOWS if windows is None else windows
    return p.startswith("/") or (w and (bool(_DRIVE.match(p)) or p.startswith(("\\\\", "//"))))

def is_remote_path(p, windows=None):
    """rclone 'remote:path' rather than a local path."""
    return ":" in p and not is_local_abs(p, windows)

def ui_path(p):
    """A local path as the UI shows and joins it: forward slashes on Windows too ("D:/Restore/x"), which Windows accepts."""
    return Path(p).as_posix() if WINDOWS else str(p)

def _resource_roots():
    """Directories that may hold bundled files (static/, rclone).

    Source checkout: the folder that holds callido_restore/ (static/, rclone). PyInstaller sets sys._MEIPASS
    (onedir `_internal`, or Contents/Frameworks inside a macOS .app). Data files
    in a .app live under Contents/Resources and are normally cross-linked into
    Frameworks; sibling folders are checked so a missing link still resolves.
    """
    roots, seen = [], set()
    def add(p):
        key = os.path.normpath(str(p))
        if key not in seen:
            seen.add(key); roots.append(Path(key))
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass: add(meipass)
        exe_dir = Path(sys.executable).resolve().parent
        add(exe_dir); add(exe_dir / "_internal")
        if exe_dir.name == "MacOS":
            contents = exe_dir.parent
            add(contents / "Frameworks"); add(contents / "Resources")
    else:
        add(Path(__file__).resolve().parent.parent)        # the checkout root: static/, a local rclone binary
    return roots

def _bundled(rel):
    for root in _resource_roots():
        p = root / rel
        if p.is_file(): return p
    return None

def _ensure_executable(path):
    """Copies of a collected binary sometimes lose the executable bit."""
    if os.name == "nt" or os.access(path, os.X_OK): return True
    try: path.chmod(path.stat().st_mode | 0o755)
    except OSError: return False
    return os.access(path, os.X_OK)

def _find_rclone():
    names = ("rclone.exe", "rclone") if os.name == "nt" else ("rclone",)
    found = []
    for root in _resource_roots():
        for name in names:
            c = root / name
            if c.is_file(): found.append(c)
    found.sort(key=lambda p: (p.is_symlink(), str(p)))  # real file before a cross-link
    for c in found:
        if _ensure_executable(c): return str(c)
    # A packaged app must not silently run whatever rclone is on the machine.
    if getattr(sys, "frozen", False):
        return ""
    for c in (Path("/opt/homebrew/bin/rclone"), Path("/usr/local/bin/rclone")):
        if c.is_file() and os.access(c, os.X_OK): return str(c)
    return "rclone.exe" if os.name == "nt" else "rclone"

RCLONE = _find_rclone()

CONF_DIR = Path.home() / ".hbk-explorer"

CONF_FILE = CONF_DIR / "sources.json"

INDEX_DIR = CONF_DIR / "index"

CACHE_DIR = CONF_DIR / "cache"   # downloaded bytes only; never a configured source folder

JOBS_DIR = CONF_DIR / "jobs"
