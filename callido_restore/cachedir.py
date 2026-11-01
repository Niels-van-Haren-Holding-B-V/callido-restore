"""The app's download cache (paths.CACHE_DIR): what is in it, and clearing it.

The cache holds downloaded bytes only (bucket files, chunk-table slices, update downloads); everything in it is fetched again on
demand. The file lists and the pool map live in paths.INDEX_DIR and are never touched here: clearing the cache does not undo a
preparation. Clearing is refused by the API while a restore, a preparation or an update runs, because those read from the cache.
"""
import os, shutil
from callido_restore import paths


def _size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try: total += os.lstat(os.path.join(root, f)).st_size
            except OSError: pass
    return total


def usage(names=None):
    """{path, bytes, parts: [{id, label, bytes}]}: one part per top-level folder (a backup's id, or 'updates'), largest first.
    names: backup id -> display name."""
    names = names or {}
    root = paths.CACHE_DIR; parts = []
    if root.is_dir():
        for e in sorted(root.iterdir()):
            b = _size(e) if e.is_dir() else (e.stat().st_size if e.is_file() else 0)
            label = names.get(e.name) or ("update downloads" if e.name == "updates" else e.name)
            parts.append({"id": e.name, "label": label, "bytes": b})
    parts.sort(key=lambda p: -p["bytes"])
    return {"path": str(root), "bytes": sum(p["bytes"] for p in parts), "parts": parts}


def clear(part=None):
    """Delete the cache's contents (or one top-level part of it). Returns {freed, left}: bytes freed and bytes that could not be
    removed (a file still open on Windows, for example). The folder itself stays."""
    root = paths.CACHE_DIR
    if not root.is_dir(): return {"freed": 0, "left": 0}
    if part is not None and (not isinstance(part, str) or part in ("", ".", "..") or "/" in part or "\\" in part):
        raise ValueError("not a cache part")
    targets = [root / part] if part else list(root.iterdir())
    before = sum(_size(t) if t.is_dir() else (t.stat().st_size if t.exists() else 0) for t in targets)
    for t in targets:
        if t.is_dir() and not t.is_symlink(): shutil.rmtree(t, ignore_errors=True)
        else:
            try: t.unlink()
            except OSError: pass
    left = sum(_size(t) if t.is_dir() else (t.stat().st_size if t.exists() else 0) for t in targets)
    return {"freed": before - left, "left": left}
