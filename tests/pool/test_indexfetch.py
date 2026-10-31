"""Bucket-index fetch: skip complete files, never delete them, resume, stop.

rclone ``--stats`` samples follow fs/accounting/stats.go String() (one-line
form ``size / size, pct, speed, ETA`` with optional ``xfr#`` / ``chk#``, and
the multi-line Checks / Transferred lines). pct is ``-`` when the total is 0.
"""
import subprocess, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from callido_restore import paths
from callido_restore.restore.backup import Backup
from callido_restore.restore.jobs import start_job
from callido_restore.sources.source import Source
from callido_restore.pool import indexfetch as F


ONE_LINE = "2026/10/30 21:21:31 NOTICE:      1.055 MiB / 52.000 MiB, 2%, 500 KiB/s, ETA 1m43s (xfr#5/100, chk#12/2048)"
ZERO_LINE = "         0 B / 0 B, -, 0 B/s, ETA -"
CHECKS = "Checks:            12 / 2048, 1%, Listed 100"
FILES = "Transferred:             5 / 100, 5%"
BYTES = "Transferred:   	    1.055 MiB / 52.000 MiB, 2%, 500 KiB/s, ETA 1m43s"
COPIED = "NOTICE: 0.index.1: Copied (new)"


class ParseStatsTests(unittest.TestCase):
    def test_one_line_and_multiline(self):
        st = F.parse_rclone_stats(ONE_LINE)
        self.assertEqual(st["byte_pct"], 2)
        self.assertEqual(st["checks"], (12, 2048))
        self.assertEqual(st["transfers"], (5, 100))
        self.assertTrue(st["_full"])
        zero = F.parse_rclone_stats(ZERO_LINE)
        self.assertIsNone(zero["byte_pct"])
        self.assertTrue(zero["_full"])
        self.assertEqual(F.parse_rclone_stats(CHECKS)["checks"], (12, 2048))
        self.assertNotIn("_full", F.parse_rclone_stats(CHECKS))
        self.assertEqual(F.parse_rclone_stats(FILES)["transfers"], (5, 100))
        byte = F.parse_rclone_stats(BYTES)
        self.assertEqual(byte["byte_pct"], 2)
        self.assertNotIn("transfers", byte)
        self.assertIsNone(F.parse_rclone_stats(COPIED))
        self.assertIsNone(F.parse_rclone_stats(""))

    def test_fraction_prefers_bytes_then_checks(self):
        self.assertAlmostEqual(F.stats_fraction({"byte_pct": 2, "checks": (12, 2048)}, 0, 10), 0.02)
        self.assertAlmostEqual(F.stats_fraction({"byte_pct": None, "checks": (12, 2048)}, 0, 10), 12 / 2048)
        self.assertAlmostEqual(F.stats_fraction(None, 3, 10), 0.3)
        self.assertEqual(F.stats_fraction(None, 0, 0), 0.0)


class CommandTests(unittest.TestCase):
    def test_copy_does_not_delete_or_overwrite(self):
        cmd = F.index_copy_cmd("rclone", "remote:hbk", 3, Path("/tmp/pool/0/3"))
        self.assertEqual(cmd[1], "copy")
        self.assertNotIn("sync", cmd)
        self.assertNotIn("move", cmd)
        self.assertNotIn("moveto", cmd)
        self.assertIn("--ignore-existing", cmd)
        self.assertIn("--include", cmd)
        self.assertIn("*.index.*", cmd)
        self.assertIn("--stats", cmd)
        self.assertIn("1s", cmd)
        self.assertIn("--stats-one-line", cmd)
        self.assertNotIn("--delete", cmd)
        self.assertTrue(any(a.startswith("--delete") for a in cmd) is False)
        self.assertEqual(cmd[2], "remote:hbk/Pool/0/3")
        self.assertEqual(cmd[3], "/tmp/pool/0/3")


def _listing(*rows):
    """rows: (bucket, name, size)."""
    return [{"b": b, "t": "index", "rev": 1, "name": name, "size": size} for b, name, size in rows]


def _manifest(root):
    """Path, size, mtime, and bytes of every file under root. Used to prove a source folder is untouched."""
    root = Path(root)
    out = []
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        st = p.stat()
        out.append((str(p.relative_to(root)), st.st_size, st.st_mtime_ns, p.read_bytes()))
    return out


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, payloads, rc=0, on_call=None):
        def run(cmd, on_line, should_stop):
            self.calls.append(list(cmd))
            self.assertIn("--ignore-existing", cmd)
            self.assertNotIn("--delete", cmd)
            if should_stop():
                return -1
            on_line(ONE_LINE)
            if on_call:
                on_call(cmd, should_stop)
            if rc != 0:
                return rc
            dest = Path(cmd[3])
            for name, data in payloads.get(dest.name, {}).items():
                path = dest / name
                if path.exists():
                    continue  # --ignore-existing
                path.write_bytes(data)
            return 0
        return run

    def _fetch(self, listing, run, remotes=("remote:hbk",)):
        job = {"cancel": False, "message": "", "progress": 0.0}
        with mock.patch("os.remove", side_effect=AssertionError("remove")), \
             mock.patch("os.unlink", side_effect=AssertionError("unlink")):
            result = F.fetch_bucket_indexes(self.root, list(remotes), listing, job, run=run, attempts=2,
                                            pause=lambda _s: None, rclone="rclone")
        return job, result

    def test_skips_complete_files_and_resumes(self):
        d0 = self.root / "Pool" / "0" / "0"
        d0.mkdir(parents=True)
        (d0 / "0.index.1").write_bytes(b"A" * 8)
        (d0 / "1.index.1").write_bytes(b"B" * 4)
        listing = _listing((0, "0.index.1", 8), (0, "1.index.1", 4), (2048, "0.index.1", 3))
        run = self._run({"1": {"0.index.1": b"CCC"}})
        job, result = self._fetch(listing, run)
        self.assertFalse(result["stopped"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2], "remote:hbk/Pool/0/1")
        self.assertEqual((d0 / "0.index.1").read_bytes(), b"A" * 8)
        self.assertEqual((self.root / "Pool" / "0" / "1" / "0.index.1").read_bytes(), b"CCC")
        self.assertIn("directory 2/2", job["message"])
        self.calls.clear()
        job2, result2 = self._fetch(listing, run)
        self.assertEqual(self.calls, [])
        self.assertEqual(result2["skipped"], 2)
        self.assertIn("already complete", job2["message"])

    def test_size_mismatch_is_not_overwritten(self):
        # The short file lives in the app cache. A failed refetch must not overwrite it,
        # and must fail the fetch so mapping does not run. The source folder is not touched.
        cache = self.root / "cache"
        source = self.root / "source"
        (cache / "Pool" / "0" / "0").mkdir(parents=True)
        (source / "Pool" / "0" / "0").mkdir(parents=True)
        (cache / "Pool" / "0" / "0" / "0.index.1").write_bytes(b"short")
        (source / "Pool" / "0" / "0" / "0.index.1").write_bytes(b"short")
        (source / "sentinel.txt").write_bytes(b"do-not-touch")
        before = _manifest(source)
        listing = _listing((0, "0.index.1", 100))
        def run(cmd, on_line, should_stop):
            self.calls.append(list(cmd))
            self.assertEqual(cmd[1], "copy")
            self.assertTrue(str(cmd[2]).startswith("remote:"))
            dest = Path(cmd[3]).resolve()
            self.assertTrue(str(dest).startswith(str(cache.resolve())))
            self.assertFalse(str(dest).startswith(str(source.resolve())))
            for bad in ("sync", "move", "moveto", "delete", "purge"):
                self.assertNotIn(bad, cmd)
            return 1
        job = {"cancel": False, "message": "", "progress": 0.0}
        with mock.patch("os.remove", side_effect=AssertionError("remove")), \
             mock.patch("os.unlink", side_effect=AssertionError("unlink")):
            result = F.fetch_bucket_indexes(cache, ["remote:hbk"], listing, job, run=run, attempts=2,
                                            pause=lambda _s: None, rclone="rclone", readonly_roots=[source])
        self.assertTrue(self.calls)
        self.assertEqual(result["failed"], [0])
        self.assertIn("mapping was not started", result["error"])
        parked = list((cache / "Pool" / "0" / "0").glob("0.index.1.mismatch*"))
        self.assertEqual([p.read_bytes() for p in parked], [b"short"])
        self.assertFalse((cache / "Pool" / "0" / "0" / "0.index.1").exists())
        self.assertEqual(_manifest(source), before)

    def test_complete_neighbour_survives_partial_directory(self):
        d0 = self.root / "Pool" / "0" / "0"
        d0.mkdir(parents=True)
        (d0 / "0.index.1").write_bytes(b"KEEPME!!")
        (d0 / "1.index.1").write_bytes(b"NO")  # size 2, listing says 4
        listing = _listing((0, "0.index.1", 8), (0, "1.index.1", 4), (0, "2.index.1", 3))
        messages = []
        class Rec(dict):
            def __setitem__(self, key, value):
                if key == "message":
                    messages.append(value)
                super().__setitem__(key, value)
        def run(cmd, on_line, should_stop):
            self.calls.append(list(cmd))
            on_line(ONE_LINE)
            dest = Path(cmd[3])
            for name, data in {"0.index.1": b"nope!!!!", "1.index.1": b"YYYY", "2.index.1": b"ZZZ"}.items():
                path = dest / name
                if path.exists():
                    continue
                path.write_bytes(data)
            return 0
        job = Rec(cancel=False, message="", progress=0.0)
        result = F.fetch_bucket_indexes(self.root, ["remote:hbk"], listing, job, run=run, attempts=1,
                                        pause=lambda _s: None, rclone="rclone")
        self.assertEqual((d0 / "0.index.1").read_bytes(), b"KEEPME!!")
        # The short cache file is renamed aside (not overwritten) and replaced by a correct download.
        parked = list(d0.glob("1.index.1.mismatch*"))
        self.assertEqual([p.read_bytes() for p in parked], [b"NO"])
        self.assertEqual((d0 / "1.index.1").read_bytes(), b"YYYY")
        self.assertEqual((d0 / "2.index.1").read_bytes(), b"ZZZ")
        self.assertEqual(result["mismatch_count"], 0)
        self.assertEqual(result["failed"], [])
        self.assertTrue(any("checks 12/2048" in m and "directory 1/1" in m for m in messages))

    def test_progress_message_includes_directory_and_stats(self):
        listing = _listing((0, "0.index.1", 3))
        def run(cmd, on_line, should_stop):
            self.calls.append(cmd)
            on_line(ONE_LINE)
            on_line(COPIED)
            dest = Path(cmd[3])
            (dest / "0.index.1").write_bytes(b"abc")
            return 0
        job, result = self._fetch(listing, run)
        self.assertFalse(result["failed"])
        self.assertIn("directory 1/1", job["message"])
        self.assertGreater(job["progress"], 0.4)

    def test_stop_between_directories_keeps_files(self):
        d0 = self.root / "Pool" / "0" / "0"
        d0.mkdir(parents=True)
        listing = _listing((0, "0.index.1", 3), (2048, "0.index.1", 3))
        job = {"cancel": False, "message": "", "progress": 0.0}
        def run_cancel(cmd, on_line, should_stop):
            self.calls.append(cmd[2])
            dest = Path(cmd[3])
            (dest / "0.index.1").write_bytes(b"abc")
            if dest.name == "0":
                job["cancel"] = True
            return 0
        result = F.fetch_bucket_indexes(self.root, ["remote:hbk"], listing, job, run=run_cancel, attempts=1,
                                        pause=lambda _s: None, rclone="rclone")
        self.assertTrue(result["stopped"])
        self.assertEqual(self.calls, ["remote:hbk/Pool/0/0"])
        self.assertEqual((d0 / "0.index.1").read_bytes(), b"abc")
        self.assertFalse((self.root / "Pool" / "0" / "1" / "0.index.1").exists())
        self.assertIn("stopped", job["message"])

    def test_stop_inside_rclone_does_not_delete(self):
        d0 = self.root / "Pool" / "0" / "0"
        d0.mkdir(parents=True)
        (d0 / "0.index.1").write_bytes(b"KEEP")
        listing = _listing((0, "0.index.1", 4), (0, "1.index.1", 3))
        messages = []
        class Rec(dict):
            def __setitem__(self, key, value):
                if key == "message":
                    messages.append(value)
                super().__setitem__(key, value)
        def run(cmd, on_line, should_stop):
            self.calls.append(cmd)
            on_line("Checks: 1 / 2, 50%")
            return -1
        job = Rec(cancel=False, message="", progress=0.0)
        result = F.fetch_bucket_indexes(self.root, ["remote:hbk"], listing, job, run=run, attempts=1,
                                        pause=lambda _s: None, rclone="rclone")
        self.assertTrue(result["stopped"])
        self.assertEqual((d0 / "0.index.1").read_bytes(), b"KEEP")
        self.assertTrue(any("checks 1/2" in m for m in messages))
        self.assertIn("stopped", job["message"])

    def test_failure_is_resumable(self):
        listing = _listing((0, "0.index.1", 3), (2048, "0.index.1", 3))
        def fail(cmd, on_line, should_stop):
            self.calls.append(cmd[2])
            dest = Path(cmd[3])
            if dest.name == "0" and not (dest / "0.index.1").exists():
                (dest / "0.index.1").write_bytes(b"abc")
                return 0
            on_line("NOTICE: network down")
            return 1
        job, result = self._fetch(listing, fail)
        self.assertEqual(result["failed"], [1])
        self.assertIn("mapping was not started", result["error"])
        self.assertEqual((self.root / "Pool" / "0" / "0" / "0.index.1").read_bytes(), b"abc")
        self.calls.clear()
        def ok(cmd, on_line, should_stop):
            self.calls.append(cmd[2])
            dest = Path(cmd[3])
            target = dest / "0.index.1"
            if not target.exists():
                target.write_bytes(b"xyz")
            return 0
        job2, result2 = self._fetch(listing, ok)
        self.assertEqual(result2["failed"], [])
        self.assertEqual(self.calls, ["remote:hbk/Pool/0/1"])
        self.assertEqual((self.root / "Pool" / "0" / "0" / "0.index.1").read_bytes(), b"abc")
        self.assertEqual((self.root / "Pool" / "0" / "1" / "0.index.1").read_bytes(), b"xyz")

    def test_second_remote_used_when_the_first_lacks_files(self):
        listing = _listing((0, "0.index.1", 3))
        def run(cmd, on_line, should_stop):
            self.calls.append(cmd[2])
            if cmd[2].startswith("a:"):
                return 0
            (Path(cmd[3]) / "0.index.1").write_bytes(b"xyz")
            return 0
        _job, result = self._fetch(listing, run, remotes=("a:hbk", "b:hbk"))
        self.assertEqual(result["failed"], [])
        self.assertEqual(self.calls, ["a:hbk/Pool/0/0", "b:hbk/Pool/0/0"])
        self.assertEqual((self.root / "Pool" / "0" / "0" / "0.index.1").read_bytes(), b"xyz")

    def test_existing_source_index_is_reused_and_source_is_untouched(self):
        cache = self.root / "cache"
        source = self.root / "hbk"
        (source / "Pool" / "0" / "0").mkdir(parents=True)
        (source / "Pool" / "0" / "0" / "0.index.1").write_bytes(b"A" * 8)
        (source / "sentinel.txt").write_bytes(b"mirror-or-backup")
        before = _manifest(source)
        listing = _listing((0, "0.index.1", 8), (0, "1.index.1", 3))
        def run(cmd, on_line, should_stop):
            self.calls.append(list(cmd))
            dest = Path(cmd[3])
            self.assertTrue(str(dest.resolve()).startswith(str(cache.resolve())))
            self.assertFalse(str(dest.resolve()).startswith(str((source / "Pool").resolve())))
            (dest / "1.index.1").write_bytes(b"ZZZ")
            return 0
        job, result = {"cancel": False, "message": "", "progress": 0.0}, None
        result = F.fetch_bucket_indexes(cache, ["remote:hbk"], listing, job, run=run, attempts=1,
                                        pause=lambda _s: None, rclone="rclone", readonly_roots=[source])
        self.assertEqual(result["failed"], [])
        self.assertEqual(self.calls[0][2], "remote:hbk/Pool/0/0")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((cache / "Pool" / "0" / "0" / "1.index.1").read_bytes(), b"ZZZ")
        self.assertFalse((source / "Pool" / "0" / "0" / "1.index.1").exists())
        self.assertEqual(_manifest(source), before)

    def test_wrong_size_in_source_is_not_moved(self):
        cache = self.root / "cache"
        source = self.root / "hbk"
        (source / "Pool" / "0" / "0").mkdir(parents=True)
        (source / "Pool" / "0" / "0" / "0.index.1").write_bytes(b"short")
        before = _manifest(source)
        listing = _listing((0, "0.index.1", 4))
        def run(cmd, on_line, should_stop):
            self.calls.append(cmd[3])
            Path(cmd[3]).mkdir(parents=True, exist_ok=True)
            (Path(cmd[3]) / "0.index.1").write_bytes(b"GOOD")
            return 0
        job = {"cancel": False, "message": "", "progress": 0.0}
        result = F.fetch_bucket_indexes(cache, ["remote:hbk"], listing, job, run=run, attempts=1,
                                        pause=lambda _s: None, rclone="rclone", readonly_roots=[source])
        self.assertEqual(result["failed"], [])
        self.assertEqual((cache / "Pool" / "0" / "0" / "0.index.1").read_bytes(), b"GOOD")
        self.assertEqual(_manifest(source), before)


class ListdirCacheTests(unittest.TestCase):
    """A file that Source.ensure downloaded into the app cache is listed as local, so build_pool uses the snapshot
    databases it fetched (they were silently skipped when listdir only looked at the source folders)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = paths.INDEX_DIR; self.old_cache = paths.CACHE_DIR
        paths.INDEX_DIR = Path(self.tmp.name) / "index"; paths.CACHE_DIR = Path(self.tmp.name) / "cache"
        local = Path(self.tmp.name) / "src"; (local / "Config/@Share/s").mkdir(parents=True)
        (local / "Config/@Share/s/1.db.2").write_bytes(b"in-source")
        self.src = Source("t4l", "t4l", str(local))
        cached = self.src.cache_dir / "Config/@Share/s"; cached.mkdir(parents=True)
        (cached / "2.db.3").write_bytes(b"in-cache!")
        (cached / "1.db.2").write_bytes(b"cache-copy-wins")              # the app cache is checked first when both exist
    def tearDown(self):
        paths.INDEX_DIR = self.old; paths.CACHE_DIR = self.old_cache; self.tmp.cleanup()
    def test_ensure_refreshes_the_listing(self):
        """A download invalidates the cached listing of its directory, so the new file shows up as local."""
        self.src.remotes = ["fake:remote"]
        def fake_run(cmd, capture_output=True, text=True):               # never the real rclone
            if cmd[1] == "lsjson": return subprocess.CompletedProcess(cmd, 0, "[]", "")
            Path(cmd[3]).write_bytes(b"downloaded")                       # rclone copyto <remote/rel> <tmp>
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            self.assertNotIn("3.db.4", self.src.listdir("Config/@Share/s"))
            self.assertEqual(self.src.ensure("Config/@Share/s/3.db.4").read_bytes(), b"downloaded")
            self.assertTrue(self.src.listdir("Config/@Share/s")["3.db.4"]["local"])
            self.assertNotIn("top.bin", self.src.listdir(""))
            self.assertEqual(self.src.ensure("top.bin").read_bytes(), b"downloaded")
            self.assertTrue(self.src.listdir("")["top.bin"]["local"])      # root listing ("." parent) is refreshed too
            (self.src.cache_dir / "Pool/0.x").mkdir(parents=True)              # a dotted directory name keeps its own key
            self.assertNotIn("1.bucket.2", self.src.listdir("Pool/0.x"))
            self.assertEqual(self.src.ensure("Pool/0.x/1.bucket.2").read_bytes(), b"downloaded")
            self.assertTrue(self.src.listdir("Pool/0.x")["1.bucket.2"]["local"])

    def test_cache_files_are_local(self):
        ents = self.src.listdir("Config/@Share/s")
        self.assertEqual(sorted(ents), ["1.db.2", "2.db.3"])
        self.assertTrue(ents["2.db.3"]["local"]); self.assertEqual(ents["2.db.3"]["size"], 9)
        self.assertTrue(ents["1.db.2"]["local"])
        self.assertEqual(self.src.ensure("Config/@Share/s/2.db.3").read_bytes(), b"in-cache!")
        self.assertEqual(self.src.ensure("Config/@Share/s/1.db.2").read_bytes(), b"cache-copy-wins")


class BuildPoolWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = paths.INDEX_DIR
        self.old_cache = paths.CACHE_DIR
        paths.INDEX_DIR = Path(self.tmp.name) / "index"
        paths.CACHE_DIR = Path(self.tmp.name) / "cache"
        local = Path(self.tmp.name) / "hbk"
        local.mkdir()
        self.src = Source("t4", "t4", str(local))
        self.backup = Backup(self.src)
        pool = self.src.index_dir / "pool"
        pool.mkdir(parents=True)
        (pool / "listing.json").write_text("[]")

    def tearDown(self):
        paths.INDEX_DIR = self.old
        paths.CACHE_DIR = self.old_cache
        self.tmp.cleanup()

    def test_stop_does_not_start_the_mapper(self):
        self.src.remotes = ["remote:hbk"]
        mapped = {}
        def fetch(*_a, **_k):
            return {"stopped": True, "directories": 2, "directories_complete": 1, "failed": [], "mismatch_count": 0}
        def boom(*_a, **_k):
            mapped["yes"] = True
            raise AssertionError("mapper ran")
        with mock.patch("callido_restore.core.pool.build_runs", lambda *_a, **_k: None), \
             mock.patch("callido_restore.core.pool.build_recsizes", lambda *_a, **_k: None), \
             mock.patch("callido_restore.pool.indexfetch.fetch_bucket_indexes", fetch), \
             mock.patch("callido_restore.pool.poolmap.build_poolmap", boom):
            job = {"cancel": False, "message": "", "progress": 0}
            result = self.backup.build_pool(job)
        self.assertTrue(result["stopped"])
        self.assertNotIn("yes", mapped)
        self.assertFalse((self.src.index_dir / "pool" / "poolmap.json").exists())

    def test_wrong_size_index_does_not_start_the_mapper(self):
        self.src.remotes = ["remote:hbk"]
        pool = self.src.index_dir / "pool"
        listing = [{"b": 0, "t": "index", "rev": 1, "size": 100, "mtime": "", "name": "0.index.1"}]
        (pool / "listing.json").write_text(__import__("json").dumps(listing))
        dest = self.src.local / "Pool" / "0" / "0"
        dest.mkdir(parents=True)
        (dest / "0.index.1").write_bytes(b"short")
        (self.src.local / "sentinel.txt").write_bytes(b"source")
        before = _manifest(self.src.local)
        def run(cmd, on_line, should_stop):
            self.assertEqual(cmd[1], "copy")
            self.assertTrue(str(cmd[2]).startswith("remote:hbk/"))
            self.assertTrue(Source.in_cache(self.src, cmd[3]) or str(cmd[3]).startswith(str(self.src.cache_dir)))
            return 1
        def boom(*_a, **_k):
            raise AssertionError("mapper ran")
        with mock.patch("callido_restore.core.pool.build_runs", lambda *_a, **_k: None), \
             mock.patch("callido_restore.core.pool.build_recsizes", lambda *_a, **_k: None), \
             mock.patch("callido_restore.pool.indexfetch.run_rclone", run), \
             mock.patch("callido_restore.pool.indexfetch.time.sleep", lambda *_a, **_k: None), \
             mock.patch("callido_restore.pool.poolmap.build_poolmap", boom):
            with self.assertRaises(RuntimeError) as cm:
                self.backup.build_pool({"cancel": False, "message": "", "progress": 0})
        self.assertIn("mapping was not started", str(cm.exception))
        self.assertFalse((pool / "poolmap.json").exists())
        self.assertEqual(_manifest(self.src.local), before)
        self.assertFalse((self.src.cache_dir / "Pool" / "0" / "0" / "0.index.1").exists())

    def test_ensure_downloads_into_the_app_cache(self):
        self.src.remotes = ["remote:hbk"]
        (self.src.local / "Pool" / "0" / "0").mkdir(parents=True)
        (self.src.local / "Pool" / "0" / "0" / "0.index.1").write_bytes(b"READY")
        (self.src.local / "sentinel.txt").write_bytes(b"keep")
        before = _manifest(self.src.local)
        reused = self.src.ensure("Pool/0/0/0.index.1")
        self.assertEqual(reused, self.src.local / "Pool" / "0" / "0" / "0.index.1")
        def fake_run(cmd, **_k):
            self.assertEqual(cmd[1], "copyto")
            self.assertTrue(str(cmd[2]).startswith("remote:hbk/"))
            dest = Path(cmd[3])
            self.assertTrue(str(dest.resolve()).startswith(str(self.src.cache_dir.resolve())))
            self.assertFalse(str(dest.resolve()).startswith(str(self.src.local.resolve())))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"from-remote")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", fake_run):
            fetched = self.src.ensure("Pool/0/0/9.index.1")
        self.assertEqual(fetched.read_bytes(), b"from-remote")
        self.assertTrue(self.src.in_cache(fetched))
        self.assertEqual(_manifest(self.src.local), before)

    def test_fetch_error_does_not_start_the_mapper(self):
        self.src.remotes = ["remote:hbk"]
        def fetch(*_a, **_k):
            return {"stopped": False, "failed": [3], "error": "bucket index fetch failed for Pool/0/3"}
        def boom(*_a, **_k):
            raise AssertionError("mapper ran")
        with mock.patch("callido_restore.core.pool.build_runs", lambda *_a, **_k: None), \
             mock.patch("callido_restore.core.pool.build_recsizes", lambda *_a, **_k: None), \
             mock.patch("callido_restore.pool.indexfetch.fetch_bucket_indexes", fetch), \
             mock.patch("callido_restore.pool.poolmap.build_poolmap", boom):
            with self.assertRaises(RuntimeError) as cm:
                self.backup.build_pool({"cancel": False, "message": "", "progress": 0})
        self.assertIn("Pool/0/3", str(cm.exception))

    def test_start_job_keeps_progress_when_stopped(self):
        def fn(job):
            job["progress"] = 0.42
            return {"stopped": True}
        job = start_job(fn, "stop-test")
        for _ in range(100):
            if job["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(job["state"], "done")
        self.assertTrue(job["result"]["stopped"])
        self.assertEqual(job["progress"], 0.42)


if __name__ == "__main__":
    unittest.main()


class ProgressBytes(unittest.TestCase):
    """The fetch job carries total_bytes (listing sizes of all index files) and done_bytes (bytes complete so far) for the UI."""
    def test_bytes_follow_the_downloads(self):
        import tempfile, shutil
        from callido_restore.pool import indexfetch as F
        root = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, root, True)
        listing = [{"b": 0, "t": "index", "rev": 1, "size": 10, "name": "0.index.1"}, {"b": 1, "t": "index", "rev": 1, "size": 20, "name": "1.index.1"},
                   {"b": 2048, "t": "index", "rev": 1, "size": 30, "name": "0.index.1"}]
        seen = []
        def run(cmd, on_line, should_stop):
            d = int(str(cmd[-1]).rstrip("/").split("/")[-1]) if False else None
            for e in listing:                       # "download": write every expected file of the directory the command names
                dest = F._bucket_dir(root, e["b"] // 2048)
                if str(dest) in " ".join(map(str, cmd)): (dest / e["name"]).write_bytes(b"x" * e["size"])
            seen.append(cmd); return 0
        job = {}
        res = F.fetch_bucket_indexes(root, ["r:"], listing, job, run=run, pause=lambda *_: None)
        self.assertFalse(res.get("error")); self.assertEqual(job["total_bytes"], 60); self.assertEqual(job["done_bytes"], 60)
        job2 = {}
        F.fetch_bucket_indexes(root, ["r:"], listing, job2, run=lambda *a: 0, pause=lambda *_: None)      # everything already there
        self.assertEqual((job2["total_bytes"], job2["done_bytes"]), (60, 60))
