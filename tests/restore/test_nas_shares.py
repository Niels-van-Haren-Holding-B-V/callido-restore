"""The Synology package lists this NAS's shared folders as restore targets, and whether the app may write into each."""
import os, sys, tempfile, unittest
from callido_restore.restore.destination import nas_shares


@unittest.skipIf(sys.platform == "win32", "a Synology volume layout; permissions as on Linux")
class NasShares(unittest.TestCase):
    def test_shared_folders_and_access(self):
        with tempfile.TemporaryDirectory() as t:
            for d in ("volume1/Documents", "volume1/Photos", "volume1/@appstore", "volume1/#recycle", "volume1/.hidden",
                      "volume2/Photos", "volume2/homes", "volumeUSB1/usbshare"):
                os.makedirs(os.path.join(t, d))
            open(os.path.join(t, "volume1", "a-file"), "w").close()
            os.chmod(os.path.join(t, "volume1", "Photos"), 0o555)                 # no Read/Write for the app yet
            try:
                got = nas_shares(os.path.join(t, "volume[0-9]*"))
            finally:
                os.chmod(os.path.join(t, "volume1", "Photos"), 0o755)
            self.assertEqual([s["name"] for s in got], ["Documents", "Photos", "homes"])   # DSM's own folders and files left out; Photos once
            by = {s["name"]: s for s in got}
            self.assertEqual(by["Documents"]["path"], os.path.join(t, "volume1", "Documents"))
            self.assertTrue(by["Documents"]["writable"])
            if os.geteuid() != 0: self.assertFalse(by["Photos"]["writable"])

    def test_no_volumes(self):
        self.assertEqual(nas_shares("/nonexistent-volume[0-9]*"), [])


if __name__ == "__main__":
    unittest.main()
