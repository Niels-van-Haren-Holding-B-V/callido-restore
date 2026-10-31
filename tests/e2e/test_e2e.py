"""End to end on a synthetic backup with a real pool (tests/e2e/hbkgen.py): open, file lists, preparation (the real anchor-first
mapper, in-process), restore of whole runs, compared byte for byte and by mtime with what the generator put in.

Fast (~2 s) and offline: no rclone, no remote, nothing under ~/.hbk-explorer. The run through the real app process and its HTTP
API is tests/e2e/test_app.py (HBK_E2E=1)."""
import shutil, tempfile, unittest

from tests.e2e import flow


class FullFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="hbk-e2e-")
        cls.rep = flow.run_inprocess(cls.work, log=lambda m: None)
        cls.m = cls.rep["manifest"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def restore_ok(self, ver):
        r = self.rep["restores"][ver]
        self.assertEqual(r["errors"], [], flow.summary(self.rep))
        for k in ("missing", "wrong_bytes", "wrong_mtime", "extra"): self.assertEqual(r[k], [], f"v{ver} {k}")
        self.assertEqual(r["ok"], len([1 for sh in self.m["versions"][str(ver)].values() for _ in sh]))

    def test_open_reads_task_shares_and_runs(self):
        o = self.rep["open"]
        self.assertTrue(o["readable"]); self.assertEqual(o["problems"], [])
        self.assertEqual(o["shares"], ["Documents", "Photos", "homes"]); self.assertEqual(o["versions"], [101, 102, 103])

    def test_preparation_finishes(self):
        self.assertTrue(self.rep["pool_status"]["map"]); self.assertTrue(self.rep["pool_status"]["complete"])
        self.assertEqual(self.rep["map"]["guessed"], 0)

    def test_last_clean_run_restores_byte_exact(self):
        self.restore_ok(self.m["last_clean"])

    def test_first_run_restores_byte_exact(self):
        self.restore_ok(min(int(v) for v in self.m["versions"]))     # older content of the file modified in run 2 included

    def test_rerun_writes_nothing_and_keeps_changed_files(self):         # T8
        rr = self.rep["rerun"]
        self.assertEqual(rr["changed"], []); self.assertEqual(rr["new_files"], []); self.assertTrue(rr["victim_kept"])
        self.assertEqual(rr["result"]["skipped"], rr["result"]["files"])

    def test_rerun_does_not_count_skipped_files_as_restored(self):
        """E2 (fixed in #117): 'Done: 25 restored · 25 skipped' after a re-run that restored nothing."""
        rr = self.rep["rerun"]["result"]
        self.assertEqual(rr["restored"], 0)

    def test_every_bucket_is_mapped(self):
        """E1 (fixed in #118): the last record of each writer's stream that spills into a following bucket has no next anchor;
        it used to stay unplaced and its tail bucket unowned (buckets 33 and 34, run 103)."""
        self.assertEqual(self.rep["pool_status"]["mapped_buckets"], self.rep["pool_status"]["total_buckets"])

    def test_newest_run_restores_completely(self):          # E1, fixed in #118
        """The newest run is the one most users restore; its big files are the tails of the writer streams. Here it is the
        incident run, but a clean newest run has the same layout."""
        from callido_restore import paths
        from callido_restore.restore.backup import Backup
        from callido_restore.sources.source import Source
        from pathlib import Path
        rep, work = self.rep, Path(self.work)
        old = (paths.CONF_DIR, paths.INDEX_DIR, paths.CACHE_DIR, paths.JOBS_DIR)
        paths.CONF_DIR = work / "conf"; paths.INDEX_DIR = work / "conf" / "index"; paths.CACHE_DIR = work / "conf" / "cache"; paths.JOBS_DIR = work / "conf" / "jobs"
        try:
            b = Backup(Source("e2e", "e2e", str(work / "e2e_1.hbk"))); ver = rep["manifest"]["incident"]; dest = work / "restore-newest"; dest.mkdir()
            job = {"id": "newest", "label": "restore", "started": 0}
            b.restore([{"share": sh, "ver": ver, "path": "", "is_dir": True} for sh in b.shares], str(dest), job)
            self.assertGreater(job["total_files"], 20)
            self.assertEqual(job["errors"], [])
            cmp_ = flow.compare(rep["manifest"], ver, {sh: dest / sh for sh in b.shares})
            self.assertEqual(cmp_["missing"] + cmp_["wrong_bytes"], [])
        finally:
            paths.CONF_DIR, paths.INDEX_DIR, paths.CACHE_DIR, paths.JOBS_DIR = old



class CrossBucketTags(unittest.TestCase):
    def test_a_tag_that_straddles_a_bucket_boundary_is_anchored(self):
        """E4 (fixed in #118): anchor._scan_entries matches a tag only inside one bucket (`if i + K > n: break`); a file whose
        first K chunks run into the writer's next bucket gets no anchor (and the record before it cannot close its spill).
        At scale 6 with 1 MiB buckets the generator has two such files in run 101."""
        work = tempfile.mkdtemp(prefix="hbk-e2e-x-")
        try:
            rep = flow.run_inprocess(work, scale=6, bucket_kb=1024)
            r = rep["restores"][min(int(v) for v in rep["manifest"]["versions"])]
            self.assertEqual(r["missing"], [], flow.summary(rep))
        finally:
            shutil.rmtree(work, ignore_errors=True)



    def test_a_straddling_tag_with_an_identical_copy(self):
        """hbk-6 finding E7: at scale 0.5 with 512 KiB buckets thesis.docx starts at bucket 2 entry 41/49 with K=9, so its tag
        straddles into bucket 7 and the anchor scan finds no position for it. Without positions the phantom pass never sees
        that run 102's thesis-copy.docx is an identical copy (phantom-of-tagged 0); the copy keeps its chunk count, the gap
        has no solution, and both files are missing from every run."""
        work = tempfile.mkdtemp(prefix="hbk-e2e-x-")
        try:
            rep = flow.run_inprocess(work, scale=0.5, bucket_kb=512)
            for ver, r in rep["restores"].items(): self.assertEqual(r["missing"], [], f"v{ver}: " + flow.summary(rep))
        finally:
            shutil.rmtree(work, ignore_errors=True)


class Rotation(unittest.TestCase):
    """Version rotation (hbk-1's mechanism, 2026-10-31): a rotated-away run, its unneeded buckets purged, the freed bucket ids
    reused by one writer while the others get fresh ones (same revision), so (revision, id) is not fill order for those."""
    def run_flow(self, **kw):
        work = tempfile.mkdtemp(prefix="hbk-e2e-rot-")
        try: return flow.run_inprocess(work, scale=1, bucket_kb=256, rotate=True, **kw)
        finally: shutil.rmtree(work, ignore_errors=True)

    def check(self, rep):
        self.assertTrue(rep["manifest"]["truth"]["reused"], "the scenario must reuse bucket ids")
        for ver, r in rep["restores"].items():
            self.assertEqual(r["wrong_bytes"], [], ver); self.assertEqual(r["missing"], [], f"v{ver}: " + flow.summary(rep))

    def test_reused_bucket_ids_restore_every_run(self):
        self.check(self.run_flow(rotate_copy=False))

    def test_copy_of_a_rotated_original_keeps_its_neighbours(self):
        """hbk-6 finding E6: run 102 copies a file that only a rotated-away run had. Its tag matches the original's entries in an
        older bucket (rev 100), so it is anchored behind in time; the record before it on the same writer (IMG_4107.jpg,
        spilling 19 -> 29 in run 101) then has its next anchor earlier than itself, gets spill-no-solution and is lost from
        every run. The copy itself restores."""
        self.check(self.run_flow(rotate_copy=True))


if __name__ == "__main__":
    unittest.main()
