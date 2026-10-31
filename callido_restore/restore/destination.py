"""Restore destinations: never overwrite or delete anything already there (T8). ``place_file`` moves a staged file into place
atomically, ``file_target``/``restore_roots`` map backup folders to destinations (H8), ``restore_target_error`` validates them."""
import json, os, subprocess
from pathlib import Path
from callido_restore.paths import RCLONE
from callido_restore.sources import rclone
from callido_restore.sources.source import is_remote_path

def place_file(tmp, out):
    """Move the staged file ``tmp`` to ``out`` without ever replacing what is at ``out``. True when placed; False when something
    (a file, directory or symlink) already existed there, in which case it is left untouched. ``tmp`` is removed either way.
    A hard link is atomic and keeps the mtime; where the destination has no hard links (some SMB/exFAT mounts) the name is
    claimed with O_EXCL first and only that empty placeholder is replaced."""
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            os.link(tmp, out); return True
        except FileExistsError:
            return False
        except OSError:
            pass
        # The placeholder is briefly visible as an empty file at out; it is this call's own and is replaced or removed below.
        try: fd = os.open(out, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError: return False
        os.close(fd)
        try: os.replace(tmp, out)
        except OSError:
            try: os.unlink(out)
            except OSError: pass
            raise
        return True
    finally:
        try:
            if os.path.lexists(tmp): os.unlink(tmp)
        except OSError: pass

def file_target(f, items, dest):
    """(root, rel) for a planned file. An item with a 'dest' (H8: one destination per top-level shared folder) keeps the
    file's path inside the share: root = that dest, rel = the file's path, so a selected subfolder lands at the same
    relative place under the share's destination and no version or share folder is added. Items without 'dest' go under
    the common dest as <share>/<path> (one-folder mode)."""
    it = items[f["item"]] if f.get("item") is not None and f["item"] < len(items) else None
    d = (it or {}).get("dest")
    if not d: return dest, f"{f['share']}/{f['path']}"
    return d.rstrip("/"), f["path"]

def restore_target_error(src, items, dest, create):
    """Why a restore into these items/dest must not start, or None. The source stays read-only: every mapped root and the
    common dest are checked against it. Remote roots are created (rclone mkdir); a local root is created when the folder
    above it exists, or, on a resume (create=False), must already exist (the share may not be mounted any more)."""
    if not items: return "nothing selected"
    unmapped = sorted({it["share"] for it in items if not it.get("dest")})
    if unmapped and not dest: return "no destination for " + ", ".join(unmapped)
    bad = src.contains_dest(dest, unmapped) if unmapped else ""
    for it in items:
        if bad: break
        if it.get("dest") and src.contains_root(it["dest"]): bad = it["dest"]
    if bad: return f"refusing to write {bad}: it is the backup itself, lies inside it, or the backup lies inside it (the source is read-only)"
    for root in restore_roots(items, dest):
        if is_remote_path(root):
            if not create: continue                                   # listed (and refused on failure) when the restore starts
            r = subprocess.run([RCLONE, "mkdir", root], capture_output=True, text=True)
            if r.returncode != 0: return f"cannot use remote destination {root}: {r.stderr.strip()[-200:]}"
        elif not os.path.isdir(root):
            if create and os.path.isdir(os.path.dirname(root)): os.makedirs(root, exist_ok=True)      # a new folder next to existing ones
            elif create: return f"the folder above {root} does not exist (is the share mounted?)"
            else: return f"destination folder {root} is not there any more (is the share mounted?)"
    return None

def restore_roots(items, dest):
    """The distinct roots a restore writes into, in item order; the common dest only when an item has no 'dest'."""
    roots = []
    for it in items:
        r = (it.get("dest") or dest or "").rstrip("/")
        if r and r not in roots: roots.append(r)
    return roots

def list_destination(dest):
    """Paths of every file already at a remote destination (recursive). Raises when the listing fails: an unknown
    destination must never be treated as empty, because the restore never writes over what is there."""
    try: entries = rclone.ls(dest, recurse=True, files_only=True)
    except rclone.RcloneError as e:
        raise RuntimeError(f"cannot list the destination {dest}: {e}; nothing was written")
    return {e["Path"] for e in entries}
