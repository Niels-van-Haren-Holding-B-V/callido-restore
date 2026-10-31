"""Resumable download of Hyper Backup bucket index files (Pool/0/<d>/*.index.*).

Reads may come from the app cache or from a configured source folder (the backup
itself, or a mirror that already holds an earlier download). Writes go only to
the app cache. rclone is ``copy`` from the remote to that cache, with
``--ignore-existing``. Nothing here deletes, renames, or uploads a source file.

A cache file whose size does not match the listing is renamed inside the cache
to ``<name>.mismatch`` (never overwritten, never deleted) and the name is
fetched again. A wrong-size file that lives in a source folder is left as it is;
it does not count as complete. Mapping must not start while a required index
is still missing or the wrong size.

Closing the app and running the pool-map build again continues from whatever
correct files are already readable. Nothing new is written under
``index/<src>/pool/``.
"""
import re, stat, subprocess, threading, time
from pathlib import Path

from callido_restore.sources import transfer

_CHK = re.compile(r"chk#(\d+)/(\d+)")
_XFR = re.compile(r"xfr#(\d+)/(\d+)")
# Integer "Checks:" / "Transferred:" lines are file counts. Byte lines carry a unit
# ("1.055 MiB") and do not match.
_FILE = re.compile(r"\b(Checks|Transferred):\s+([0-9][0-9,]*)\s*/\s*([0-9][0-9,]*)\s*,\s*(\d+)%")
# rclone's stats String(): "<size> / <size>, <pct>, <speed>, ETA <eta>" where pct is "12%" or "-". The first size is what this
# rclone run has transferred so far, in-flight bytes included; it feeds the job's byte counter between completed files.
_BYTE = re.compile(r"([\d.]+)\s*([A-Za-z]+)\s*/\s*[\d.]+\s*[A-Za-z]+\s*,\s*(\d+%|-)")
_UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40, "PiB": 1 << 50,
          "kB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}


def parse_rclone_stats(line):
    """Pull progress out of one rclone ``--stats`` line.

    Returns None when the line is not a stats line. ``_full`` is set when the
    line is a byte-progress snapshot (the one-line form, or the first line of
    the multi-line form) so the caller can drop stale counters from the
    previous interval. Recognises:

    * current one-line form (``size / size, PCT, speed, ETA`` plus optional
      ``xfr#a/b`` and ``chk#a/b``; PCT is ``-`` when the total is unknown)
    * multi-line ``Checks: N / M, P%`` and integer ``Transferred: N / M, P%``
    """
    if not line:
        return None
    out = {}
    m = _CHK.search(line)
    if m:
        out["checks"] = (int(m.group(1)), int(m.group(2)))
    m = _XFR.search(line)
    if m:
        out["transfers"] = (int(m.group(1)), int(m.group(2)))
    for m in _FILE.finditer(line):
        a = int(m.group(2).replace(",", ""))
        b = int(m.group(3).replace(",", ""))
        key = "checks" if m.group(1) == "Checks" else "transfers"
        out[key] = (a, b)
    m = _BYTE.search(line)
    if m:
        out["_full"] = True
        out["byte_pct"] = None if m.group(3) == "-" else int(m.group(3).rstrip("%"))
        unit = _UNITS.get(m.group(2))
        if unit:
            out["bytes"] = int(float(m.group(1)) * unit)
    return out or None


def stats_fraction(stats, have, total):
    """0..1 within one directory. Prefer byte progress, then check counts, then files on disk."""
    stats = stats or {}
    byte_pct = stats.get("byte_pct")
    checks = stats.get("checks")
    transfers = stats.get("transfers")
    if isinstance(byte_pct, int) and byte_pct > 0:
        return byte_pct / 100.0
    if checks and checks[1]:
        return checks[0] / checks[1]
    if transfers and transfers[1]:
        return transfers[0] / transfers[1]
    if total:
        return have / total
    return 0.0


def describe_dir(i, n, d, stats, have, total, note=""):
    head = f"bucket indexes: directory {i + 1}/{n} (Pool/0/{d})"
    bits = []
    stats = stats or {}
    if stats.get("checks"):
        a, b = stats["checks"]
        bits.append(f"checks {a}/{b}")
    if stats.get("transfers"):
        a, b = stats["transfers"]
        bits.append(f"transferred {a}/{b}")
    if isinstance(stats.get("byte_pct"), int):
        bits.append(f"{stats['byte_pct']}%")
    if total:
        bits.append(f"{have}/{total} files on disk")
    if note:
        bits.append(note)
    return head if not bits else head + " - " + ", ".join(bits)


def index_copy_cmd(rclone, remote, directory, dest):
    """Copy Pool/0/<directory>/*.index.* from a remote into the local app cache.

    ``dest`` is a directory under the app cache, never a source folder and never
    the remote. ``copy`` (not ``sync`` or ``move``) plus ``--ignore-existing``
    so a file already in the cache is not replaced. No delete flag is set.
    The remote is only read.
    """
    return [
        rclone, "copy", f"{remote}/Pool/0/{directory}", str(dest),
        "--include", "*.index.*",
        "--ignore-existing",
        *transfer.profile("index_files").flags(),          # parallelism and resilience: sources/transfer.py, one place for all
        "--stats", "1s",
        "--stats-one-line",
        "--stats-log-level", "NOTICE",
    ]


def indexes_by_dir(listing):
    """directory number -> {index file name: size or None}."""
    out = {}
    for e in listing:
        if e.get("t") != "index" or "b" not in e or not e.get("name"):
            continue
        out.setdefault(int(e["b"]) // 2048, {})[e["name"]] = e.get("size")
    return out


def _bucket_dir(root, directory):
    return Path(root) / "Pool" / "0" / str(int(directory))


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def park_cache_mismatch(directory, name):
    """Rename a wrong-size index inside one cache directory. Never overwrites or deletes.

    Returns the new path, or None if the file could not be moved aside. The bytes stay
    in the cache either way. This must not be pointed at a source folder.
    """
    src = Path(directory) / name
    try:
        if not src.is_file():
            return None
    except OSError:
        return None
    for n in range(0, 1000):
        suffix = ".mismatch" if n == 0 else f".mismatch.{n}"
        dest = src.with_name(name + suffix)
        if dest.exists():
            continue
        try:
            src.rename(dest)
            return dest
        except OSError:
            return None
    return None


def index_file_state(dest, name, size):
    """'complete', 'mismatch', or 'missing'. Does not create, rename, or delete the file."""
    p = Path(dest) / name
    try:
        st = p.stat()
    except OSError:
        return "missing"
    if not stat.S_ISREG(st.st_mode):
        return "missing"
    if size is None:
        return "complete"
    try:
        expected = int(size)
    except (TypeError, ValueError):
        return "complete"
    return "complete" if st.st_size == expected else "mismatch"


def _progress(base, span, i, n, frac):
    if n <= 0:
        return base + span
    frac = max(0.0, min(1.0, float(frac)))
    return base + span * ((i + frac) / n)


def run_rclone(cmd, on_line, should_stop):
    """Run rclone, forwarding each stderr line to ``on_line``. Returns -1 if ``should_stop`` fires (process is terminated, files are not removed)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    def reader():
        try:
            for line in proc.stderr:
                on_line(line.rstrip("\n"))
        except Exception:
            pass
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    cancelled = False
    try:
        while proc.poll() is None:
            if should_stop():
                cancelled = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                break
            time.sleep(0.2)
    finally:
        t.join(timeout=2)
    if cancelled:
        return -1
    return proc.returncode if proc.returncode is not None else 1


def _name_states(cache_root, readonly_roots, directory, name, size):
    """Cache state, and whether any read-only root already has this name at the listing size."""
    cache_state = index_file_state(_bucket_dir(cache_root, directory), name, size)
    source_ok = False
    for root in readonly_roots or ():
        if index_file_state(_bucket_dir(root, directory), name, size) == "complete":
            source_ok = True
            break
    return cache_state, source_ok


def _refuse_source_write(dest, readonly_roots):
    for root in readonly_roots or ():
        if _inside(dest, root):
            raise RuntimeError(f"refusing to write bucket indexes into source folder {root}")


def fetch_bucket_indexes(cache_root, remotes, listing, job, run=None, attempts=2, pause=time.sleep,
                         progress_base=0.4, progress_span=0.3, rclone=None, readonly_roots=()):
    """Download missing bucket index files into ``cache_root`` only.

    A file counts as present when the cache or any read-only root has it at the
    listing size. Those roots (the backup, an extra folder, or a user-made mirror
    that already holds a download) are never written. A wrong-size file in the
    cache is renamed to ``*.mismatch`` inside the cache and fetched again. A
    wrong-size file in a read-only root is left in place and does not count.

    ``run(cmd, on_line, should_stop) -> int`` defaults to rclone. Return -1 to
    report a cancel. A non-zero exit is retried ``attempts`` times per remote.
    The returned dict has ``error`` set when anything required is still missing
    or the wrong size, so the caller can stop before mapping.
    """
    cache_root = Path(cache_root)
    readonly_roots = [Path(r) for r in (readonly_roots or ())]
    if run is None:
        run = run_rclone
    if rclone is None:
        from callido_restore.paths import RCLONE
        rclone = RCLONE
    by_dir = indexes_by_dir(listing)
    dirs = sorted(by_dir)
    n = len(dirs)
    skipped = 0
    mismatch_count = 0
    failed = []
    # Bytes for the UI ("1.2 of 6.9 GB downloaded"): the listing sizes of every index file, and the bytes of those complete so far.
    job["total_bytes"] = sum(int(size or 0) for expected in by_dir.values() for size in expected.values())
    done_before = 0                      # complete bytes of the directories handled so far
    done_dir = {}                        # complete bytes of the directory in progress

    def set_done(directory, bytes_complete):
        done_dir[directory] = bytes_complete
        job["done_bytes"] = done_before + bytes_complete

    def tally(directory, expected, park=False, track=True):
        """Return (complete_count, cache_mismatch_count, missing_count); records the complete bytes for the progress text
        (``track=False`` for the final sweep over every directory, which must not move the counters).

        ``park`` renames a wrong-size cache file aside. Read-only roots are never changed.
        A correct file in a read-only root counts as complete even when the cache has no copy.
        """
        comp = mis = miss = 0; have = 0
        for name, size in expected.items():
            cache_state, source_ok = _name_states(cache_root, readonly_roots, directory, name, size)
            if park and cache_state == "mismatch":
                park_cache_mismatch(_bucket_dir(cache_root, directory), name)
                cache_state = index_file_state(_bucket_dir(cache_root, directory), name, size)
            if cache_state == "complete" or source_ok:
                comp += 1; have += int(size or 0)
            elif cache_state == "mismatch":
                mis += 1
            else:
                miss += 1
        if track: set_done(directory, have)
        return comp, mis, miss

    def satisfied():
        ok = 0
        for d, expected in by_dir.items():
            comp, mis, miss = tally(d, expected, park=False, track=False)
            if mis == 0 and miss == 0 and comp == len(expected):
                ok += 1
        return ok

    def stopped():
        done = satisfied()
        job["message"] = (f"stopped - {done}/{n} index directories are complete; "
                          "downloaded index files were kept. Run Build again to continue.")
        return {"stopped": True, "directories": n, "directories_complete": done, "skipped": skipped,
                "failed": list(failed), "mismatch_count": mismatch_count}

    if job.get("cancel"):
        return stopped()
    if not dirs:
        return {"stopped": False, "directories": 0, "directories_complete": 0, "skipped": 0,
                "failed": [], "mismatch_count": 0}

    for i, d in enumerate(dirs):
        if job.get("cancel"):
            return stopped()
        expected = by_dir[d]
        comp, mis, miss = tally(d, expected, park=True)
        total = len(expected)
        if mis == 0 and miss == 0:
            skipped += 1
            done_before += done_dir.pop(d, 0)
            job["message"] = describe_dir(i, n, d, None, comp, total, "already complete")
            job["progress"] = _progress(progress_base, progress_span, i, n, 1.0)
            continue
        if not remotes:
            failed.append(d)
            done_before += done_dir.pop(d, 0)
            mismatch_count += mis
            job["message"] = describe_dir(i, n, d, None, comp, total, f"{miss + mis} not usable")
            continue
        dest = _bucket_dir(cache_root, d)
        _refuse_source_write(dest, readonly_roots)
        dest.mkdir(parents=True, exist_ok=True)
        got = False
        for remote in remotes:
            if job.get("cancel"):
                return stopped()
            for attempt in range(attempts):
                if job.get("cancel"):
                    return stopped()
                latest = {}
                have0 = done_dir.get(d, 0)                     # complete bytes before this rclone run; its counter starts at 0
                dir_bytes = sum(int(size or 0) for size in expected.values())
                def on_line(line, _i=i, _d=d, _total=total, _expected=expected, _have0=have0, _dir_bytes=dir_bytes):
                    st = parse_rclone_stats(line)
                    if not st:
                        return
                    full = st.pop("_full", False)
                    if full:
                        latest.clear()
                    latest.update(st)
                    have, _, _ = tally(_d, _expected, park=False)
                    # Between completed files the on-disk count stands still; rclone's transferred bytes (partial files
                    # included) keep the job's bytes, and so the speed in the UI, moving. Never below the complete bytes,
                    # never above the directory's listed size.
                    if latest.get("bytes"):
                        set_done(_d, max(done_dir.get(_d, 0), min(_have0 + latest["bytes"], _dir_bytes)))
                    job["message"] = describe_dir(_i, n, _d, latest, have, _total)
                    job["progress"] = _progress(progress_base, progress_span, _i, n, stats_fraction(latest, have, _total))
                rc = run(index_copy_cmd(rclone, remote, d, dest), on_line, lambda: bool(job.get("cancel")))
                if rc == -1 or job.get("cancel"):
                    return stopped()
                comp, mis, miss = tally(d, expected, park=False)
                if rc == 0 and miss == 0 and mis == 0:
                    got = True
                    break
                if rc == 0 and (miss or mis):
                    break  # this remote finished but files are still unusable; try the next one
                if attempt + 1 < attempts:
                    job["message"] = describe_dir(i, n, d, None, comp, total, f"failed, retry {attempt + 1}")
                    pause(3)
            if got or tally(d, expected, park=False)[1:] == (0, 0):
                got = True
                break
        comp, mis, miss = tally(d, expected, park=False)
        done_before += done_dir.get(d, 0); done_dir.pop(d, None)          # this directory is settled; the next one adds to it
        mismatch_count += mis
        if mis == 0 and miss == 0:
            job["message"] = describe_dir(i, n, d, None, comp, total, "done")
            job["progress"] = _progress(progress_base, progress_span, i, n, 1.0)
        else:
            failed.append(d)
            job["message"] = describe_dir(i, n, d, None, comp, total, f"{miss + mis} still missing or wrong size")
    if failed:
        names = ", ".join(f"Pool/0/{d}" for d in failed[:8])
        extra = f" (+{len(failed) - 8} more)" if len(failed) > 8 else ""
        err = (f"bucket index files are missing or the wrong size ({names}{extra}); mapping was not started. "
               "Source folders were not modified. A wrong-size file in the app cache was renamed to *.mismatch there.")
        job["message"] = err
        return {"stopped": False, "directories": n, "directories_complete": satisfied(), "skipped": skipped,
                "failed": failed, "mismatch_count": mismatch_count, "error": err}
    return {"stopped": False, "directories": n, "directories_complete": satisfied(), "skipped": skipped,
            "failed": [], "mismatch_count": mismatch_count}
