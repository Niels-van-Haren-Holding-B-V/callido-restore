"""T8 rules: a restore never writes over or deletes what is at the destination.

* ``dest_inside_source`` refuses a destination that is the backup source or lies inside it (remote path or local
  folder/cache), and accepts siblings and unrelated folders.
* ``list_destination`` raises when rclone cannot list the destination; a listing failure is never treated as "empty".
* ``Backup.restore`` has no overwrite parameter any more.
* ``place_file`` never replaces what is at the destination, even when it appeared after the existence check.
* The restore loop itself (local and remote target) leaves an existing file with a different size or mtime unchanged and
  counts it as skipped.
"""
import inspect, json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

import hbk_explorer


class DestInsideSource(unittest.TestCase):
    def test_remote_inside_and_equal(self):
        self.assertTrue(hbk_explorer.dest_inside_source("gdrive:Synology/droopy_1.hbk", "gdrive:Synology/droopy_1.hbk", "/tmp/x"))
        self.assertTrue(hbk_explorer.dest_inside_source("gdrive:Synology/droopy_1.hbk/restore", "gdrive:Synology/droopy_1.hbk/", "/tmp/x"))

    def test_remote_sibling_and_other_remote(self):
        self.assertFalse(hbk_explorer.dest_inside_source("gdrive:Synology/droopy_1.hbk-restore", "gdrive:Synology/droopy_1.hbk", "/tmp/x"))
        self.assertFalse(hbk_explorer.dest_inside_source("nas:GDriveRestore/restore-2333", "gdrive:Synology/droopy_1.hbk", "/tmp/x"))
        self.assertFalse(hbk_explorer.dest_inside_source("nas:share/folder", None, "/tmp/x"))

    def test_local_inside_source_folder(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "backup.hbk"); os.makedirs(os.path.join(src, "Pool"))
            self.assertTrue(hbk_explorer.dest_inside_source(src, None, src))
            self.assertTrue(hbk_explorer.dest_inside_source(os.path.join(src, "Pool"), None, src))
            self.assertFalse(hbk_explorer.dest_inside_source(os.path.join(d, "restore"), None, src))
            self.assertFalse(hbk_explorer.dest_inside_source(src + "-copy", None, src))

    def test_local_source_given_as_remote_path(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(hbk_explorer.dest_inside_source(os.path.join(d, "sub"), d, "/nonexistent/cache"))


class ListDestination(unittest.TestCase):
    def test_failure_raises_instead_of_empty(self):
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="directory not found")
        with mock.patch.object(hbk_explorer.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as cm:
                hbk_explorer.list_destination("nas:share/missing")
        self.assertIn("directory not found", str(cm.exception))

    def test_success_returns_paths(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps([{"Path": "homes/a/x.txt", "Size": 3}, {"Path": "b.jpg", "Size": 9}]), stderr="")
        with mock.patch.object(hbk_explorer.subprocess, "run", return_value=ok):
            self.assertEqual(hbk_explorer.list_destination("nas:share/restore"), {"homes/a/x.txt", "b.jpg"})

    def test_empty_listing_is_empty_set(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(hbk_explorer.subprocess, "run", return_value=ok):
            self.assertEqual(hbk_explorer.list_destination("nas:share/new"), set())


class PlaceFile(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        self.tmp = os.path.join(self.d, "stage", "f"); os.makedirs(os.path.dirname(self.tmp))
        Path(self.tmp).write_bytes(b"NEW"); os.utime(self.tmp, (1700000000, 1700000000))
        self.out = os.path.join(self.d, "share", "sub", "f")

    def test_places_new_file_with_mtime(self):
        self.assertTrue(hbk_explorer.place_file(self.tmp, self.out))
        self.assertEqual(Path(self.out).read_bytes(), b"NEW")
        self.assertEqual(int(os.stat(self.out).st_mtime), 1700000000)
        self.assertFalse(os.path.exists(self.tmp))

    def test_existing_file_is_kept(self):
        os.makedirs(os.path.dirname(self.out)); Path(self.out).write_bytes(b"OLD-LONGER"); os.utime(self.out, (1000, 1000))
        self.assertFalse(hbk_explorer.place_file(self.tmp, self.out))
        self.assertEqual(Path(self.out).read_bytes(), b"OLD-LONGER")
        self.assertEqual(int(os.stat(self.out).st_mtime), 1000)
        self.assertFalse(os.path.exists(self.tmp))

    def test_existing_symlink_even_dangling_is_kept(self):
        os.makedirs(os.path.dirname(self.out)); os.symlink("/nonexistent/target", self.out)
        self.assertFalse(hbk_explorer.place_file(self.tmp, self.out))
        self.assertTrue(os.path.islink(self.out)); self.assertEqual(os.readlink(self.out), "/nonexistent/target")

    def test_without_hard_links_places_and_keeps_existing(self):
        with mock.patch.object(hbk_explorer.os, "link", side_effect=PermissionError("no hard links")):
            self.assertTrue(hbk_explorer.place_file(self.tmp, self.out))
            self.assertEqual(Path(self.out).read_bytes(), b"NEW")
            Path(self.tmp).write_bytes(b"NEW2")
            self.assertFalse(hbk_explorer.place_file(self.tmp, self.out))
            self.assertEqual(Path(self.out).read_bytes(), b"NEW")
            self.assertFalse(os.path.exists(self.tmp))


class _FakePool:
    """Writes ``size`` bytes of the file's letter and sets the mtime, like ``PoolReader.extract``; ``appear`` lists the output
    names for which a foreign file shows up at the final destination while the extract runs (the lexists/extract race)."""
    def __init__(self, dest, appear=()):
        self.dest, self.appear, self.calls = dest, set(appear), []
    def extract(self, off, out, size=None, mtime=None, job=None, fh_cache=None):
        self.calls.append(os.path.basename(out))
        Path(out).write_bytes(os.path.basename(out)[0].encode() * size); os.utime(out, (mtime, mtime))
        if os.path.basename(out) in self.appear:
            final = os.path.join(self.dest, "share", os.path.basename(out))     # every test file lives in share/<name>
            os.makedirs(os.path.dirname(final), exist_ok=True); Path(final).write_bytes(b"INTRUDER")
        return size


class _Backup(hbk_explorer.Backup):
    """Real ``restore``; the pool and the file list come from the test (``pool`` is a read-only property on Backup)."""
    pool = property(lambda self: self._fake_pool)


def _fake_backup(dest, files, appear=()):
    b = _Backup.__new__(_Backup)
    b.src = mock.Mock(id="testsrc"); b._fake_pool = _FakePool(dest, appear)
    b.restore_plan = lambda items, skip_meta=True: [{"share": "share", "path": n, "off": -1, "size": sz, "mtime": mt} for n, sz, mt in files]
    return b


class RestoreLoopLocal(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        self.dest = os.path.join(self.d, "dest"); os.makedirs(os.path.join(self.dest, "share"))
        p = mock.patch.object(hbk_explorer, "JOBS_DIR", Path(self.d) / "jobs"); p.start(); self.addCleanup(p.stop)

    def run_restore(self, files, appear=()):
        b = _fake_backup(self.dest, files, appear)
        job = {"id": "t1", "label": "restore", "started": 0.0}
        return b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], self.dest, job), job, b

    def test_existing_file_with_other_size_and_mtime_is_skipped(self):
        old = os.path.join(self.dest, "share", "a.txt"); Path(old).write_bytes(b"OLD"); os.utime(old, (1000, 1000))
        res, job, b = self.run_restore([("a.txt", 5, 1700000000), ("b.txt", 4, 1700000001)])
        self.assertEqual(Path(old).read_bytes(), b"OLD"); self.assertEqual(int(os.stat(old).st_mtime), 1000)
        self.assertEqual(Path(self.dest, "share", "b.txt").read_bytes(), b"bbbb")
        self.assertEqual(int(os.stat(os.path.join(self.dest, "share", "b.txt")).st_mtime), 1700000001)
        self.assertEqual((res["files"], res["restored"], res["skipped"], res["errors"]), (2, 2, 1, 0))
        self.assertEqual(job["skipped"], 1); self.assertEqual(b.pool.calls, ["b.txt"])
        self.assertEqual(sorted(os.listdir(self.dest)), ["share"])            # staging folder removed, nothing else added

    def test_resume_removes_only_this_jobs_leftover_staging_folder(self):
        mine = os.path.join(self.dest, ".hbk-restore-t1-abc"); other = os.path.join(self.dest, ".hbk-restore-zz-abc")
        for d in (mine, other): os.makedirs(d); Path(d, "share", "x").parent.mkdir(); Path(d, "share", "x").write_bytes(b"part")
        self.run_restore([("b.txt", 4, 1700000001)])
        self.assertFalse(os.path.exists(mine)); self.assertTrue(os.path.exists(os.path.join(other, "share", "x")))
        self.assertEqual(sorted(os.listdir(self.dest)), [".hbk-restore-zz-abc", "share"])

    def test_file_appearing_during_extract_is_kept(self):
        res, job, b = self.run_restore([("c.txt", 3, 1700000000)], appear={"c.txt"})
        self.assertEqual(Path(self.dest, "share", "c.txt").read_bytes(), b"INTRUDER")
        self.assertEqual((res["restored"], res["skipped"], res["errors"]), (1, 1, 0))
        self.assertEqual(sorted(os.listdir(self.dest)), ["share"])


class RestoreLoopRemote(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        p = mock.patch.object(hbk_explorer, "JOBS_DIR", Path(self.d) / "jobs"); p.start(); self.addCleanup(p.stop)
        self.calls = []
        def run(cmd, **kw):
            self.calls.append(cmd)
            if cmd[1] == "lsjson":
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([{"Path": "share/a.txt", "Size": 3}]), stderr="")
            if cmd[1] == "moveto":
                staged, target = cmd[2], cmd[3]
                if not target.endswith("/c.txt"): os.unlink(staged)        # moved; c.txt "appeared" after the listing: left in place
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            raise AssertionError(cmd)
        p = mock.patch.object(hbk_explorer.subprocess, "run", side_effect=run); p.start(); self.addCleanup(p.stop)

    def test_listed_and_late_files_are_skipped_others_uploaded(self):
        b = _fake_backup("nas:Restore", [("a.txt", 5, 1), ("b.txt", 4, 2), ("c.txt", 3, 3)])
        job = {"id": "t2", "label": "restore", "started": 0.0}
        res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job)
        moves = [c for c in self.calls if c[1] == "moveto"]
        self.assertEqual([c[3] for c in moves], ["nas:Restore/share/b.txt", "nas:Restore/share/c.txt"])
        self.assertTrue(all("--ignore-existing" in c for c in moves))
        self.assertEqual((res["files"], res["restored"], res["skipped"], res["errors"]), (3, 3, 2, 0))
        self.assertEqual(b.pool.calls, ["b.txt", "c.txt"])


class NoOverwriteParameter(unittest.TestCase):
    def test_restore_has_no_overwrite_option(self):
        self.assertNotIn("overwrite", inspect.signature(hbk_explorer.Backup.restore).parameters)


if __name__ == "__main__":
    unittest.main()
