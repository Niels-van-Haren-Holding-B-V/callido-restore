"""T8 rules: a restore never writes over or deletes what is at the destination.

* A destination inside the backup source is refused by ``Source.contains_dest`` (tests in ``test_dest_check.py``).
* ``list_destination`` raises when rclone cannot list the destination; a listing failure is never treated as "empty".
* ``Backup.restore`` has no overwrite parameter any more.
"""
import inspect, json, subprocess, unittest
from unittest import mock

import hbk_explorer


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
