"""The search/listing filter (``Backup._where``) selects a folder by an exact, case-sensitive prefix (H19), like the restore plan."""
import sqlite3, unittest

import hbk_explorer


class WherePrefixTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.execute("create table files(path text, name text, dir text, ext text, size integer, mtime integer, ctime integer, status text, flags text, is_dir integer)")
        for p in ("Foto_2024", "Foto_2024/a.jpg", "FotoX2024/b.jpg", "Photos/c.jpg", "photos/d.jpg", "100%/e.txt", "100x/f.txt", "Photos.old/g.jpg"):
            self.c.execute("insert into files values(?,?,?,?,?,?,?,?,?,?)", (p, p.rsplit("/", 1)[-1], p.rsplit("/", 1)[0] if "/" in p else "", "", 1, 0, 0, "U", "", 0 if "/" in p else 1))

    def paths(self, q):
        where, args = hbk_explorer.Backup._where(q)
        return sorted(r[0] for r in self.c.execute(f"select path from files{where}", args))

    def test_prefix_is_exact(self):
        self.assertEqual(self.paths({"prefix": "Foto_2024"}), ["Foto_2024", "Foto_2024/a.jpg"])       # the folder itself and what is below it
        self.assertEqual(self.paths({"prefix": "Photos"}), ["Photos/c.jpg"])
        self.assertEqual(self.paths({"prefix": "photos"}), ["photos/d.jpg"])
        self.assertEqual(self.paths({"prefix": "100%"}), ["100%/e.txt"])
        self.assertEqual(self.paths({"prefix": "Foto_2024/"}), ["Foto_2024/a.jpg"])

    def test_no_prefix_means_everything(self):
        self.assertEqual(len(self.paths({})), 8)


if __name__ == "__main__":
    unittest.main()
