"""Google client id checks that run before the sign-in."""
import io, json, unittest, urllib.error
from unittest import mock

from callido_restore.sources import google


class ClientId(unittest.TestCase):
    def test_format(self):
        self.assertEqual(google.client_id_error("123456-abc9xyz.apps.googleusercontent.com"), "")
        self.assertIn("apps.googleusercontent.com", google.client_id_error("GOCSPX-secret"))
        self.assertIn("does not look like", google.client_id_error("abc.apps.googleusercontent.com"))


class ClientCheck(unittest.TestCase):
    def _http_error(self, body):
        return urllib.error.HTTPError("https://oauth2.googleapis.com/token", 401, "Unauthorized", {}, io.BytesIO(json.dumps(body).encode()))

    def test_unknown_pair_is_reported(self):
        with mock.patch.object(google.urllib.request, "urlopen", side_effect=self._http_error({"error": "invalid_client"})):
            self.assertIn("does not know this client id and secret", google.client_check("1-a.apps.googleusercontent.com", "x"))

    def test_known_pair_with_bogus_token_passes(self):
        with mock.patch.object(google.urllib.request, "urlopen", side_effect=self._http_error({"error": "invalid_grant"})):
            self.assertEqual(google.client_check("1-a.apps.googleusercontent.com", "x"), "")

    def test_offline_does_not_block(self):
        with mock.patch.object(google.urllib.request, "urlopen", side_effect=urllib.error.URLError("no network")):
            self.assertEqual(google.client_check("1-a.apps.googleusercontent.com", "x"), "")
