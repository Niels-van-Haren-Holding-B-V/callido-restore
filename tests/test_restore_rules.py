"""T8 rules: a restore never writes over or deletes what is at the destination.

* ``dest_inside_source`` refuses a destination that is the backup source or lies inside it (remote path or local
  folder/cache), and accepts siblings and unrelated folders.
* ``list_destination`` raises when rclone cannot list the destination; a listing failure is never treated as "empty".
* ``Backup.restore`` has no overwrite parameter any more.
"""
import inspect, json, os, subprocess, tempfile, unittest
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


class NoOverwriteParameter(unittest.TestCase):
    def test_restore_has_no_overwrite_option(self):
        self.assertNotIn("overwrite", inspect.signature(hbk_explorer.Backup.restore).parameters)


if __name__ == "__main__":
    unittest.main()
