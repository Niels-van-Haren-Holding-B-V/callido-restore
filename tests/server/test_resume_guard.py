"""/api/restore/resume never starts a second restore: 409 while that job or any restore runs (hbk-4 on #187)."""
import json, tempfile, threading, unittest
from callido_restore.server.api import LocalServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from callido_restore import paths
from callido_restore.server import api


class ResumeGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.old = paths.JOBS_DIR; paths.JOBS_DIR = Path(self.tmp.name)
        (paths.JOBS_DIR / "6135a4c2a9").with_suffix(".json").write_text(json.dumps({"id": "6135a4c2a9", "state": "paused", "src": "x", "items": []}))
        self.srv = LocalServer(("127.0.0.1", 0), api.H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]; self.saved = dict(api.JOBS)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); api.JOBS.clear(); api.JOBS.update(self.saved)
        paths.JOBS_DIR = self.old; self.tmp.cleanup()

    def post(self, path, body):
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), method="POST",
                      headers={"Origin": f"http://127.0.0.1:{self.port}", "Sec-Fetch-Site": "same-origin"})
        try:
            with urlopen(req) as r: return r.status, json.loads(r.read() or b"{}")
        except HTTPError as e: return e.code, json.loads(e.read() or b"{}")

    def test_refused_while_any_restore_runs(self):
        api.JOBS["live"] = {"id": "live", "label": "restore", "state": "running"}
        code, out = self.post("/api/restore/resume", {"id": "6135a4c2a9"})
        self.assertEqual(code, 409); self.assertIn("already running", out["error"])
        self.assertTrue((paths.JOBS_DIR / "6135a4c2a9.json").is_file())          # the saved job is kept for later

    def test_refused_while_that_job_runs(self):
        api.JOBS["6135a4c2a9"] = {"id": "6135a4c2a9", "label": "restore (resumed)", "state": "running"}
        self.assertEqual(self.post("/api/restore/resume", {"id": "6135a4c2a9"})[0], 409)

    def test_other_jobs_do_not_block(self):
        api.JOBS["idx"] = {"id": "idx", "label": "index v1 Documents", "state": "running"}
        code, out = self.post("/api/restore/resume", {"id": "6135a4c2a9"})
        self.assertNotEqual(code, 409, out)


if __name__ == "__main__":
    unittest.main()
