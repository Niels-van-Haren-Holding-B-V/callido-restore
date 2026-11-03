"""Callido Restore inside DSM: DSM's own web server runs this as /webman/3rdparty/callido-restore/index.cgi/<path>.

The app itself listens on the NAS's loopback only (127.0.0.1:18765), so it cannot be reached from the network. This CGI is
the only way in: it lets in a DSM administrator with a valid DSM session (DSM's own authenticate.cgi, with the SynoToken the
page fetches from /webman/login.cgi), and forwards the request to the app. The page and its static files are served without
a session (they hold no data); every /api/ request needs one. No extra port, no firewall rule, no separate password.
"""
import grp, http.client, os, pwd, subprocess, sys
from urllib.parse import parse_qsl, urlencode

APP = ("127.0.0.1", 18765)
PREFIX = "/webman/3rdparty/callido-restore/index.cgi"
AUTHENTICATE = "/usr/syno/synoman/webman/modules/authenticate.cgi"
ADMIN_GROUP = "administrators"
PASS_HEADERS = ("CONTENT_TYPE",)


def out(status, body, ctype="application/json; charset=utf-8", extra=()):
    w = sys.stdout.buffer
    w.write(f"Status: {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\n".encode())
    for k, v in extra: w.write(f"{k}: {v}\r\n".encode())
    w.write(b"\r\n"); w.write(body); w.flush()


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


def main(env=os.environ, stdin=sys.stdin.buffer, auth=dsm_admin):
    path = env.get("PATH_INFO") or "/"
    if not path.startswith("/"): path = "/" + path
    pairs = parse_qsl(env.get("QUERY_STRING", ""), keep_blank_values=True)
    query = [(k, v) for k, v in pairs if k != "SynoToken"]
    token = env.get("HTTP_X_SYNO_TOKEN") or next((v for k, v in pairs if k == "SynoToken"), "")   # a download link carries it in the query
    if path.startswith("/api/") and not auth({**env, "HTTP_X_SYNO_TOKEN": token}):
        return out("401 Unauthorized", b'{"error": "Sign in to DSM as an administrator, then open Callido Restore from the DSM main menu.", "dsm": true}')
    method = env.get("REQUEST_METHOD", "GET")
    n = int(env.get("CONTENT_LENGTH") or 0)
    body = stdin.read(n) if n > 0 else None
    headers = {"Host": f"{APP[0]}:{APP[1]}", "X-Callido-Prefix": PREFIX}
    if env.get("CONTENT_TYPE"): headers["Content-Type"] = env["CONTENT_TYPE"]
    try:
        c = http.client.HTTPConnection(*APP, timeout=120)
        c.request(method, path + ("?" + urlencode(query) if query else ""), body=body, headers=headers)
        r = c.getresponse(); data = r.read()
    except OSError:
        return out("503 Service Unavailable", b'{"error": "Callido Restore is not running. Start it in Package Center."}')
    extra = [(k, v) for k, v in r.getheaders() if k.lower() in ("content-disposition",)]
    out(f"{r.status} {r.reason}", data, r.getheader("Content-Type") or "application/octet-stream", extra)


if __name__ == "__main__": main()
