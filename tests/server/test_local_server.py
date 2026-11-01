"""The app's server binds without a reverse DNS lookup: http.server's server_bind calls socket.getfqdn("127.0.0.1"), which took
35 s on GitHub's macOS runners and hangs the same way on Macs whose reverse lookups hang (some VPN or company networks)."""
import json, socket, threading, time, unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.request import urlopen

from callido_restore.server.api import H, LocalServer


def hanging_getfqdn(name=""):
    time.sleep(5); return name


class NoReverseLookup(unittest.TestCase):
    def test_the_standard_server_does_the_lookup(self):            # the control: what LocalServer avoids
        with mock.patch.object(socket, "getfqdn", side_effect=RuntimeError("reverse lookup")):
            with self.assertRaises(RuntimeError): ThreadingHTTPServer(("127.0.0.1", 0), H)

    def test_binds_at_once_when_reverse_lookups_hang(self):
        with mock.patch.object(socket, "getfqdn", side_effect=hanging_getfqdn) as fq:
            t0 = time.time(); srv = LocalServer(("127.0.0.1", 0), H); took = time.time() - t0
        try:
            self.assertLess(took, 1.0); fq.assert_not_called()
            self.assertEqual((srv.server_name, srv.server_port), ("127.0.0.1", srv.server_address[1]))
        finally:
            srv.server_close()

    @unittest.skipIf(__import__("os").name == "nt", "SO_REUSEADDR lets a second bind succeed on Windows; the app's duplicate-launch check there is hbk-5's")
    def test_a_port_in_use_still_raises(self):                       # app.main: OSError on bind -> _already_running(port)
        first = LocalServer(("127.0.0.1", 0), H)
        try:
            with self.assertRaises(OSError): LocalServer(("127.0.0.1", first.server_address[1]), H)
        finally:
            first.server_close()

    def test_serves_the_api(self):
        with mock.patch.object(socket, "getfqdn", side_effect=RuntimeError("reverse lookup")):
            srv = LocalServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/sources", timeout=10) as r:
                self.assertEqual(r.status, 200); self.assertIsInstance(json.loads(r.read()), list)
        finally:
            srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
