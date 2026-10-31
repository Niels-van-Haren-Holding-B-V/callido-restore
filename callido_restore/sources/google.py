"""Google OAuth client checks that need no sign-in. A wrong client id or secret otherwise only shows up as an error page in the
browser halfway through the consent flow."""
import json, re, urllib.error, urllib.parse, urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
CLIENT_ID = re.compile(r"^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$")


def client_id_error(client_id):
    """'' when the id looks like a Google OAuth client id, else what is wrong with it."""
    cid = (client_id or "").strip()
    if not cid.endswith(".apps.googleusercontent.com"): return "the client id should end in .apps.googleusercontent.com (copy it from APIs & Services → Credentials)"
    if not CLIENT_ID.match(cid): return "that does not look like a Google client id (expected <number>-<letters>.apps.googleusercontent.com)"
    return ""


def client_check(client_id, client_secret, timeout=10):
    """Ask Google's token endpoint with a bogus refresh token: an unknown id/secret pair answers invalid_client, a known pair
    invalid_grant. Returns '' when the pair is accepted or the check could not run (no network), else a message."""
    data = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret, "grant_type": "refresh_token", "refresh_token": "x"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: r.read()
        return ""
    except urllib.error.HTTPError as e:
        try: err = json.loads(e.read() or b"{}").get("error", "")
        except ValueError: err = ""
        if err == "invalid_client": return "Google does not know this client id and secret together: check both in the Google Cloud console under APIs & Services → Credentials (the secret must belong to this client id)"
        return ""                                                   # invalid_grant and the like: the pair itself is accepted
    except (urllib.error.URLError, OSError, TimeoutError):
        return ""                                                   # offline: the sign-in itself will tell
