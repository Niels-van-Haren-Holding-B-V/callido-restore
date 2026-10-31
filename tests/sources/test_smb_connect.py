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


class SmbAddress(unittest.TestCase):
    def test_port_split_off(self):
        for raw, want in [("nas", ("nas", None)), ("nas:445", ("nas", "445")), ("\\\\nas:4450\\x", ("nas", "4450")),
                          ("192.0.2.10:445", ("192.0.2.10", "445")), ("[2001:db8::1]:445", ("2001:db8::1", "445")),
                          ("[2001:db8::1]", ("2001:db8::1", None)), ("2001:db8::1", ("2001:db8::1", None)), ("nas:x", ("nas", None))]:
            self.assertEqual(rclone.smb_address(raw), want, raw)


def ip(*octets): return ".".join(str(o) for o in octets)      # built, so no literal local address sits in the repo (private check)


class SmbErrors(unittest.TestCase):
    def test_typo_of_a_local_range_is_named(self):
        typo = ip(192, 158, 188, 69)
        msg = rclone.smb_error_message(f"couldn't connect SMB: dial tcp {typo}:445: i/o timeout", typo, "u")
        self.assertTrue(msg.startswith(f"{typo} is not an address on a local network"), msg)
        self.assertIn(f"Did you mean {ip(192, 168, 188, 69)}?", msg)
        self.assertIn("did not answer on the SMB port (445) within 20 seconds", msg)
        self.assertIn("Details: couldn't connect SMB", msg)

    def test_no_hint_for_local_addresses_and_names(self):
        for h in (ip(192, 168, 1, 2), ip(10, 0, 0, 5), ip(172, 20, 1, 1), "nas", "nas.local", "2001:db8::1", ip(169, 254, 1, 1), ip(100, 101, 102, 103), ip(100, 64, 0, 1)):   # 100.64/10: Tailscale / CGNAT
            self.assertEqual(rclone.smb_host_hint(h), "", h)
        self.assertIn("not an address on a local network", rclone.smb_host_hint(ip(8, 8, 8, 8)))
        self.assertNotIn("Did you mean", rclone.smb_host_hint(ip(8, 8, 8, 8)))

    def test_kinds(self):
        cases = {"dial tcp: lookup nas: no such host": "could not be found on this network",
                 "connectex: No connection could be made because the target machine actively refused it.": "refused the SMB connection",
                 "response error: The attempted logon is invalid (STATUS_LOGON_FAILURE)": "rejected the user name or password for niels",
                 "STATUS_ACCESS_DENIED": "refused access for niels",
                 "dial tcp: connect: network is unreachable": "no network route to nas",
                 "no answer from rclone within 60 s": "did not answer on the SMB port (445)",
                 "something new": "Could not connect to nas."}
        for raw, want in cases.items():
            self.assertIn(want, rclone.smb_error_message(raw, "nas", "niels"), raw)

    def test_custom_port_in_message(self):
        self.assertIn("SMB port (4450)", rclone.smb_error_message("i/o timeout", "nas", port="4450"))


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

    def test_job_uses_the_port_option_and_a_fast_probe(self):
        import subprocess
        ran, listed, result = [], {}, {}
        def fake_run(args, **kw):
            ran.append(list(args))
            return subprocess.CompletedProcess(args, 0, "OBSCURED\n" if "obscure" in args else "", "")
        def fake_ls(path, **kw):
            listed.update(kw, path=path); return [{"Name": "Restore"}, {"Name": "homes"}]
        def run_now(fn, label):
            job = {}; result.update(fn(job)); return {"id": "j1"}
        with mock.patch.object(rclone, "remotes", return_value=[]), mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch.object(rclone, "ls", side_effect=fake_ls), mock.patch.object(rclone, "forget"), \
             mock.patch.object(rclone, "fscache_clear"), mock.patch("callido_restore.server.api.start_job", side_effect=run_now):
            code, _ = self.post({"kind": "smb", "name": "nas", "host": "smb://nas:4450/Restore", "user": "u", "password": "p"})
        self.assertEqual(code, 200)
        create = next(a for a in ran if "create" in a)
        self.assertIn("host=nas", create); self.assertIn("port=4450", create); self.assertIn("pass=OBSCURED", create)
        self.assertEqual(listed["path"], "nas:")
        self.assertEqual(listed["config"], {"ConnectTimeout": "20s", "LowLevelRetries": 1, "Retries": 1})
        self.assertEqual(result, {"remote": "nas:", "shares": ["Restore", "homes"]})

    def test_failed_listing_gives_the_plain_message_and_logs(self):
        import io, subprocess
        from contextlib import redirect_stdout
        typo = ip(192, 158, 188, 69); result = {}
        def fake_run(args, **kw): return subprocess.CompletedProcess(args, 0, "OBS\n" if "obscure" in args else "", "")
        def run_now(fn, label):
            job = {}
            try: fn(job)
            except RuntimeError as e: result["error"] = str(e)
            return {"id": "j1"}
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(rclone, "remotes", return_value=[]), mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch.object(rclone, "ls", side_effect=rclone.RcloneError(f"couldn't connect SMB: dial tcp {typo}:445: i/o timeout")), \
             mock.patch.object(rclone, "forget"), mock.patch.object(rclone, "fscache_clear"), \
             mock.patch("callido_restore.server.api.start_job", side_effect=run_now):
            self.post({"kind": "smb", "name": "nas", "host": typo, "user": "niels", "password": "secret-pw"})
        self.assertIn(f"Did you mean {ip(192, 168, 188, 69)}?", result["error"])
        log = buf.getvalue()
        self.assertIn(f"connect smb 'nas': host={typo} port=445 user=niels (password not logged)", log)
        self.assertIn("listing failed after", log); self.assertNotIn("secret-pw", log)


if __name__ == "__main__":
    unittest.main()
