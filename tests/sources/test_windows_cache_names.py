"""A backup name Windows cannot hold as written (a shared folder "Holding B.V.": Windows drops the trailing period) is kept in
the app cache under rclone's Windows name ("Holding B.V．"). The fetch writes a plain temporary name first, so rclone's own mapping
cannot send the file elsewhere. Before: Python made "Holding B.V", rclone wrote into "Holding B.V．", the fetch retried until it
failed, and both folders showed up as extra shares with no snapshot."""
import os, tempfile, unittest
from pathlib import Path
from unittest import mock

from callido_restore import paths
from callido_restore.paths import backup_name, local_name, local_rel
from callido_restore.sources import rclone
from callido_restore.sources.source import Source


class Names(unittest.TestCase):
    def test_round_trip(self):
        for n in ("Holding B.V.", "Icon\r", "a: b?.txt", "x<y>|\"z\"*", "trailing ", "plain.txt", "B.V", ".hidden", "été"):
            w = local_name(n, windows=True)
            self.assertEqual(backup_name(w, windows=True), n)
            self.assertFalse(w.endswith((".", " ")) and n not in ("plain.txt",), w)
        self.assertEqual(local_name("Holding B.V.", windows=True), "Holding B.V．")
        self.assertEqual(local_name("Holding B.V", windows=True), "Holding B.V")             # a different share stays different
        self.assertEqual(local_rel("Config/@Share/Holding B.V./1201.db.1202", windows=True), "Config/@Share/Holding B.V．/1201.db.1202")

    def test_posix_unchanged(self):
        self.assertEqual(local_name("Holding B.V.", windows=False), "Holding B.V.")
        self.assertEqual(backup_name("Holding B.V．", windows=False), "Holding B.V．")


class Fetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        for name in ("INDEX_DIR", "CACHE_DIR"):
            p = mock.patch.object(paths, name, Path(self.tmp.name) / name.lower()); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(paths, "WINDOWS", True); p.start(); self.addCleanup(p.stop)
        self.local = os.path.join(self.tmp.name, "mirror", "b.hbk"); os.makedirs(self.local)
        self.dsts = []
        def copy(src, dst, **kw):
            self.dsts.append(Path(dst)); Path(dst).write_bytes(b"db")
        p = mock.patch.object(rclone, "copyfile", side_effect=copy); p.start(); self.addCleanup(p.stop)
        ls = {"Config/@Share": [{"Name": "Holding B.V.", "Size": 0, "IsDir": True}, {"Name": "homes", "Size": 0, "IsDir": True}]}
        p = mock.patch.object(rclone, "ls", side_effect=lambda r, **kw: ls.get(r.split("b.hbk/", 1)[-1], [])); p.start(); self.addCleanup(p.stop)
        self.s = Source("s1", "s1", self.local, ["gdrive:b.hbk"])

    def test_fetch_lands_under_the_windows_name_and_is_found(self):
        rel = "Config/@Share/Holding B.V./1201.db.1202"
        got = self.s.ensure(rel)
        self.assertEqual(got, self.s.cache_dir / "Config/@Share/Holding B.V．/1201.db.1202"); self.assertEqual(got.read_bytes(), b"db")
        self.assertEqual(self.dsts[0].parent, self.s.cache_dir / ".fetch")            # rclone wrote a plain name, outside the share folders
        self.assertRegex(self.dsts[0].name, r"^[0-9a-f]{32}\.part$")
        self.assertEqual(self.s.locate(rel), got); self.assertEqual(self.s.ensure(rel), got); self.assertEqual(len(self.dsts), 1)
        self.assertEqual(sorted(self.s.listdir("Config/@Share/Holding B.V.")), ["1201.db.1202"])

    def test_no_extra_shares_from_the_cache(self):
        share = self.s.cache_dir / "Config/@Share"
        (share / "Holding B.V").mkdir(parents=True)                                   # the empty folder the old fetch left behind
        (share / "Holding B.V．").mkdir(); (share / "Holding B.V．" / "1201.db.1202").write_bytes(b"db")
        self.assertEqual(sorted(self.s.listdir("Config/@Share")), ["Holding B.V.", "homes"])
        self.assertNotIn(".fetch", self.s.listdir(""))


if __name__ == "__main__":
    unittest.main()
