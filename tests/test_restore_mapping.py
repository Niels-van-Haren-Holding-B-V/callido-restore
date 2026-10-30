"""H8: one destination per selected backup folder.

Each basket item may carry its own ``dest``; its contents are written there directly (no version or source folder in
between). Items without ``dest`` go under the common ``dest`` as ``<share>/<path>``. The mapping is persisted in the job
file, so pause/resume keeps it. ``/api/restore`` refuses a mapped target inside the backup and creates new local roots.
"""
import json, os, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import hbk_explorer
from hbk_explorer import file_target, restore_roots


class FileTargetTests(unittest.TestCase):
    items = [{"share": "Documents", "ver": 1234, "path": "", "is_dir": 1, "dest": "nas:Documents"},
             {"share": "Photos", "ver": 1234, "path": "2024/Trip", "is_dir": 1, "dest": "/Volumes/Photos/Trip"},
             {"share": "homes", "ver": 1234, "path": "alice/notes.txt", "is_dir": 0, "dest": "nas:homes/alice"},
             {"share": "Backups", "ver": 1234, "path": "", "is_dir": 1}]

    def test_share_root_to_share_root(self):
        f = {"share": "Documents", "path": "Docs/a.txt", "item": 0}
        self.assertEqual(file_target(f, self.items, ""), ("nas:Documents", "Docs/a.txt"))

    def test_subfolder_keeps_its_relative_path(self):                      # H13: dest is per shared folder, no wrapper, no flattening
        f = {"share": "Photos", "path": "2024/Trip/DSC_0001.NEF", "item": 1}
        self.assertEqual(file_target(f, self.items, ""), ("/Volumes/Photos/Trip", "2024/Trip/DSC_0001.NEF"))
        f = {"share": "Photos", "path": "2024/Trip/raw/DSC_0002.NEF", "item": 1}
        self.assertEqual(file_target(f, self.items, ""), ("/Volumes/Photos/Trip", "2024/Trip/raw/DSC_0002.NEF"))

    def test_single_file_item(self):
        f = {"share": "homes", "path": "alice/notes.txt", "item": 2}
        self.assertEqual(file_target(f, self.items, ""), ("nas:homes/alice", "alice/notes.txt"))

    def test_item_without_dest_uses_common_dest(self):
        f = {"share": "Backups", "path": "x/y.bin", "item": 3}
        self.assertEqual(file_target(f, self.items, "nas:Restore/r1"), ("nas:Restore/r1", "Backups/x/y.bin"))

    def test_roots(self):
        self.assertEqual(restore_roots(self.items, "nas:Restore/r1"), ["nas:Documents", "/Volumes/Photos/Trip", "nas:homes/alice", "nas:Restore/r1"])
        self.assertEqual(restore_roots(self.items[:3], ""), ["nas:Documents", "/Volumes/Photos/Trip", "nas:homes/alice"])
        self.assertEqual(restore_roots([{"share": "a", "path": "", "dest": "/x/"}, {"share": "b", "path": "", "dest": "/x"}], ""), ["/x"])


class FakePool:
    """Writes '<share>:<path>' as the file content; offsets are negative so no bucket prefetch happens."""
    def __init__(self): self.extracted = []
    def plan(self, off): return [], None
    def bucket_name(self, bk, kind): return f"{bk}.bucket"
    def extract(self, off, out, size=0, mtime=0, job=None, fh_cache=None):
        self.extracted.append(out)
        with open(out, "w") as fh: fh.write(f"content {off}")
        os.utime(out, (mtime, mtime))


class FakeBackup(hbk_explorer.Backup):
    pool = None                      # shadows the property; the real one needs a pool map
    def __init__(self, src, files):
        self.src = src; self.pool = FakePool(); self._files = files
    def conn(self, share, ver):
        """An in-memory 'files' table with the columns restore_count() reads, filled from self._files for that share."""
        import sqlite3
        c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
        c.execute("create table files(path text, name text, dir text, size integer, mode integer, is_dir integer)")
        for f in self._files:
            if f["share"] != share: continue
            c.execute("insert into files values(?,?,?,?,?,?)", (f["path"], f["path"].rsplit("/", 1)[-1], f["path"].rsplit("/", 1)[0] if "/" in f["path"] else "", f["size"], 0o100644, 0))
        return c
    def restore_plan(self, items, skip_meta=True):
        out = []
        for idx, it in enumerate(items):
            base = it["path"].strip("/")
            for f in self._files:
                if f["share"] != it["share"]: continue
                if it.get("is_dir") and base and not f["path"].startswith(base + "/"): continue
                if not it.get("is_dir") and f["path"] != it["path"]: continue
                out.append(dict(f, item=idx, ver=int(it["ver"])))
        return out


FILES = [{"share": "A", "path": "x.txt", "size": 9, "mtime": 1700000000, "off": -1},
         {"share": "A", "path": "sub/y.txt", "size": 9, "mtime": 1700000001, "off": -2},
         {"share": "B", "path": "docs/z.txt", "size": 9, "mtime": 1700000002, "off": -3},
         {"share": "B", "path": "docs/deeper/w.txt", "size": 9, "mtime": 1700000003, "off": -4},
         {"share": "B", "path": "other/v.txt", "size": 9, "mtime": 1700000004, "off": -5}]


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); t = self.tmp.name
        self.saved = (hbk_explorer.INDEX_DIR, hbk_explorer.CACHE_DIR, hbk_explorer.JOBS_DIR)
        hbk_explorer.INDEX_DIR = hbk_explorer.Path(t) / "index"; hbk_explorer.CACHE_DIR = hbk_explorer.Path(t) / "cache"; hbk_explorer.JOBS_DIR = hbk_explorer.Path(t) / "jobs"
        self.src = hbk_explorer.Source("t8", "t8", os.path.join(t, "mirror", "backup_1.hbk"), ["nas:Backups/backup_1.hbk"])
        self.b = FakeBackup(self.src, FILES)
        self.dA = os.path.join(t, "ShareA"); self.dB = os.path.join(t, "Docs"); os.makedirs(self.dA); os.makedirs(self.dB)
        self.items = [{"share": "A", "ver": 1234, "path": "", "is_dir": 1, "dest": self.dA},
                      {"share": "B", "ver": 1234, "path": "docs", "is_dir": 1, "dest": self.dB}]
    def tearDown(self):
        hbk_explorer.INDEX_DIR, hbk_explorer.CACHE_DIR, hbk_explorer.JOBS_DIR = self.saved; self.tmp.cleanup()
    def job(self, label="restore"):
        return {"id": "j" + label[:3], "label": label, "started": time.time(), "state": "running"}


class MappedRestoreTests(_Fixture):
    def test_two_folders_two_destinations_exact_paths(self):
        res = self.b.restore(self.items, "", self.job())
        self.assertEqual((res["files"], res["restored"], res["skipped"], res["errors"]), (4, 4, 0, 0))
        for rel in [os.path.join(self.dA, "x.txt"), os.path.join(self.dA, "sub", "y.txt"), os.path.join(self.dB, "docs", "z.txt"), os.path.join(self.dB, "docs", "deeper", "w.txt")]:
            self.assertTrue(os.path.isfile(rel), rel)
        self.assertEqual(len(self.b.pool.extracted), 4)
        self.assertTrue(all(".hbk-restore-" in x for x in self.b.pool.extracted))                    # extracted into the per-root staging folder
        self.assertFalse(os.path.exists(os.path.join(self.dB, "other")))                     # only the selected subtree (H13)
        self.assertFalse(os.path.exists(os.path.join(self.dA, "A")))                         # no share or version folder
        self.assertEqual(int(os.stat(os.path.join(self.dB, "docs", "deeper", "w.txt")).st_mtime), 1700000003)

    def test_existing_file_is_skipped_per_root(self):
        os.makedirs(os.path.join(self.dB, "docs", "deeper")); open(os.path.join(self.dB, "docs", "deeper", "w.txt"), "w").write("mine")
        res = self.b.restore(self.items, "", self.job())
        self.assertEqual((res["restored"], res["skipped"]), (4, 1))                      # "restored" counts handled files, skipped included
        self.assertEqual(len(self.b.pool.extracted), 3)
        self.assertEqual(open(os.path.join(self.dB, "docs", "deeper", "w.txt")).read(), "mine")

    def test_mapping_persisted_and_resumed(self):
        job = self.job(); job["cancel"] = True                                               # pause before the first file
        res = self.b.restore(self.items, "", job)
        self.assertTrue(res["paused"]); self.assertEqual(res["next_index"], 0)
        saved = json.loads((hbk_explorer.JOBS_DIR / "jres.json").read_text())
        self.assertEqual(saved["state"], "paused"); self.assertEqual([it["dest"] for it in saved["items"]], [self.dA, self.dB])
        self.assertEqual(saved["roots"], [self.dA, self.dB])
        # resume the way /api/restore/resume does: items (with their dest) and next_index from the job file
        b2 = FakeBackup(self.src, FILES)
        res = b2.restore(saved["items"], saved["dest"], self.job("resumed"), saved.get("skip_meta", True), start_at=saved["next_index"])
        self.assertEqual(res["restored"], 4)
        self.assertTrue(os.path.isfile(os.path.join(self.dB, "docs", "deeper", "w.txt")))

    def test_mixed_mapped_and_common_dest(self):
        common = os.path.join(self.tmp.name, "rest"); os.makedirs(common)
        items = [self.items[0], {"share": "B", "ver": 1234, "path": "", "is_dir": 1}]
        res = self.b.restore(items, common, self.job())
        self.assertEqual(res["restored"], 5)
        self.assertTrue(os.path.isfile(os.path.join(common, "B", "other", "v.txt")))         # one-folder layout keeps <share>/<path>
        self.assertTrue(os.path.isfile(os.path.join(self.dA, "x.txt")))


class RestoreCountTests(_Fixture):
    def test_counts_match_the_plan(self):                                  # H13: live "Selected: N files · size" without materialising rows
        self.assertEqual(self.b.restore_count(self.items), {"files": 4, "bytes": 36})
        self.assertEqual(self.b.restore_count([{"share": "B", "ver": 2333, "path": "", "is_dir": 1}]), {"files": 3, "bytes": 27})
        self.assertEqual(self.b.restore_count([{"share": "B", "ver": 2333, "path": "docs/z.txt", "is_dir": 0}]), {"files": 1, "bytes": 9})
        self.assertEqual(self.b.restore_count([]), {"files": 0, "bytes": 0})

    def test_prefix_is_exact_and_case_sensitive(self):                         # H17: LIKE would match Foto_2024 -> FotoX2024, Photos -> photos
        files = [{"share": "A", "path": p, "size": 1, "mtime": 0, "off": -1} for p in ("Foto_2024/a.jpg", "FotoX2024/b.jpg", "Photos/c.jpg", "photos/d.jpg", "100%/e.txt", "100x/f.txt", "Photos.old/g.jpg")]
        b = FakeBackup(self.src, files)
        for folder, want in (("Foto_2024", 1), ("Photos", 1), ("photos", 1), ("100%", 1), ("", 7)):
            self.assertEqual(b.restore_count([{"share": "A", "ver": 1, "path": folder, "is_dir": 1}])["files"], want, folder)

    def test_skip_meta_in_sql(self):
        files = FILES + [{"share": "A", "path": "@eaDir/x.txt", "size": 5, "mtime": 0, "off": -9}, {"share": "A", "path": "a/@eaDir/SYNOPHOTO_THUMB.jpg", "size": 5, "mtime": 0, "off": -9},
                         {"share": "A", "path": "doc.docx@SynoResource", "size": 5, "mtime": 0, "off": -9}, {"share": "A", "path": "sub/.DS_Store", "size": 5, "mtime": 0, "off": -9}]
        b = FakeBackup(self.src, files); sel = [{"share": "A", "ver": 2333, "path": "", "is_dir": 1}]
        self.assertEqual(b.restore_count(sel, skip_meta=True), {"files": 2, "bytes": 18})
        self.assertEqual(b.restore_count(sel, skip_meta=False), {"files": 6, "bytes": 38})


class RestoreApiTests(_Fixture):
    def setUp(self):
        super().setUp()
        hbk_explorer.SOURCES["t8"] = self.src; hbk_explorer.BACKUPS["t8"] = self.b
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), hbk_explorer.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); hbk_explorer.SOURCES.pop("t8", None); hbk_explorer.BACKUPS.pop("t8", None); super().tearDown()
    def post(self, path, body):
        req = Request(f"http://127.0.0.1:{self.srv.server_address[1]}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(req) as r: return r.status, json.loads(r.read())
        except Exception as e:
            return e.code, json.loads(e.read())
    def get(self, path):
        with urlopen(f"http://127.0.0.1:{self.srv.server_address[1]}{path}") as r: return json.loads(r.read())

    def test_mapped_target_inside_backup_is_refused(self):
        bad = [dict(self.items[0]), dict(self.items[1], dest=os.path.join(self.src.local, "Pool"))]
        code, out = self.post("/api/restore", {"src": "t8", "dest": "", "items": bad})
        self.assertEqual(code, 400); self.assertIn("Pool", out["error"])
        code, out = self.post("/api/restore", {"src": "t8", "dest": "", "items": [dict(self.items[0], dest="nas:Backups")]})   # the backup lies inside it
        self.assertEqual(code, 400); self.assertIn("nas:Backups", out["error"])

    def test_unmapped_item_needs_common_dest(self):
        code, out = self.post("/api/restore", {"src": "t8", "dest": "", "items": [{"share": "B", "ver": 1234, "path": "", "is_dir": 1}]})
        self.assertEqual(code, 400); self.assertIn("no destination for B", out["error"])

    def test_new_local_root_is_created_and_restore_runs(self):
        new = os.path.join(self.tmp.name, "NewShare")
        code, j = self.post("/api/restore", {"src": "t8", "dest": "", "items": [dict(self.items[0], dest=new + "/"), self.items[1]]})
        self.assertEqual(code, 200)
        for _ in range(100):
            job = self.get(f"/api/job?id={j['id']}")
            if job["state"] != "running": break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job)
        self.assertTrue(os.path.isfile(os.path.join(new, "sub", "y.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.dB, "docs", "z.txt")))
        saved = self.get("/api/jobs/saved")[0]
        self.assertEqual(saved["roots"], [new, self.dB])

    def test_plan_mappings_preview(self):
        out = self.get("/api/restore_plan?src=t8&dest=&items=" + json.dumps(self.items).replace(" ", "%20").replace('"', "%22"))
        self.assertEqual(out["files"], 4)
        self.assertEqual(out["mappings"][1]["sample_to"], os.path.join(self.dB, "docs", "z.txt"))
        self.assertEqual(out["mappings"][1]["sample_from"], "B/docs/z.txt")
        self.assertEqual((out["mappings"][0]["files"], out["mappings"][0]["dest"]), (2, self.dA))

    def test_plan_mappings_one_row_per_share(self):
        items = self.items + [{"share": "B", "ver": 1234, "path": "other", "is_dir": 1, "dest": self.dB}]
        out = self.get("/api/restore_plan?src=t8&dest=&items=" + json.dumps(items).replace(" ", "%20").replace('"', "%22"))
        self.assertEqual([m["share"] for m in out["mappings"]], ["A", "B"])
        self.assertEqual((out["mappings"][1]["paths"], out["mappings"][1]["files"]), (["docs", "other"], 3))


if __name__ == "__main__":
    unittest.main()
