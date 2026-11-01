"""One place for how rclone moves data. Every rclone process and the daemon start from the same resilience and Google Drive
pacing (``COMMON``); each kind of work adds its own profile (``PROFILES``): how many files at once, how far to read ahead.
Nothing else in the app carries a transfer number.

Measured on 2026-10-31 on a real 6 GB backup over Google Drive with the user's own key, same rclone, 25-30 s per run:

* bucket index files (about 170 KB each, Drive takes about a second to start every file, so parallelism sets the rate):
  16 transfers 33 MB, 64 transfers 84 MB, 128 transfers 86 MB (Google's rate limit caps it there; the pacer backs off on 403);
* bucket data files (about 52 MB each, one stream gives about 7 MB/s, range requests in parallel do not help):
  4 at a time 29 MB/s, 8 at a time 43 MB/s.
"""
from dataclasses import dataclass

# Resilience and Drive pacing for every rclone process: retries at both levels, and a pacer that starts fast and slows down
# by itself when Google answers 403 rateLimitExceeded (which the user's own key makes rare, see docs/USER-GUIDE.md).
COMMON = ("--retries", "5", "--retries-sleep", "5s", "--low-level-retries", "20",
          "--drive-pacer-min-sleep", "10ms", "--drive-pacer-burst", "200")

# The daemon serves listings, index-set slices and restore copies for the whole app; a transactions-per-second cap keeps a
# burst of small requests from tripping the per-user limit that would then slow everything else down too.
DAEMON = COMMON + ("--tpslimit", "16")


@dataclass(frozen=True)
class Profile:
    """``workers``: files in flight at once. ``prefetch``: how many files a consumer may queue ahead of itself (restore).
    ``checkers``: rclone's listing/compare threads for a directory copy; 0 when the work goes through the daemon."""
    name: str
    workers: int
    prefetch: int = 0
    checkers: int = 0
    why: str = ""

    def flags(self):
        """Command-line flags for a ``rclone copy`` process of this kind (the daemon gets its profile as worker threads)."""
        out = list(COMMON) + ["--transfers", str(self.workers)]
        if self.checkers: out += ["--checkers", str(self.checkers)]
        return out


PROFILES = {
    # Pool/0/<d>/*.index.*: tens of thousands of small files, copied per directory by one rclone process.
    "index_files": Profile("index_files", workers=64, checkers=32, why="small files: start latency bound, 64 is Google's cap"),
    # Config/file_chunk*.index record-set slices (about 8 MB each) through the daemon while the pool is prepared.
    "index_slices": Profile("index_slices", workers=8, why="8 MB slices, one daemon connection each"),
    # Pool/0/<d>/*.bucket.* data files (about 52 MB each) through the daemon while files are restored.
    # 2026-11-01, a home line to Drive (3 MB/s per stream): 8 at a time 3.6 MB/s, 16 at a time 5.0 MB/s, 32 at a time 6.1 MB/s,
    # no rate-limit errors; on a faster line 8 already filled it (above), more streams cost nothing there.
    "restore": Profile("restore", workers=32, prefetch=64, why="52 MB files; slow lines need many streams (32: 6.1 MB/s where 8 gave 3.6)"),
}


def profile(kind):
    return PROFILES[kind]
