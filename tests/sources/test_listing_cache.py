"""Remote folder listings are kept in index/<source>/listings.json so a restart opens a backup without Drive round trips;
they expire after Source.LISTING_TTL, a refresh re-reads the remote, and local entries are always read fresh from disk."""
import os, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from callido_restore import paths
from callido_restore.sources import rclone
from callido_restore.sources.source import Source


def _entries(names):
    return [{"Name": n, "Size": 3, "IsDir": False, "Path": n} for n in names]


class PersistedListings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for name in ("INDEX_DIR", "CACHE_DIR"):
            p = mock.patch.object(paths, name, Path(self.tmp.name) / name.lower()); p.start(); self.addCleanup(p.stop)
        self.local = os.path.join(self.tmp.name, "mirror", "backup_1.hbk"); os.makedirs(self.local)

    def src(self): return Source("s1", "s1", self.local, ["nas:Backups/backup_1.hbk"])

    def test_second_source_instance_reuses_the_listing(self):
        with mock.patch.object(rclone, "ls", return_value=_entries(["a", "b"])) as ls:
            self.assertEqual(sorted(self.src().listdir("Config")), ["a", "b"])
            self.assertEqual(ls.call_count, 1)
            self.assertTrue((paths.INDEX_DIR / "s1" / "listings.json").is_file())
            self.assertEqual(sorted(self.src().listdir("Config")), ["a", "b"])   # a new process: no remote call
            self.assertEqual(ls.call_count, 1)

    def test_refresh_and_forget_ask_the_remote_again(self):
        with mock.patch.object(rclone, "ls", return_value=_entries(["a"])) as ls:
            s = self.src(); s.listdir("Config"); s.listdir("Config", refresh=True)
            self.assertEqual(ls.call_count, 2)
            s.forget_listings(); self.assertFalse((paths.INDEX_DIR / "s1" / "listings.json").exists())
            s.listdir("Config"); self.assertEqual(ls.call_count, 3)

    def test_expired_listing_is_refetched(self):
        with mock.patch.object(rclone, "ls", return_value=_entries(["a"])) as ls:
            s = self.src(); s.listdir("Config")
            with mock.patch.object(Source, "LISTING_TTL", 0.0):
                self.src().listdir("Config")
            self.assertEqual(ls.call_count, 2)

    def test_local_entries_are_read_fresh_and_merged(self):
        with mock.patch.object(rclone, "ls", return_value=_entries(["remote.db"])):
            s = self.src(); self.assertEqual(sorted(s.listdir("Config")), ["remote.db"])
            os.makedirs(os.path.join(self.local, "Config")); Path(self.local, "Config", "local.db").write_bytes(b"x" * 5)
            ents = self.src().listdir("Config")                                 # new instance: remote part from disk, local part live
            self.assertEqual(sorted(ents), ["local.db", "remote.db"])
            self.assertTrue(ents["local.db"]["local"]); self.assertTrue(ents["remote.db"]["remote"])

    def test_unreachable_remote_keeps_the_saved_listing(self):
        with mock.patch.object(rclone, "ls", return_value=_entries(["a"])):
            self.src().listdir("Config")
        with mock.patch.object(rclone, "ls", side_effect=rclone.RcloneError("offline")), mock.patch.object(Source, "LISTING_TTL", 0.0):
            self.assertEqual(sorted(self.src().listdir("Config")), ["a"])


if __name__ == "__main__":
    unittest.main()
