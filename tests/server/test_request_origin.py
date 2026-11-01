"""The local API refuses requests from other web pages (DNS rebinding, cross-site requests) and serves its own window and
command-line tools."""
import json, threading, unittest
from callido_restore.server.api import LocalServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from callido_restore.server import api


class Refusal(unittest.TestCase):
    def test_rules(self):
        r = lambda host, origin=None, site=None: api.request_refusal(host, origin, site, 8765)
        self.assertIsNone(r("127.0.0.1:8765")); self.assertIsNone(r("localhost:8765")); self.assertIsNone(r("[::1]:8765"))
        self.assertIsNone(r("127.0.0.1:8765", "http://127.0.0.1:8765", "same-origin"))     # the app's own page
        self.assertIsNone(r("127.0.0.1:8765", None, "none"))                                # typed into the address bar
        self.assertIsNotNone(r("evil.example:8765"))                                        # DNS rebinding
        self.assertIsNotNone(r("127.0.0.1:9999"))
        self.assertIsNotNone(r("127.0.0.1:8765", "https://evil.example"))                   # cross-site POST
        self.assertIsNotNone(r("127.0.0.1:8765", None, "cross-site"))                       # cross-site <img src=/api/quit>
        self.assertIsNotNone(r("127.0.0.1:8765", "http://127.0.0.1:9999"))
        self.assertIsNotNone(r(None))
        self.assertIsNotNone(r("127.0.0.1:8765", "null"))                                  # sandboxed iframe / data: page, older engine
        self.assertIsNotNone(r("127.0.0.1:8765", "null", "cross-site"))
        self.assertIsNone(r("127.0.0.1:8765", "null", "same-origin"))                       # only when the browser says same-origin
        # what the real pywebview window sends (probed): GET without Origin, POST with our origin, both same-origin
        self.assertIsNone(r("127.0.0.1:8765", None, "same-origin")); self.assertIsNone(r("127.0.0.1:8765", "http://127.0.0.1:8765", "same-origin"))

    def test_page_opened_from_a_link_elsewhere(self):
        r = lambda path, mode, dest, host="127.0.0.1:8765", origin=None: api.request_refusal(host, origin, "cross-site", 8765, path, mode, dest)
        self.assertIsNone(r("/", "navigate", "document")); self.assertIsNone(r("/index.html", "navigate", "document"))
        self.assertIsNotNone(r("/", "navigate", "iframe"))                                  # never inside another site's frame
        self.assertIsNotNone(r("/api/quit", "navigate", "document"))                        # a link to the API stays refused
        self.assertIsNotNone(r("/", "no-cors", "image"))
        self.assertIsNotNone(r("/", "navigate", "document", host="evil.example:8765"))      # DNS rebinding still refused
        self.assertIsNotNone(r("/", "navigate", "document", origin="https://evil.example")) # a form POSTed to / is not a link


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.srv = LocalServer(("127.0.0.1", 0), api.H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
    def tearDown(self): self.srv.shutdown(); self.srv.server_close()

    def get(self, path, **headers):
        try:
            with urlopen(Request(f"http://127.0.0.1:{self.port}{path}", headers=headers)) as r: return r.status, json.loads(r.read() or b"{}")
        except HTTPError as e: return e.code, json.loads(e.read() or b"{}")

    def test_own_page_and_tools_pass_others_are_refused(self):
        self.assertEqual(self.get("/api/about")[0], 200)                                              # curl, smoke test
        self.assertEqual(self.get("/api/about", Origin=f"http://127.0.0.1:{self.port}", **{"Sec-Fetch-Site": "same-origin"})[0], 200)
        code, body = self.get("/api/about", Host=f"evil.example:{self.port}")
        self.assertEqual(code, 403); self.assertIn("not this app", body["error"])
        self.assertEqual(self.get("/api/quit", **{"Sec-Fetch-Site": "cross-site"})[0], 403)           # and the app is still up
        self.assertEqual(self.get("/api/about")[0], 200)
        nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        with urlopen(Request(f"http://127.0.0.1:{self.port}/", headers=nav)) as r: self.assertEqual(r.status, 200)  # opened from a link
        self.assertEqual(self.get("/api/quit", **nav)[0], 403)
        req = Request(f"http://127.0.0.1:{self.port}/api/job/cancel", data=b"{}", headers={"Origin": "https://evil.example", "Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as cm: urlopen(req)
        self.assertEqual(cm.exception.code, 403)
