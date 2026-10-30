"""Resumable download of Hyper Backup bucket index files (Pool/0/<d>/*.index.*).

Already-downloaded index files are never deleted or overwritten. A file that
exists and whose size matches the pool listing is skipped. rclone is invoked
with ``--ignore-existing`` so a file that is present (even at a different
size) is left untouched. Closing the app and running the pool-map build again
continues from the files that are still missing; the files on disk are the
checkpoint (nothing new is written under ``index/<src>/pool/``).
"""
import re, stat, subprocess, threading, time
from pathlib import Path

_CHK = re.compile(r"chk#(\d+)/(\d+)")
_XFR = re.compile(r"xfr#(\d+)/(\d+)")
# Integer "Checks:" / "Transferred:" lines are file counts. Byte lines carry a unit
# ("1.055 MiB") and do not match.
_FILE = re.compile(r"\b(Checks|Transferred):\s+([0-9][0-9,]*)\s*/\s*([0-9][0-9,]*)\s*,\s*(\d+)%")
# rclone's stats String(): "<size> / <size>, <pct>, <speed>, ETA <eta>" where pct is "12%" or "-".
_BYTE = re.compile(r"/\s*[\d.]+\s*[A-Za-z]+\s*,\s*(\d+%|-)")


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
        out["byte_pct"] = None if m.group(1) == "-" else int(m.group(1).rstrip("%"))
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
    """One rclone copy for Pool/0/<directory>/*.index.*.

    ``copy`` (not ``sync``) plus ``--ignore-existing`` so files already on disk
    are not replaced. No delete flag is set.
    """
    return [
        rclone, "copy", f"{remote}/Pool/0/{directory}", str(dest),
        "--include", "*.index.*",
        "--ignore-existing",
        "--transfers", "16",
        "--checkers", "16",
        "--drive-pacer-min-sleep", "10ms",
        "--drive-pacer-burst", "200",
        "--retries", "5",
        "--low-level-retries", "20",
        "--retries-sleep", "5s",
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


def index_file_state(dest, name, size):
    """'complete', 'mismatch', or 'missing'. Existing files are never classified in a way that deletes them."""
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


def fetch_bucket_indexes(local_root, remotes, listing, job, run=None, attempts=2, pause=time.sleep,
                         progress_base=0.4, progress_span=0.3, rclone=None):
    """Download missing bucket index files. Never deletes or overwrites a file that is already present.

    ``run(cmd, on_line, should_stop) -> int`` defaults to rclone. Return -1 to report a cancel.
    A non-zero exit is retried ``attempts`` times per remote; other remotes are tried when files
    are still missing. Directories whose index files are all present (any size) are not fetched.
    Size mismatches are counted and left on disk.
    """
    local_root = Path(local_root)
    if run is None:
        run = run_rclone
    if rclone is None:
        from hbk_explorer import RCLONE
        rclone = RCLONE
    by_dir = indexes_by_dir(listing)
    dirs = sorted(by_dir)
    n = len(dirs)
    skipped = 0
    mismatch_count = 0
    failed = []

    def counts(dest, expected):
        comp = mis = miss = 0
        for name, size in expected.items():
            state = index_file_state(dest, name, size)
            if state == "complete":
                comp += 1
            elif state == "mismatch":
                mis += 1
            else:
                miss += 1
        return comp, mis, miss

    def satisfied():
        n_ok = 0
        for d, expected in by_dir.items():
            dest = local_root / "Pool" / "0" / str(d)
            _, _, miss = counts(dest, expected)
            if miss == 0:
                n_ok += 1
        return n_ok

    def stopped():
        done = satisfied()
        job["message"] = f"stopped - {done}/{n} index directories have no missing files; downloaded index files were kept. Run Build again to continue."
        return {"stopped": True, "directories": n, "directories_complete": done, "skipped": skipped,
                "failed": list(failed), "mismatch_count": mismatch_count}

    if job.get("cancel"):
        return stopped()
    if not remotes or not dirs:
        return {"stopped": False, "directories": n, "directories_complete": n, "skipped": n,
                "failed": [], "mismatch_count": 0}

    for i, d in enumerate(dirs):
        if job.get("cancel"):
            return stopped()
        expected = by_dir[d]
        dest = local_root / "Pool" / "0" / str(d)
        dest.mkdir(parents=True, exist_ok=True)
        comp, mis, miss = counts(dest, expected)
        total = len(expected)
        if miss == 0:
            skipped += 1
            mismatch_count += mis
            note = "already complete" if mis == 0 else f"{mis} size mismatch(es) left untouched"
            job["message"] = describe_dir(i, n, d, None, comp, total, note)
            job["progress"] = _progress(progress_base, progress_span, i, n, 1.0)
            continue
        got = False
        for remote in remotes:
            if job.get("cancel"):
                return stopped()
            for attempt in range(attempts):
                if job.get("cancel"):
                    return stopped()
                latest = {}
                def on_line(line, _i=i, _d=d, _total=total, _dest=dest, _expected=expected):
                    st = parse_rclone_stats(line)
                    if not st:
                        return
                    full = st.pop("_full", False)
                    if full:
                        latest.clear()
                    latest.update(st)
                    have, _, _ = counts(_dest, _expected)
                    job["message"] = describe_dir(_i, n, _d, latest, have, _total)
                    job["progress"] = _progress(progress_base, progress_span, _i, n, stats_fraction(latest, have, _total))
                rc = run(index_copy_cmd(rclone, remote, d, dest), on_line, lambda: bool(job.get("cancel")))
                if rc == -1 or job.get("cancel"):
                    return stopped()
                comp, mis, miss = counts(dest, expected)
                if rc == 0 and miss == 0:
                    got = True
                    break
                if rc == 0 and miss:
                    break  # this remote finished but files are still missing; try the next one
                if attempt + 1 < attempts:
                    job["message"] = describe_dir(i, n, d, None, comp, total, f"failed, retry {attempt + 1}")
                    pause(3)
            if got or counts(dest, expected)[2] == 0:
                got = True
                break
        comp, mis, miss = counts(dest, expected)
        mismatch_count += mis
        if miss == 0:
            job["message"] = describe_dir(i, n, d, None, comp, total, "done" if mis == 0 else f"{mis} size mismatch(es) left untouched")
            job["progress"] = _progress(progress_base, progress_span, i, n, 1.0)
        else:
            failed.append(d)
            job["message"] = describe_dir(i, n, d, None, comp, total, f"{miss} still missing")
    if failed:
        names = ", ".join(f"Pool/0/{d}" for d in failed)
        err = f"bucket index fetch failed for {names}; already-downloaded index files were left in place. Run Build again to continue."
        job["message"] = err
        return {"stopped": False, "directories": n, "directories_complete": satisfied(), "skipped": skipped,
                "failed": failed, "mismatch_count": mismatch_count, "error": err}
    return {"stopped": False, "directories": n, "directories_complete": satisfied(), "skipped": skipped,
            "failed": [], "mismatch_count": mismatch_count}
