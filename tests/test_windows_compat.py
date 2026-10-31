"""Windows portability (T14): local Windows paths are never taken for rclone remotes, the subprocess flags per platform, and the
Windows-only process helpers (run for real on the windows-latest CI job, skipped elsewhere)."""
import os, subprocess, sys, unittest
from unittest import mock

from callido_restore import oscompat, paths
from callido_restore.pool import anchor, poolmap


class PathKinds(unittest.TestCase):
    def test_windows_local_paths(self):
        for p in ("C:\\Restore", "C:/Restore", "c:", "D:\\", "\\\\nas\\share\\x", "//nas/share", "/Volumes/x"):
            self.assertFalse(paths.is_remote_path(p, windows=True), p)
            self.assertTrue(paths.is_local_abs(p, windows=True), p)

    def test_remotes_stay_remotes(self):
        for w in (True, False):
            for p in ("nas:Backups/x.hbk", "gdrive:", "my-drive:Synology", "ab:c"):
                self.assertTrue(paths.is_remote_path(p, windows=w), (p, w))

    def test_posix_unchanged(self):
        # On macOS/Linux a one-letter remote keeps working exactly as before.
        self.assertTrue(paths.is_remote_path("C:Restore", windows=False))
        self.assertTrue(paths.is_remote_path("C:/Restore", windows=False))
        self.assertFalse(paths.is_remote_path("/Volumes/a:b", windows=False))
        self.assertFalse(paths.is_remote_path("relative/folder", windows=False))

    def test_source_reexport(self):
        from callido_restore.sources.source import is_remote_path
        self.assertIs(is_remote_path, paths.is_remote_path)


class PopenFlags(unittest.TestCase):
    def test_posix(self):
        with mock.patch.object(oscompat, "WINDOWS", False):
            self.assertEqual(oscompat.popen_kw(), {})
            self.assertEqual(oscompat.popen_kw(new_group=True), {"start_new_session": True})

    def test_windows(self):
        with mock.patch.object(oscompat, "WINDOWS", True):
            self.assertEqual(oscompat.popen_kw(), {"creationflags": oscompat.CREATE_NO_WINDOW})
            self.assertEqual(oscompat.popen_kw(new_group=True),
                             {"creationflags": oscompat.CREATE_NO_WINDOW | oscompat.CREATE_NEW_PROCESS_GROUP})


class RssBranches(unittest.TestCase):
    def test_windows_branch_uses_working_set(self):
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch.object(oscompat, "peak_rss_mb", return_value=42.5):
            self.assertEqual(anchor._rss_mb(), 42.5)

    @unittest.skipIf(os.name == "nt", "POSIX branch")
    def test_posix_branch(self):
        with mock.patch.object(oscompat, "WINDOWS", False):
            self.assertGreater(anchor._rss_mb(), 1)


class KillGroup(unittest.TestCase):
    def test_windows_uses_taskkill_tree(self):
        p = mock.Mock(pid=1234)
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch.object(oscompat, "kill_tree", return_value=True) as kt:
            poolmap._kill_group(p, grace=0.1)
        kt.assert_called_once_with(1234); p.kill.assert_not_called(); p.wait.assert_called_once()

    def test_windows_falls_back_to_kill(self):
        p = mock.Mock(pid=1234)
        with mock.patch.object(oscompat, "WINDOWS", True), mock.patch.object(oscompat, "kill_tree", return_value=False):
            poolmap._kill_group(p, grace=0.1)
        p.kill.assert_called_once()


@unittest.skipUnless(os.name == "nt", "Windows only")
class RealWindows(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(oscompat.pid_alive(os.getpid()))
        p = subprocess.Popen([sys.executable, "-c", "pass"]); p.wait()
        self.assertFalse(oscompat.pid_alive(p.pid))

    def test_peak_rss(self):
        self.assertGreater(oscompat.peak_rss_mb(), 1)

    def test_kill_tree(self):
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
        p = subprocess.Popen([sys.executable, "-c", code], **oscompat.popen_kw(new_group=True))
        self.assertTrue(oscompat.kill_tree(p.pid))
        p.wait(10)
        self.assertFalse(oscompat.pid_alive(p.pid))


if __name__ == "__main__":
    unittest.main()
