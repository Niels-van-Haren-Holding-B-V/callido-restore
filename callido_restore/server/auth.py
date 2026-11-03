"""Network mode (the app on a NAS, opened from other computers): a password and session cookies.

On a desktop the server listens on 127.0.0.1 only and needs no login. With ``--listen`` on another address (a NAS, in Docker
or as a package) everyone on the network can reach it, so every request then needs a session: ``POST /api/login`` with the
password sets an HttpOnly, SameSite=Strict cookie. Sessions live in memory (a restart asks for the password again) and expire
after ``SESSION_DAYS`` without use. Wrong passwords are slowed down per client address. The password comes from the
environment (``CALLIDO_PASSWORD``) or a file (``--password-file``); network mode refuses to start without one.
"""
import hmac, os, secrets, threading, time

COOKIE = "callido_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8
FAIL_DELAY = (0, 1, 2, 5, 10, 30)                 # seconds a client waits before its next try, by failed tries in a row

_state = {"password": None, "sessions": {}, "fails": {}}
_lock = threading.Lock()


def enable(password):
    """Turn network mode on with this password (None turns it off: the desktop app)."""
    if password is not None and len(password) < MIN_PASSWORD:
        raise ValueError(f"the password must have at least {MIN_PASSWORD} characters")
    with _lock:
        _state["password"] = password; _state["sessions"].clear(); _state["fails"].clear()


def enabled(): return _state["password"] is not None


def remote():
    """The page is used from another computer: network mode (a password), or the Synology package, where DSM's web server
    passes requests on to the app on the NAS's loopback (CALLIDO_DSM=1, DSM checks the login). Then Quit and the in-app
    update are off, and the Google sign-in is handed over from the user's browser."""
    return enabled() or os.environ.get("CALLIDO_DSM") == "1"


def _cookie_value(cookie_header):
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE: return v
    return None


def valid(cookie_header):
    """Does the request carry a live session? Always true on the desktop (no network mode)."""
    if not enabled(): return True
    tok = _cookie_value(cookie_header)
    if not tok: return False
    now = time.time()
    with _lock:
        exp = _state["sessions"].get(tok)
        if not exp or exp < now:
            _state["sessions"].pop(tok, None); return False
        _state["sessions"][tok] = now + SESSION_DAYS * 86400          # in use: extended
    return True


def _wait(client, now):
    n, last = _state["fails"].get(client, (0, 0.0))
    return max(0.0, last + FAIL_DELAY[min(n, len(FAIL_DELAY) - 1)] - now)


def wait_before_try(client):
    """Seconds this client must still wait before it may try a password again (0: may try now)."""
    with _lock: return _wait(client, time.time())


def attempt(password, client):
    """One sign-in try: ("ok", token, 0) for the right password, ("wrong", None, wait) for a wrong one, ("wait", None, wait)
    when the client may not try yet (the password is then not looked at). Checking the wait, counting the
    try and comparing happen under one lock: the server answers requests in parallel threads, and a check before the lock let
    a burst of simultaneous guesses all pass the same open slot."""
    if not enabled(): return "wait", None, 0.0
    now = time.time()
    with _lock:
        wait = _wait(client, now)
        if wait > 0: return "wait", None, wait
        _sweep(now)
        if hmac.compare_digest((password or "").encode(), _state["password"].encode()):
            _state["fails"].pop(client, None)
            tok = secrets.token_urlsafe(32); _state["sessions"][tok] = now + SESSION_DAYS * 86400
            return "ok", tok, 0.0
        n, _ = _state["fails"].get(client, (0, 0.0)); _state["fails"][client] = (n + 1, now)
        return "wrong", None, _wait(client, now)


def login(password, client):
    """A new session token for the right password, else None (and the client waits longer before the next try)."""
    return attempt(password, client)[1]


def _sweep(now):
    """Forget expired sessions and failed tries older than a day (called under the lock, on each sign-in)."""
    for t in [t for t, exp in _state["sessions"].items() if exp < now]: del _state["sessions"][t]
    for c in [c for c, (_, last) in _state["fails"].items() if now - last > 86400]: del _state["fails"][c]


def logout(cookie_header):
    tok = _cookie_value(cookie_header)
    if tok:
        with _lock: _state["sessions"].pop(tok, None)


def set_cookie(token, secure=False):
    """The Set-Cookie value for a new session (``secure`` when the page came over HTTPS, e.g. behind DSM's reverse proxy)."""
    return (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}"
            + ("; Secure" if secure else ""))


def clear_cookie(): return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
