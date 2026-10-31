"""The shared rclone client: subprocess fallback when the daemon is not running, remote-control calls when it is, and (when an
rclone binary is available) a real daemon round trip on a local folder."""
import json, os, shutil, subprocess, tempfile, unittest
from unittest import mock

from callido_restore.sources import rclone as rc


class SplitPath(unittest.TestCase):
    def test_remote_and_local(self):
        self.assertEqual(rc.split_path("gdrive:Synology/x.hbk"), ("gdrive:", "Synology/x.hbk"))
        self.assertEqual(rc.split_path("nas:"), ("nas:", ""))
        self.assertEqual(rc.split_path("/Volumes/Backup/x.hbk"), ("/Volumes/Backup/x.hbk", ""))


class Fallback(unittest.TestCase):
    """Without a daemon the helpers run the same rclone command lines as before."""
    def setUp(self):
        self.assertFalse(rc.DAEMON.running)

    def test_ls_builds_the_old_command_line(self):
        ok = subprocess.CompletedProcess([], 0, stdout=json.dumps([{"Path": "a", "Name": "a", "IsDir": True, "Size": -1}]), stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok) as run:
            out = rc.ls("nas:share", dirs_only=True, max_depth=1, no_modtime=True)
        self.assertEqual(out[0]["Name"], "a")
        self.assertEqual(run.call_args[0][0][1:], ["lsjson", "--no-mimetype", "--no-modtime", "--dirs-only", "--max-depth", "1", "nas:share"])

    def test_ls_failure_raises_with_rclone_text(self):
        bad = subprocess.CompletedProcess([], 1, stdout="", stderr="directory not found")
        with mock.patch.object(subprocess, "run", return_value=bad):
            with self.assertRaises(rc.RcloneError) as cm: rc.ls("nas:missing")
        self.assertIn("directory not found", str(cm.exception))

    def test_remotes_parses_listremotes_long(self):
        ok = subprocess.CompletedProcess([], 0, stdout="nas: smb\ngdrive: drive\n", stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok):
            self.assertEqual(rc.remotes(), [("nas:", "smb"), ("gdrive:", "drive")])


class Cache(unittest.TestCase):
    def setUp(self): rc.forget()
    def tearDown(self): rc.forget()

    def test_repeat_listing_is_served_from_cache_until_forgotten(self):
        ok = subprocess.CompletedProcess([], 0, stdout=json.dumps([{"Path": "a", "Name": "a", "IsDir": True, "Size": -1}]), stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok) as run:
            rc.ls("nas:", dirs_only=True, cache=True); rc.ls("nas:", dirs_only=True, cache=True)
            self.assertEqual(run.call_count, 1)
            rc.ls("nas:", dirs_only=True)                      # uncached call always asks rclone
            self.assertEqual(run.call_count, 2)
            rc.forget("nas:"); rc.ls("nas:", dirs_only=True, cache=True)
            self.assertEqual(run.call_count, 3)

    def test_cache_expires(self):
        ok = subprocess.CompletedProcess([], 0, stdout="nas: smb\n", stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok) as run, mock.patch.object(rc, "CACHE_TTL", 0.0):
            rc.remotes(cache=True); rc.remotes(cache=True)
            self.assertEqual(run.call_count, 2)


class ViaDaemon(unittest.TestCase):
    """With the daemon 'running', calls go to operations/list etc. (rc() is faked here)."""
    def test_ls_uses_operations_list_with_max_depth(self):
        calls = []
        def fake_rc(method, timeout=None, **params):
            calls.append((method, params)); return {"list": [{"Path": "x", "Name": "x", "IsDir": False, "Size": 3}]}
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(rc.DAEMON, "rc", side_effect=fake_rc):
            out = rc.ls("gdrive:Synology", files_only=True, max_depth=1)
        self.assertEqual(out[0]["Name"], "x")
        m, p = calls[0]
        self.assertEqual(m, "operations/list"); self.assertEqual((p["fs"], p["remote"]), ("gdrive:", "Synology"))
        self.assertTrue(p["opt"]["filesOnly"]); self.assertEqual(p["_config"], {"MaxDepth": 1})

    def test_remotes_and_version_via_rc(self):
        answers = {"config/listremotes": {"remotes": ["nas", "gdrive"]}, "config/dump": {"nas": {"type": "smb"}, "gdrive": {"type": "drive"}}, "core/version": {"version": "v1.75.2"}}
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(rc.DAEMON, "rc", side_effect=lambda m, timeout=None, **p: answers[m]):
            self.assertEqual(rc.remotes(), [("nas:", "smb"), ("gdrive:", "drive")])
            self.assertEqual(rc.version(), "rclone v1.75.2")


@unittest.skipUnless(rc.RCLONE and shutil.which(rc.RCLONE), "needs an rclone binary")
class RealDaemon(unittest.TestCase):
    """Start the real daemon once, list a temp folder through it, stop it."""
    def test_round_trip_on_a_local_folder(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "sub")); open(os.path.join(d, "f.txt"), "w").write("hi")
        self.assertTrue(rc.start(timeout=15)); self.addCleanup(rc.stop)
        self.assertTrue(rc.DAEMON.running)
        names = sorted(e["Name"] for e in rc.ls(d))
        self.assertEqual(names, ["f.txt", "sub"])
        self.assertEqual([e["Name"] for e in rc.ls(d, dirs_only=True)], ["sub"])
        self.assertTrue(rc.version().startswith("rclone v"))
        with self.assertRaises(rc.RcloneError): rc.ls(os.path.join(d, "nonexistent"))
        rc.stop(); self.assertFalse(rc.DAEMON.running)


if __name__ == "__main__":
    unittest.main()
