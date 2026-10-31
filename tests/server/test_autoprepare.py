"""The pool preparation starts by itself (after open and after a share load) when it can, respects the user's pause, and
the pause and resume endpoints drive it."""
import json, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import urlopen, Request
from urllib.parse import urlencode

from callido_restore import paths
from callido_restore.restore.backup import Backup
from callido_restore.restore.jobs import JOBS
from callido_restore.server import api
from callido_restore.sources.source import SOURCES, Source


class AutoPrepare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.old = (paths.INDEX_DIR, paths.CACHE_DIR); paths.INDEX_DIR = root / "index"; paths.CACHE_DIR = root / "cache"
        (root / "hbk").mkdir(); SOURCES["ap"] = Source("ap", "ap", str(root / "hbk")); api.BACKUPS.pop("ap", None)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), api.H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.built = []; self.release = threading.Event()        # a fake preparation runs until released or cancelled
        def build_pool(b, job, max_bucket=None):
            self.built.append(job["id"])
            while not job.get("cancel") and not self.release.is_set(): time.sleep(0.01)
            if not job.get("cancel"): b.mark_prepared(1, 1)
            return {"mapped": 1, "upto": 1}
        self.patches = [mock.patch.object(Backup, "build_pool", build_pool), mock.patch.object(Backup, "raw_snapshots", lambda b: ["/x/1234.db.5"]), mock.patch.object(api, "AUTOPREPARE", True)]
        for p in self.patches: p.start()

    def tearDown(self):
        self.release.set(); time.sleep(0.05)
        for p in self.patches: p.stop()
        self.srv.shutdown(); self.srv.server_close(); SOURCES.pop("ap", None); api.BACKUPS.pop("ap", None)
        for k in [k for k, j in JOBS.items() if j.get("src") == "ap" or j["label"] == "open backup"]: JOBS.pop(k, None)
        paths.INDEX_DIR, paths.CACHE_DIR = self.old; self.tmp.cleanup()

    def get(self, path, **q):
        with urlopen(f"{self.base}{path}?{urlencode(q)}") as r: return json.loads(r.read())
    def post(self, path, **body):
        with urlopen(Request(f"{self.base}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})) as r: return json.loads(r.read())
    def wait_job(self, jid):
        for _ in range(100):
            j = self.get("/api/job", id=jid)
            if j["state"] != "running": return j
            time.sleep(0.02)
        return j

    def test_open_starts_the_preparation_once(self):
        self.wait_job(self.get("/api/open", src="ap", job="1")["id"])
        time.sleep(0.1)
        pool_jobs = [j for j in JOBS.values() if j["label"] == "build pool map" and j.get("src") == "ap"]
        self.assertEqual(len(pool_jobs), 1); self.assertEqual(self.built, [pool_jobs[0]["id"]])
        self.wait_job(self.get("/api/open", src="ap", job="1")["id"]); time.sleep(0.1)       # a second open while it runs: no second job
        self.assertEqual(len(self.built), 1); self.assertEqual(self.get("/api/pool", src="ap")["running"], pool_jobs[0]["id"])
        self.release.set(); self.wait_job(pool_jobs[0]["id"])
        self.assertTrue(self.get("/api/pool", src="ap")["complete"])
        self.wait_job(self.get("/api/open", src="ap", job="1")["id"]); time.sleep(0.1)
        self.assertEqual(len(self.built), 1)                                                  # finished: not started again
        st = self.post("/api/pool/resume", src="ap"); self.assertTrue(st["complete"]); self.assertEqual(len(self.built), 1)   # nothing to resume

    def test_an_older_mappers_map_is_redone(self):
        api.backup_for("ap"); api.BACKUPS["ap"]._prepared_file().parent.mkdir(parents=True, exist_ok=True)
        api.BACKUPS["ap"]._prepared_file().write_text(json.dumps({"mapper": "something-old", "mapped": 5, "total": 5}))
        self.assertFalse(self.get("/api/pool", src="ap")["complete"])
        self.wait_job(self.get("/api/open", src="ap", job="1")["id"]); time.sleep(0.1)
        self.assertEqual(len(self.built), 1); self.release.set()

    def test_without_a_raw_snapshot_nothing_starts(self):
        with mock.patch.object(Backup, "raw_snapshots", lambda b: []):
            self.wait_job(self.get("/api/open", src="ap", job="1")["id"]); time.sleep(0.1)
        self.assertEqual(self.built, []); self.release.set()

    def test_pause_cancels_and_blocks_the_auto_start_until_resume(self):
        self.wait_job(self.get("/api/open", src="ap", job="1")["id"]); time.sleep(0.05)
        jid = self.built[0]; self.assertEqual(JOBS[jid]["state"], "running")
        out = self.post("/api/pool/pause", src="ap")
        self.assertTrue(out["paused"]); self.assertEqual(out["cancelled"], jid); self.wait_job(jid)
        st = self.get("/api/pool", src="ap"); self.assertTrue(st["paused"]); self.assertIsNone(st["running"])
        self.wait_job(self.get("/api/open", src="ap", job="1")["id"]); time.sleep(0.1)
        self.assertEqual(len(self.built), 1)                                                  # paused: the next open does not restart it
        j = self.post("/api/pool/resume", src="ap"); self.assertEqual(self.post("/api/pool/resume", src="ap")["id"], j["id"])   # a double click joins
        self.assertEqual(len(self.built), 2); self.assertFalse(self.get("/api/pool", src="ap")["paused"]); self.release.set(); self.wait_job(j["id"])

    def test_explicit_build_lifts_the_pause_and_status_shows_running(self):
        api.backup_for("ap").set_pool_paused(True)
        j = self.get("/api/pool/build", src="ap")
        st = self.get("/api/pool", src="ap"); self.assertEqual(st["running"], j["id"]); self.assertFalse(st["paused"])
        self.assertEqual(self.get("/api/pool/build", src="ap")["id"], j["id"])              # a second click joins the running job
        self.release.set(); self.wait_job(j["id"])

    def test_parallel_starts_make_one_job(self):
        """open, share loads and Resume may all race: the lock lets exactly one preparation through."""
        api.backup_for("ap")
        results = []
        ts = [threading.Thread(target=lambda: results.append(api.maybe_autoprepare("ap"))) for _ in range(8)]
        for t in ts: t.start()
        for t in ts: t.join()
        time.sleep(0.05)
        self.assertEqual(len(self.built), 1); self.assertEqual(len([r for r in results if r]), 1); self.release.set()

    def test_a_backup_with_its_own_chunk_table_needs_no_preparation(self):
        """Pool/chunk_index locates every chunk: /api/pool says complete, the auto-start leaves it alone, and an explicit build
        returns at once without mapping."""
        with mock.patch.object(Backup, "pool_status", lambda b: {"chunk_index": True, "complete": True, "paused": False, "map": False}):
            self.wait_job(self.get("/api/open", src="ap", job="1")["id"]); time.sleep(0.1)
            self.assertEqual(self.built, [])
        self.release.set()

    def test_off_switch(self):
        with mock.patch.object(api, "AUTOPREPARE", False):
            self.wait_job(self.get("/api/open", src="ap", job="1")["id"]); time.sleep(0.1)
        self.assertEqual(self.built, []); self.release.set()
