"""SMB connect feedback (Niels on Windows: "slow, no progress, not sure it went well"): the host as people type it, live
elapsed-time messages while rclone works, and the API's early checks."""
import json, threading, time, unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from callido_restore.server.api import H
from callido_restore.sources import rclone


class SmbHost(unittest.TestCase):
    def test_forms_people_type(self):
        for raw, want in [("nas", "nas"), ("  192.0.2.10 ", "192.0.2.10"), ("\\\\nas", "nas"), ("\\\\nas\\Restore\\x", "nas"),
                          ("//nas/Restore", "nas"), ("smb://nas.local/Restore", "nas.local"), ("SMB://user@nas", "nas"), ("", ""), (None, "")]:
            self.assertEqual(rclone.smb_host(raw), want, raw)


class Elapsed(unittest.TestCase):
    def test_message_counts_up_and_result_passes_through(self):
        job, seen = {}, []
        def work():
            for _ in range(30):
                seen.append(job.get("message")); time.sleep(0.01)
            return 42
        self.assertEqual(rclone.with_elapsed(job, "Listing the shares on nas…", work, every=0.05), 42)
        self.assertEqual(seen[0], "Listing the shares on nas…")
        self.assertTrue(any(m and m.endswith(" s") for m in seen), seen)

    def test_errors_pass_through_and_ticking_stops(self):
        job = {}
        with self.assertRaises(RuntimeError): rclone.with_elapsed(job, "x", lambda: (_ for _ in ()).throw(RuntimeError("boom")), every=0.01)
        before = job["message"]; time.sleep(0.05)
        self.assertEqual(job["message"], before)


class ConnectApi(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
    def post(self, body):
        req = Request(f"http://127.0.0.1:{self.srv.server_port}/api/rclone/connect", data=json.dumps(body).encode(), method="POST")
        try:
            with urlopen(req, timeout=10) as r: return r.status, json.loads(r.read())
        except HTTPError as e: return e.code, json.loads(e.read())

    def test_empty_host_is_refused_before_any_rclone_runs(self):
        with mock.patch.object(rclone, "remotes", return_value=[]), mock.patch("subprocess.run") as run:
            code, out = self.post({"kind": "smb", "name": "nas", "host": " \\\\ ", "user": "u", "password": "p"})
        self.assertEqual(code, 400); self.assertIn("NAS address", out["error"]); run.assert_not_called()

    def test_password_is_obscured_inside_the_job_not_before_it(self):
        # the request returns a job id at once; rclone (obscure, config create, listing) only runs inside the job
        with mock.patch.object(rclone, "remotes", return_value=[]), mock.patch("subprocess.run") as run, \
             mock.patch("callido_restore.server.api.start_job", return_value={"id": "j1"}) as sj:
            code, out = self.post({"kind": "smb", "name": "nas", "host": "\\\\nas\\Restore", "user": "u", "password": "p"})
        self.assertEqual((code, out), (200, {"id": "j1"})); run.assert_not_called(); sj.assert_called_once()


if __name__ == "__main__":
    unittest.main()
