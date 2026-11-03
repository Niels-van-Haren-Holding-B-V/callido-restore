"""The Synology package's CGI (packaging/synology/spk/src/app/dsm_cgi.py): DSM's web server runs it, it lets in a DSM
administrator only, and forwards to the app on the NAS's loopback, which then serves the page with the CGI's prefix."""
import importlib.util, io, json, os, threading, unittest
from pathlib import Path
from unittest import mock

from callido_restore.server import api
from callido_restore.server.api import H, LocalServer

if os.name == "nt": raise unittest.SkipTest("the package's CGI runs on a Synology only (it uses grp/pwd)")

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("dsm_cgi", ROOT / "packaging/synology/spk/src/app/dsm_cgi.py")
cgi = importlib.util.module_from_spec(spec); spec.loader.exec_module(cgi)


class CGI(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"CALLIDO_DSM": "1"}); p.start(); self.addCleanup(p.stop)
        self.srv = LocalServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close); self.addCleanup(self.srv.shutdown)
        self.addCleanup(setattr, cgi, "APP", cgi.APP); cgi.APP = ("127.0.0.1", self.srv.server_address[1])

    def run_cgi(self, path, auth=lambda env: "admin", method="GET", body=b"", query="", **env):
        out = io.BytesIO(); seen = {}
        def a(e): seen.update(e); return auth(e)
        e = {"PATH_INFO": path, "REQUEST_METHOD": method, "QUERY_STRING": query, "CONTENT_LENGTH": str(len(body)), **env}
        with mock.patch.object(cgi.sys, "stdout", mock.Mock(buffer=out)):
            cgi.main(env=e, stdin=io.BytesIO(body), auth=a)
        head, _, data = out.getvalue().partition(b"\r\n\r\n")
        return head.decode(), data, seen

    def test_the_page_comes_with_the_prefix(self):
        head, page, _ = self.run_cgi("/", auth=lambda e: None)                # the page holds no data: no session needed
        self.assertIn("Status: 200", head); self.assertIn(b'window.CALLIDO_PREFIX="/webman/3rdparty/callido-restore/index.cgi"', page)

    def test_api_needs_a_dsm_administrator(self):
        head, body, _ = self.run_cgi("/api/sources", auth=lambda e: None)
        self.assertIn("Status: 401", head); self.assertTrue(json.loads(body)["dsm"])
        head, body, _ = self.run_cgi("/api/sources")
        self.assertIn("Status: 200", head); self.assertEqual(json.loads(body), [])

    def test_the_token_reaches_the_check_and_not_the_app(self):
        _, _, seen = self.run_cgi("/api/sources", query="SynoToken=T0K&x=1")
        self.assertEqual(seen["HTTP_X_SYNO_TOKEN"], "T0K")
        _, _, seen = self.run_cgi("/api/sources", HTTP_X_SYNO_TOKEN="H3D")
        self.assertEqual(seen["HTTP_X_SYNO_TOKEN"], "H3D")

    def test_post_with_a_body_and_quit_is_refused(self):
        head, body, _ = self.run_cgi("/api/app/settings", method="POST", body=b'{"tray": false}', CONTENT_TYPE="application/json")
        self.assertIn("Status: 200", head)
        head, _, _ = self.run_cgi("/api/quit"); self.assertIn("Status: 409", head)      # the NAS app keeps running

    def test_app_not_running(self):
        cgi.APP = ("127.0.0.1", 9)
        head, body, _ = self.run_cgi("/api/sources"); self.assertIn("Status: 503", head)

    def test_authenticate_cgi_result(self):
        class R:
            def __init__(s, out, rc=0): s.stdout, s.returncode, s.stderr = out, rc, ""
        with mock.patch.object(cgi.grp, "getgrall", return_value=[mock.Mock(gr_name="administrators", gr_mem=["anna"])]), \
             mock.patch.object(cgi.pwd, "getpwnam", side_effect=KeyError):
            self.assertEqual(cgi.dsm_admin({"HTTP_COOKIE": "id=x"}, run=lambda *a, **k: R("anna\n")), "anna")
            self.assertIsNone(cgi.dsm_admin({"HTTP_COOKIE": "id=x"}, run=lambda *a, **k: R("bob\n")))     # not an administrator
            self.assertIsNone(cgi.dsm_admin({}, run=lambda *a, **k: R("", 1)))                           # no session
            seen = {}
            cgi.dsm_admin({"HTTP_X_SYNO_TOKEN": "T", "HTTP_COOKIE": "id=x"}, run=lambda a, env=None, **k: (seen.update(env), R(""))[1])
            self.assertEqual((seen["QUERY_STRING"], seen["HTTP_COOKIE"]), ("SynoToken=T", "id=x"))


class Page(unittest.TestCase):
    def test_prefix_only_in_dsm_mode_and_only_a_3rdparty_cgi(self):
        html = b"<html><head><title>x</title></head></html>"
        with mock.patch.dict(os.environ, {"CALLIDO_DSM": "1"}):
            self.assertIn(b"CALLIDO_PREFIX", api.dsm_page(html, "/webman/3rdparty/callido-restore/index.cgi"))
            for bad in (None, "", "/evil", "https://evil.example/x.cgi", '/webman/3rdparty/a/b.cgi"</script>'):
                self.assertEqual(api.dsm_page(html, bad), html, bad)
        with mock.patch.dict(os.environ, {"CALLIDO_DSM": ""}):
            self.assertEqual(api.dsm_page(html, "/webman/3rdparty/callido-restore/index.cgi"), html)


if __name__ == "__main__": unittest.main()
