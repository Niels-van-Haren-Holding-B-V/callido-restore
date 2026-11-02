"""tls.context: certificates verified, and on macOS the system's roots are loaded too (a CI-built app's own OpenSSL looks for
them in a Python.framework folder most Macs do not have: every update check failed with CERTIFICATE_VERIFY_FAILED)."""
import io, json, os, ssl, subprocess, sys, unittest
from unittest import mock

from callido_restore import tls, update


class Context(unittest.TestCase):
    def setUp(self):
        tls._ctx = None; self.addCleanup(setattr, tls, "_ctx", None)

    def test_verifies_certificates(self):
        ctx = tls.context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED); self.assertTrue(ctx.check_hostname)

    @unittest.skipUnless(os.path.isfile(tls.MAC_ROOTS), "no system root list here")
    def test_macos_loads_the_system_roots_when_the_built_in_location_has_none(self):
        # SSL_CERT_FILE / SSL_CERT_DIR pointing nowhere stand in for a CI build's missing Python.framework folder
        code = ("import ssl, sys; from callido_restore import tls; sys.platform = 'darwin'; "
                "print(ssl.create_default_context().cert_store_stats()['x509_ca'], tls.context().cert_store_stats()['x509_ca'])")
        env = {**os.environ, "SSL_CERT_FILE": "/nonexistent/cert.pem", "SSL_CERT_DIR": "/nonexistent/certs"}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).stdout.split()
        self.assertEqual(int(out[0]), 0)                                   # the situation of the CI-built app
        self.assertGreater(int(out[1]), 0)                                 # fixed by the system's roots

    def test_is_built_once(self):
        self.assertIs(tls.context(), tls.context())


class UpdateUsesIt(unittest.TestCase):
    def test_the_release_check_passes_the_context(self):
        seen = {}
        def fake(req, timeout=None, context=None):
            seen["ctx"] = context; return io.BytesIO(json.dumps({"tag_name": "v9.9.9"}).encode())
        with mock.patch.object(update.urllib.request, "urlopen", fake):
            self.assertEqual(update.latest_release()["tag_name"], "v9.9.9")
        self.assertIs(seen["ctx"], tls.context())
