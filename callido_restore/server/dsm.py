"""Sign in with the DSM session (the app as a Synology package): no separate password for a DSM administrator.

DSM's own check for a logged-in user is ``/usr/syno/synoman/webman/modules/authenticate.cgi``: run as a CGI with the browser's
cookies it prints the user name of a valid DSM session, else nothing. Browsers send cookies per host name, not per port, so a
page opened as http://<nas>:8765 from the DSM main menu carries DSM's session cookie too. Only members of the group
``administrators`` are let in. Where this does not work (not a Synology, the package user may not run the check, DSM wants its
SynoToken, the page was opened under another host name), the reason is returned and the password sign-in stays.
"""
import grp, os, pwd, subprocess

AUTHENTICATE = "/usr/syno/synoman/webman/modules/authenticate.cgi"
GROUP = "administrators"


def available():
    return os.path.isfile(AUTHENTICATE) and os.access(AUTHENTICATE, os.X_OK)


def _groups(user):
    gs = {g.gr_name for g in grp.getgrall() if user in g.gr_mem}
    try: gs.add(grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name)
    except KeyError: pass
    return gs


def session_user(cookie_header, client_ip, token=None, run=subprocess.run, groups=_groups):
    """(user, None) for a DSM administrator's valid session, else (None, why)."""
    if not available(): return None, "this is not a Synology NAS"
    if not cookie_header: return None, "the browser sent no DSM session (open the app from the DSM main menu)"
    env = {"PATH": "/usr/syno/bin:/usr/bin:/bin", "GATEWAY_INTERFACE": "CGI/1.1", "REQUEST_METHOD": "GET",
           "SERVER_PROTOCOL": "HTTP/1.1", "HTTP_COOKIE": cookie_header, "REMOTE_ADDR": client_ip or ""}
    if token: env.update(HTTP_X_SYNO_TOKEN=token, QUERY_STRING="SynoToken=" + token)
    try: r = run([AUTHENTICATE], env=env, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e: return None, f"DSM's sign-in check could not run ({e})"
    user = (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else ""
    if r.returncode != 0 or not user:
        return None, "DSM did not confirm a session" + (f" ({(r.stderr or '').strip()[-200:]})" if (r.stderr or "").strip() else "")
    if GROUP not in groups(user): return None, f"DSM user {user} is not an administrator"
    return user, None
