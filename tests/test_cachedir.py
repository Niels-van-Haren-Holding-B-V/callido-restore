import tempfile, unittest
from pathlib import Path
from callido_restore import cachedir, paths


class CacheDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.old = paths.CACHE_DIR
        paths.CACHE_DIR = Path(self.tmp.name) / "cache"
        (paths.CACHE_DIR / "src1" / "pool").mkdir(parents=True); (paths.CACHE_DIR / "updates").mkdir()
        (paths.CACHE_DIR / "src1" / "pool" / "a.bucket").write_bytes(b"x" * 300)
        (paths.CACHE_DIR / "updates" / "app.dmg").write_bytes(b"y" * 100)
        self.index = Path(self.tmp.name) / "index.json"; self.index.write_text("{}")      # outside the cache: never touched

    def tearDown(self):
        paths.CACHE_DIR = self.old; self.tmp.cleanup()

    def test_usage_per_part_largest_first_with_names(self):
        u = cachedir.usage({"src1": "Weekly backup"})
        self.assertEqual(u["bytes"], 400)
        self.assertEqual([(p["label"], p["bytes"]) for p in u["parts"]], [("Weekly backup", 300), ("update downloads", 100)])

    def test_clear_one_part_then_all_keeps_the_folder(self):
        self.assertEqual(cachedir.clear("updates"), {"freed": 100, "left": 0})
        self.assertTrue((paths.CACHE_DIR / "src1").is_dir())
        self.assertEqual(cachedir.clear(), {"freed": 300, "left": 0})
        self.assertTrue(paths.CACHE_DIR.is_dir()); self.assertEqual(list(paths.CACHE_DIR.iterdir()), [])
        self.assertTrue(self.index.is_file())

    def test_a_part_is_a_name_never_a_path(self):
        for bad in ("..", "../x", "a/b", "", ".", 3):
            with self.assertRaises(ValueError): cachedir.clear(bad)
        self.assertEqual(cachedir.usage()["bytes"], 400)

    def test_missing_cache_folder(self):
        paths.CACHE_DIR = Path(self.tmp.name) / "nope"
        self.assertEqual(cachedir.usage()["bytes"], 0); self.assertEqual(cachedir.clear(), {"freed": 0, "left": 0})


if __name__ == "__main__":
    unittest.main()
