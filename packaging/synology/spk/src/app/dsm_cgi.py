"""Callido Restore inside DSM: DSM's own web server runs this as /webman/3rdparty/callido-restore/index.cgi/<path>.

The app itself listens on the NAS's loopback only (127.0.0.1:18765), so it cannot be reached from the network. This CGI is
the only way in:
* every /api/ request must come from DSM's own page (no cross-site Origin or Sec-Fetch-Site: the app's own cross-site check
  never sees the browser's headers, and DSM enforces its SynoToken only while "Improve protection against cross-site request
  forgery attacks" is on), must carry the SynoToken the page fetches from /webman/login.cgi, and must belong to a DSM
  administrator's valid session (DSM's own authenticate.cgi);
* the request then goes to the app with the package's shared secret (var/cgi_secret, written at each service start), which
  the app requires in this mode: another process on the NAS cannot use the app's loopback port without it.
The page holds no data and is served without a session; the app serves nothing else outside /api/.
"""
import grp, http.client, os, pwd, subprocess, sys
from urllib.parse import parse_qsl, urlencode, urlparse

APP = ("127.0.0.1", 18765)
PREFIX = "/webman/3rdparty/callido-restore/index.cgi"
AUTHENTICATE = "/usr/syno/synoman/webman/modules/authenticate.cgi"
ADMIN_GROUP = "administrators"
SECRET_FILE = "/var/packages/callido-restore/var/cgi_secret"
CHUNK = 1 << 20


def head(status, ctype="application/json; charset=utf-8", length=None, extra=()):
    w = sys.stdout.buffer
    w.write(f"Status: {status}\r\nContent-Type: {ctype}\r\nCache-Control: no-store\r\n".encode())
    if length is not None: w.write(f"Content-Length: {length}\r\n".encode())
    for k, v in extra: w.write(f"{k}: {v}\r\n".encode())
    w.write(b"\r\n")
    return w


def out(status, body, ctype="application/json; charset=utf-8"):
    w = head(status, ctype, len(body)); w.write(body); w.flush()


def cross_site(env):
    """Why a request is not DSM's own page calling (another site's page with the admin's DSM cookie), or None."""
    site = (env.get("HTTP_SEC_FETCH_SITE") or "").lower()
    if site and site not in ("same-origin", "none"): return f"a {site} request"
    origin = env.get("HTTP_ORIGIN")
    if origin:
        o = urlparse(origin)
        if origin == "null" or (o.netloc or "").lower() != (env.get("HTTP_HOST") or "").lower(): return f"Origin {origin}"
    return None


def dsm_admin(env, run=subprocess.run):
    """The DSM user name of an administrator's valid session, else None. authenticate.cgi reads the session cookie from
    HTTP_COOKIE and, with DSM's CSRF protection, the SynoToken from X-SYNO-TOKEN / the query string."""
    token = env.get("HTTP_X_SYNO_TOKEN", "")
    cgi_env = {k: v for k, v in env.items() if k.startswith(("HTTP_", "REMOTE_", "SERVER_")) or k in ("GATEWAY_INTERFACE",)}
    cgi_env.update(REQUEST_METHOD="GET", QUERY_STRING=("SynoToken=" + token) if token else "", PATH="/usr/syno/bin:/usr/bin:/bin")
    try: r = run([AUTHENTICATE], env=cgi_env, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError): return None
    user = (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else ""
    if r.returncode != 0 or not user: return None
    groups = {g.gr_name for g in grp.getgrall() if user in g.gr_mem}
    try: groups.add(grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name)
    except KeyError: pass
    return user if ADMIN_GROUP in groups else None


def secret(path=None):
    try: return open(path or SECRET_FILE).read().strip()
    except OSError: return ""


def main(env=os.environ, stdin=sys.stdin.buffer, auth=dsm_admin, secret=secret):
    path = env.get("PATH_INFO") or "/"
    if not path.startswith("/"): path = "/" + path
    pairs = parse_qsl(env.get("QUERY_STRING", ""), keep_blank_values=True)
    query = [(k, v) for k, v in pairs if k != "SynoToken"]
    # a download link carries the token in its query; it then also lands in DSM's access log and the browser history,
    # acceptable for a token that is bound to the DSM session and ends with it
    token = env.get("HTTP_X_SYNO_TOKEN") or next((v for k, v in pairs if k == "SynoToken"), "")
    if path.startswith("/api/"):
        why = cross_site(env)
        if why: return out("403 Forbidden", ('{"error": "refused: %s"}' % why.replace('"', "'")).encode())
        if not token or not auth({**env, "HTTP_X_SYNO_TOKEN": token}):
            return out("401 Unauthorized", b'{"error": "Sign in to DSM as an administrator, then open Callido Restore from the DSM main menu.", "dsm": true}')
    key = secret()
    if not key: return out("503 Service Unavailable", b'{"error": "Callido Restore is not running (or its key cannot be read). Start it in Package Center."}')
    method = env.get("REQUEST_METHOD", "GET")
    n = int(env.get("CONTENT_LENGTH") or 0)
    body = stdin.read(n) if n > 0 else None
    headers = {"Host": f"{APP[0]}:{APP[1]}", "X-Callido-Prefix": PREFIX, "X-Callido-Secret": key}
    if env.get("CONTENT_TYPE"): headers["Content-Type"] = env["CONTENT_TYPE"]
    try:
        c = http.client.HTTPConnection(*APP, timeout=120)
        c.request(method, path + ("?" + urlencode(query) if query else ""), body=body, headers=headers)
        r = c.getresponse()
        extra = [(k, v) for k, v in r.getheaders() if k.lower() == "content-disposition"]
        w = head(f"{r.status} {r.reason}", r.getheader("Content-Type") or "application/octet-stream", r.getheader("Content-Length"), extra)
        while True:                                     # streamed: a large file download is not held in memory a second time
            chunk = r.read(CHUNK)
            if not chunk: break
            w.write(chunk)
        w.flush()
    except TimeoutError:
        return out("504 Gateway Timeout", b'{"error": "Callido Restore took too long to answer. Try again in a moment."}')
    except OSError:
        return out("503 Service Unavailable", b'{"error": "Callido Restore is not running. Start it in Package Center."}')


if __name__ == "__main__": main()
