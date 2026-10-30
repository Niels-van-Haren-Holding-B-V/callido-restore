"""Restore destination safety: a target inside the backup source is refused (local roots and rclone remotes)."""
import tempfile, unittest
from pathlib import Path

import hbk_explorer


class ContainsDestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = hbk_explorer.INDEX_DIR; self.old_cache = hbk_explorer.CACHE_DIR
        hbk_explorer.INDEX_DIR = Path(self.tmp.name) / "index"; hbk_explorer.CACHE_DIR = Path(self.tmp.name) / "cache"
        self.local = Path(self.tmp.name) / "mirror" / "droopy_1.hbk"; (self.local / "Config").mkdir(parents=True)
        self.extra = Path(self.tmp.name) / "vol" / "droopy_1.hbk"; self.extra.mkdir(parents=True)
        self.src = hbk_explorer.Source("t7", "t7", str(self.local), ["nas:Backups/droopy_1.hbk", "gdrive:Synology/droopy_1.hbk"], [str(self.extra)])
    def tearDown(self):
        hbk_explorer.INDEX_DIR = self.old; hbk_explorer.CACHE_DIR = self.old_cache; self.tmp.cleanup()
    def test_local_paths(self):
        c = self.src.contains_dest
        self.assertTrue(c(str(self.local)))                        # the backup itself
        self.assertTrue(c(str(self.local / "Config")))             # inside it
        self.assertTrue(c(str(self.local.parent)))                 # a parent of it (restore would write next to the .hbk tree)
        self.assertTrue(c(str(self.extra / "Pool")))               # inside an extra root
        self.assertFalse(c(str(Path(self.tmp.name) / "restore-2333")))
        self.assertFalse(c(str(Path(self.tmp.name) / "mirror" / "droopy_1.hbk-restore")))   # sibling with a shared prefix
    def test_remote_paths(self):
        c = self.src.contains_dest
        self.assertTrue(c("nas:Backups/droopy_1.hbk"))
        self.assertTrue(c("nas:Backups/droopy_1.hbk/Pool"))
        self.assertTrue(c("nas:Backups"))                          # parent of the backup on the same remote
        self.assertTrue(c("nas:"))                                 # remote root
        self.assertTrue(c("gdrive:Synology/droopy_1.hbk/"))
        self.assertFalse(c("nas:Restore/restore-2333"))
        self.assertFalse(c("nas:Backups/droopy_1.hbk-restore"))
        self.assertFalse(c("other:Backups/droopy_1.hbk"))


if __name__ == "__main__":
    unittest.main()
