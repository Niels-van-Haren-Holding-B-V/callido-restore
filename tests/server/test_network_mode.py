"""Network mode (the app on a NAS): the password and sessions, the request guard for any Host, the server behind the sign-in,
and the Google sign-in handed over from the user's own browser."""
import http.client, json, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from callido_restore.server import api, auth
from callido_restore.server.api import H, LocalServer, request_refusal

PW = "a-long-password"


class Sessions(unittest.TestCase):
    def setUp(self): auth.enable(PW); self.addCleanup(auth.enable, None)

    def test_off_on_the_desktop(self):
        auth.enable(None); self.assertFalse(auth.enabled()); self.assertTrue(auth.valid(None))

    def test_a_short_password_is_refused(self):
        with self.assertRaises(ValueError): auth.enable("short")

    def test_login_gives_a_session_and_logout_ends_it(self):
        self.assertFalse(auth.valid(None)); self.assertFalse(auth.valid(f"{auth.COOKIE}=guess"))
        tok = auth.login(PW, "192.0.2.5"); self.assertTrue(tok)
        cookie = f"other=1; {auth.COOKIE}={tok}"
        self.assertTrue(auth.valid(cookie))
        auth.logout(cookie); self.assertFalse(auth.valid(cookie))

    def test_wrong_passwords_slow_the_client_down(self):
        self.assertIsNone(auth.login("wrong", "192.0.2.6"))
        self.assertGreater(auth.wait_before_try("192.0.2.6"), 0)
        self.assertIsNone(auth.login(PW, "192.0.2.6"))                       # even the right one waits
        self.assertEqual(auth.wait_before_try("192.0.2.7"), 0)               # another client is not affected
        with mock.patch.object(auth.time, "time", return_value=auth.time.time() + 60):
            self.assertTrue(auth.login(PW, "192.0.2.6"))

    def test_simultaneous_guesses_get_one_try(self):
        """The server answers in parallel threads: a burst of wrong passwords at the same moment is one try, the rest must wait."""
        from concurrent.futures import ThreadPoolExecutor
        start = threading.Barrier(40)
        def guess(i):
            start.wait(); return auth.attempt(f"wrong-{i}", "192.0.2.9")[0]
        with ThreadPoolExecutor(40) as ex: results = list(ex.map(guess, range(40)))
        self.assertEqual(results.count("wrong"), 1); self.assertEqual(results.count("wait"), 39)

    def test_an_expired_session_is_gone(self):
        tok = auth.login(PW, "192.0.2.8")
        with mock.patch.object(auth.time, "time", return_value=auth.time.time() + (auth.SESSION_DAYS + 1) * 86400):
            self.assertFalse(auth.valid(f"{auth.COOKIE}={tok}"))

    def test_cookie_flags(self):
        c = auth.set_cookie("t", secure=True)
        for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"): self.assertIn(flag, c)
        self.assertNotIn("Secure", auth.set_cookie("t"))


class Guard(unittest.TestCase):
    def test_any_host_is_fine_in_network_mode_but_not_on_the_desktop(self):
        self.assertIsNone(request_refusal("nas:8765", None, None, 8765, network=True))
        self.assertIsNone(request_refusal("192.0.2.10:8765", None, None, 8765, network=True))
        self.assertTrue(request_refusal("nas:8765", None, None, 8765))

    def test_the_origin_must_be_the_host_itself(self):
        self.assertIsNone(request_refusal("nas:8765", "http://nas:8765", "same-origin", 8765, network=True))
        self.assertIsNone(request_refusal("nas.example", "https://nas.example", "same-origin", 8765, network=True))
        self.assertTrue(request_refusal("nas:8765", "http://evil.example", None, 8765, network=True))
        self.assertTrue(request_refusal("nas:8765", None, "cross-site", 8765, network=True))
        self.assertTrue(request_refusal("", None, None, 8765, network=True))


class Server(unittest.TestCase):
    """The real server in network mode: only the sign-in page and /api/login answer without a session."""
    def setUp(self):
        auth.enable(PW); self.addCleanup(auth.enable, None)
        self.srv = LocalServer(("127.0.0.1", 0), H); self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close); self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, cookie=None, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"nas:{self.port}"}
        if cookie: h["Cookie"] = cookie
        if origin: h["Origin"] = origin
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h); r = c.getresponse()
        out = (r.status, r.getheader("Set-Cookie"), r.read()); c.close(); return out

    def test_without_a_session(self):
        st, _, page = self.req("GET", "/")
        self.assertEqual(st, 200); self.assertIn(b"/api/login", page); self.assertNotIn(b"dlgSettings", page)   # the sign-in page, not the app
        for path in ("/api/sources", "/api/jobs", "/api/about", "/api/ls?path=/"):
            st, _, body = self.req("GET", path); self.assertEqual(st, 401, path); self.assertTrue(json.loads(body)["login"])
        st, _, _ = self.req("POST", "/api/cache/clear", {}); self.assertEqual(st, 401)

    def test_sign_in_then_use_the_app(self):
        st, cookie, _ = self.req("POST", "/api/login", {"password": "wrong"}); self.assertEqual(st, 401); self.assertIsNone(cookie)
        auth._state["fails"].clear()
        st, cookie, _ = self.req("POST", "/api/login", {"password": PW}); self.assertEqual(st, 200)
        session = cookie.split(";")[0]
        st, _, page = self.req("GET", "/", cookie=session); self.assertEqual(st, 200); self.assertIn(b"dlgSettings", page)
        self.assertEqual(self.req("GET", "/api/sources", cookie=session)[0], 200)
        self.assertEqual(self.req("GET", "/api/quit", cookie=session)[0], 409)                    # the NAS app is not quit from a page
        self.assertEqual(self.req("POST", "/api/update/install", {}, cookie=session)[0], 409)     # a new image or package instead
        self.assertEqual(self.req("POST", "/api/cache/clear", {}, cookie=session, origin="http://evil.example")[0], 403)

    def test_behind_a_reverse_proxy_the_forwarded_host_is_the_origin(self):
        st, cookie, _ = self.req("POST", "/api/login", {"password": PW}); session = cookie.split(";")[0]
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/sources", headers={"Host": "localhost:8765", "X-Forwarded-Host": "restore.example",
                                                  "Origin": "https://restore.example", "Cookie": session})
        r = c.getresponse(); r.read(); c.close(); self.assertEqual(r.status, 200)

    def test_a_burst_of_requests_gets_one_401_and_the_rest_429(self):
        from concurrent.futures import ThreadPoolExecutor
        # the server's listen backlog is 64 (LocalServer), so all 20 connects are queued, none dropped or reset; the window is
        # long so that a slow runner cannot let the lock expire between them (the lock itself is tested on auth.attempt)
        self.enterContext(mock.patch.object(auth, "FAIL_DELAY", (0, 300)))
        with ThreadPoolExecutor(20) as ex: codes = list(ex.map(lambda i: self.req("POST", "/api/login", {"password": f"x{i}"})[0], range(20)))
        self.assertEqual(codes.count(401), 1); self.assertEqual(codes.count(429), 19)

    def test_too_many_tries_answers_wait(self):
        self.req("POST", "/api/login", {"password": "wrong"})
        st, _, body = self.req("POST", "/api/login", {"password": PW}); self.assertEqual(st, 429); self.assertIn(b"Try again", body)


class _FakeRclone(BaseHTTPRequestHandler):
    """rclone authorize's local listener: /auth sends the browser to Google; / receives Google's answer."""
    got = []
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/auth"):
            self.send_response(307)
            self.send_header("Location", "https://accounts.google.com/o/oauth2/auth?client_id=x&redirect_uri=http%3A%2F%2F127.0.0.1%3A53682%2F&state=S123")
            self.end_headers(); return
        _FakeRclone.got.append(self.path); self.send_response(200); self.end_headers(); self.wfile.write(b"ok")


class RemoteSignIn(unittest.TestCase):
    def setUp(self):
        _FakeRclone.got = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeRclone); self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close); self.addCleanup(self.srv.shutdown)
        self.job = {"state": "running", "message": ""}
        api.remote_sign_in(self.job, f"http://127.0.0.1:{self.port}/auth?state=S123")

    def test_the_job_offers_the_google_page(self):
        self.assertTrue(self.job["auth_url"].startswith("https://accounts.google.com/"))
        self.assertEqual((self.job["auth_port"], self.job["auth_state"]), (self.port, "S123"))

    def test_only_the_address_the_sign_in_ended_on_is_delivered(self):
        bad = [f"http://evil.example:{self.port}/?state=S123&code=c",                 # another host
               f"http://127.0.0.1:{self.port + 1}/?state=S123&code=c",                # another port
               f"http://127.0.0.1:{self.port}/other?state=S123&code=c",               # another path
               f"http://127.0.0.1:{self.port}/?state=OLD&code=c",                     # an earlier sign-in
               f"http://127.0.0.1:{self.port}/?state=S123", "", "not a url"]
        for u in bad:
            with self.assertRaises(ValueError, msg=u): api.finish_remote_sign_in(self.job, u)
        self.assertEqual(_FakeRclone.got, [])
        with self.assertRaisesRegex(ValueError, "access_denied"):
            api.finish_remote_sign_in(self.job, f"http://127.0.0.1:{self.port}/?error=access_denied&state=S123")
        api.finish_remote_sign_in(self.job, f"  http://127.0.0.1:{self.port}/?state=S123&code=4%2Fabc&scope=x  ")
        self.assertEqual(_FakeRclone.got, ["/?state=S123&code=4%2Fabc&scope=x"]); self.assertNotIn("auth_url", self.job)

    def test_a_page_that_does_not_go_to_google_is_refused(self):
        class Elsewhere(_FakeRclone):
            def do_GET(self):
                self.send_response(307); self.send_header("Location", "https://evil.example/"); self.end_headers()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere); threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        with self.assertRaises(RuntimeError): api.remote_sign_in({}, f"http://127.0.0.1:{srv.server_address[1]}/auth?state=x")


if __name__ == "__main__": unittest.main()
