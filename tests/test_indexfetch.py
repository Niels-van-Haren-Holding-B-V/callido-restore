"""Bucket-index fetch: skip complete files, never delete them, resume, stop.

rclone ``--stats`` samples follow fs/accounting/stats.go String() (one-line
form ``size / size, pct, speed, ETA`` with optional ``xfr#`` / ``chk#``, and
the multi-line Checks / Transferred lines). pct is ``-`` when the total is 0.
"""
import tempfile, time, unittest
from pathlib import Path
from unittest import mock

import hbk_explorer
import hbk_indexfetch as F


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
        d0 = self.root / "Pool" / "0" / "0"
        d0.mkdir(parents=True)
        (d0 / "0.index.1").write_bytes(b"short")
        before = (d0 / "0.index.1").read_bytes()
        listing = _listing((0, "0.index.1", 100))
        run = self._run({"0": {"0.index.1": b"X" * 100}})
        job, result = self._fetch(listing, run)
        self.assertEqual(self.calls, [])
        self.assertEqual((d0 / "0.index.1").read_bytes(), before)
        self.assertEqual(result["mismatch_count"], 1)
        self.assertIn("left untouched", job["message"])
        self.assertEqual(result["failed"], [])

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
        self.assertEqual((d0 / "1.index.1").read_bytes(), b"NO")
        self.assertEqual((d0 / "2.index.1").read_bytes(), b"ZZZ")
        self.assertEqual(result["mismatch_count"], 1)
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
        self.assertIn("left in place", result["error"])
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


class BuildPoolWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = hbk_explorer.INDEX_DIR
        hbk_explorer.INDEX_DIR = Path(self.tmp.name) / "index"
        local = Path(self.tmp.name) / "hbk"
        local.mkdir()
        self.src = hbk_explorer.Source("t4", "t4", str(local))
        self.backup = hbk_explorer.Backup(self.src)
        pool = self.src.index_dir / "pool"
        pool.mkdir(parents=True)
        (pool / "listing.json").write_text("[]")

    def tearDown(self):
        hbk_explorer.INDEX_DIR = self.old
        self.tmp.cleanup()

    def test_stop_does_not_start_the_mapper(self):
        self.src.remotes = ["remote:hbk"]
        mapped = {}
        def fetch(*_a, **_k):
            return {"stopped": True, "directories": 2, "directories_complete": 1, "failed": [], "mismatch_count": 0}
        def boom(*_a, **_k):
            mapped["yes"] = True
            raise AssertionError("mapper ran")
        with mock.patch("hbk_pool.build_runs", lambda *_a, **_k: None), \
             mock.patch("hbk_pool.build_recsizes", lambda *_a, **_k: None), \
             mock.patch("hbk_indexfetch.fetch_bucket_indexes", fetch), \
             mock.patch("hbk_poolmap.build_poolmap", boom):
            job = {"cancel": False, "message": "", "progress": 0}
            result = self.backup.build_pool(job)
        self.assertTrue(result["stopped"])
        self.assertNotIn("yes", mapped)
        self.assertFalse((self.src.index_dir / "pool" / "poolmap.json").exists())

    def test_fetch_error_does_not_start_the_mapper(self):
        self.src.remotes = ["remote:hbk"]
        def fetch(*_a, **_k):
            return {"stopped": False, "failed": [3], "error": "bucket index fetch failed for Pool/0/3"}
        def boom(*_a, **_k):
            raise AssertionError("mapper ran")
        with mock.patch("hbk_pool.build_runs", lambda *_a, **_k: None), \
             mock.patch("hbk_pool.build_recsizes", lambda *_a, **_k: None), \
             mock.patch("hbk_indexfetch.fetch_bucket_indexes", fetch), \
             mock.patch("hbk_poolmap.build_poolmap", boom):
            with self.assertRaises(RuntimeError) as cm:
                self.backup.build_pool({"cancel": False, "message": "", "progress": 0})
        self.assertIn("Pool/0/3", str(cm.exception))

    def test_start_job_keeps_progress_when_stopped(self):
        def fn(job):
            job["progress"] = 0.42
            return {"stopped": True}
        job = hbk_explorer.start_job(fn, "stop-test")
        for _ in range(100):
            if job["state"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(job["state"], "done")
        self.assertTrue(job["result"]["stopped"])
        self.assertEqual(job["progress"], 0.42)


if __name__ == "__main__":
    unittest.main()
