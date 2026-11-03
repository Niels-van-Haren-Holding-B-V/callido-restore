"""Entry point: start the local server, show the UI in a native window (or the browser with --browser), log to app.log when
frozen. Usage: python -m callido_restore [--port 8765] [--browser | --no-window] [--smoke]"""
import argparse, os, subprocess, sys, threading
from pathlib import Path
from callido_restore import appsettings, paths
from callido_restore.app import tray as _tray
from callido_restore.about import APP_NAME, VERSION
from callido_restore.paths import RCLONE, _bundled, _resource_roots
from callido_restore.restore.jobs import JOBS
from callido_restore.server.api import H, _exit_app, start_auto_update, LocalServer
from callido_restore.sources import rclone
from callido_restore.sources.source import load_sources

def _verify_frozen():
    """Fail fast when a packaged build is missing modules or bundled files."""
    if not getattr(sys, "frozen", False): return
    import lz4.block, webview  # noqa: F401
    from callido_restore.core import pool  # noqa: F401
    from callido_restore.pool import poolmap, poolmap2, anchor, indexfetch  # noqa: F401
    if _bundled("static/index.html") is None:
        roots = ", ".join(str(r) for r in _resource_roots())
        raise SystemExit("bundled static/index.html not found in: " + roots)
    if not RCLONE or not Path(RCLONE).is_file() or not os.access(RCLONE, os.X_OK):
        raise SystemExit("bundled rclone is missing or not executable; refusing to use an rclone installed on the system")

def _log(msg):
    stream = sys.stdout if sys.stdout is not None else sys.stderr
    if stream is None: return
    try: print(msg, file=stream, flush=True)
    except Exception: pass

def _open_browser(port):
    try:
        import webbrowser; webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception: _log(f"could not open a browser; open http://127.0.0.1:{port}")

def _already_running(port):
    """True when an instance of the app already answers on this port (e.g. the app was opened twice)."""
    try:
        from urllib.request import urlopen
        with urlopen(f"http://127.0.0.1:{port}/api/about", timeout=2) as r: return b'"version"' in r.read()
    except Exception: return False

def run_window(port, smoke=False):
    """Show the UI in a native window (pywebview: WebKit on macOS, Edge WebView2 on Windows, GTK/Qt on Linux). Blocks until the
    window is closed. Must run on the main thread. With smoke=True the window closes itself once the page has loaded and the
    function returns the page title (None when it never loaded), so a build can be checked without a person."""
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True                   # the explorer's single-file download links
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    url = f"http://127.0.0.1:{port}"
    win = webview.create_window(APP_NAME, url, width=1360, height=880, min_size=(900, 600), text_select=True)
    result = {}; quitting = [False]
    tray = None if smoke else _make_tray(win, quitting)
    def closing():
        # The "keep running" setting: closing the window hides it; the menu-bar / tray icon brings it back or quits. On macOS
        # only the close button or Cmd+W hides; Cmd+Q, the Dock's Quit and logging out quit as before.
        if quitting[0] or smoke: return True                   # Quit from the icon's menu: already asked there
        if tray and tray.shown and (sys.platform != "darwin" or _tray.mac_close_was_the_window()):
            win.hide(); tray.dock(False); return False
        return _may_quit(win)
    win.events.closing += closing
    if smoke:
        guard = threading.Timer(30, lambda: (_log("smoke: window never loaded"), _exit_app(2)))
        def loaded():
            guard.cancel()
            try: result["title"] = win.evaluate_js("document.title")
            finally: _log(f"smoke: page loaded, title={result.get('title')!r}"); win.destroy()
        win.events.loaded += loaded
        guard.start()
    def started():
        if tray is None: return
        try:
            if sys.platform == "darwin": _tray.mac_watch_events()
            if appsettings.get("tray"): tray.start()
            appsettings.TRAY_AVAILABLE[0] = True
            appsettings.on_change(lambda d: tray.start() if d.get("tray") else tray.stop())
        except Exception as e: _log(f"tray: not available ({e}); closing the window quits the app")
    webview.start(started, private_mode=False)                 # keep the page's local storage (restore selection, last run) across restarts
    if tray: tray.stop()
    return result.get("title")

def _may_quit(win):
    """True when nothing runs, or the user agrees to quit while a job (e.g. a restore) runs."""
    running = [j["label"] for j in JOBS.values() if j["state"] == "running"]
    if not running: return True
    return bool(win.create_confirmation_dialog(APP_NAME, f"{len(running)} job(s) still running: {running[0]}. "
                                               "Quit anyway? Restore jobs are saved and can be resumed later."))

def _make_tray(win, quitting):
    """The menu-bar / notification-area icon (app/tray.py): Show brings the window back, Quit closes it the normal way (with the
    question while a job runs)."""
    def show(): tray.dock(True); win.show(); win.restore()
    def quit_():
        if any(j["state"] == "running" for j in JOBS.values()):
            tray.dock(True); win.show()                                 # the question appears with the window
            if not _may_quit(win): return
        quitting[0] = True; win.destroy()
    tray = _tray.Tray(show, quit_, lambda: list(JOBS.values()))
    return tray

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--mapper":           # child process of the pool preparation (pool/poolmap.py)
        from callido_restore.pool import poolmap
        return poolmap.mapper_main(sys.argv[2:])
    if len(sys.argv) == 3 and sys.argv[1] == "--helper-smoke":     # build check (smoke_windows): start the update helper's way
        from callido_restore import update_windows
        return update_windows.helper_smoke(sys.argv[2])
    if len(sys.argv) == 4 and sys.argv[1] == "--scan-worker":      # one of the mapper's anchor-scan workers (pool/anchor.py)
        from callido_restore.pool import anchor
        return anchor._worker_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=f"{APP_NAME}: explore a Synology Hyper Backup and restore files from it")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CALLIDO_PORT") or 8765))
    ap.add_argument("--listen", default=os.environ.get("CALLIDO_LISTEN") or "127.0.0.1",
                    help="address to listen on; anything but 127.0.0.1 is network mode (a NAS): server only, password required")
    ap.add_argument("--password-file", default=os.environ.get("CALLIDO_PASSWORD_FILE"),
                    help="network mode: file holding the password (or set CALLIDO_PASSWORD)")
    ap.add_argument("--no-browser", "--no-window", dest="no_window", action="store_true", help="server only (no window, no browser)")
    ap.add_argument("--browser", action="store_true", help="use the system browser instead of the app window")
    ap.add_argument("--smoke", action="store_true", help="open the window, wait for the page, close it and exit (build check)")
    a = ap.parse_args()
    network = a.listen not in ("127.0.0.1", "localhost", "::1")
    if network:
        from callido_restore.server import auth
        pw = os.environ.get("CALLIDO_PASSWORD")
        if a.password_file:
            try: pw = Path(a.password_file).read_text().rstrip("\r\n")    # the file's line end only; spaces are part of it, as in the variable
            except OSError as e: raise SystemExit(f"cannot read the password file: {e}")
        if not pw: raise SystemExit("network mode (--listen) needs a password: set CALLIDO_PASSWORD or --password-file")
        try: auth.enable(pw)
        except ValueError as e: raise SystemExit(str(e))
        a.no_window = True                                          # a NAS has no screen: the page is opened from other computers
    if getattr(sys, "frozen", False) and not a.smoke and not network:      # .app bundle: no terminal, so keep a log file next to the settings (smoke: stdout)
        try:
            paths.CONF_DIR.mkdir(parents=True, exist_ok=True)
            sys.stdout = sys.stderr = open(paths.CONF_DIR / "app.log", "a", buffering=1)
        except OSError as e: _log(f"warning: cannot write {paths.CONF_DIR / 'app.log'} ({e}); logging to the console instead")
    _verify_frozen()
    load_sources()
    try:
        from callido_restore.pool import poolmap
        n = poolmap.kill_orphan_helpers()                          # mapper / scan workers left by a crashed app
        if n: _log(f"ended {n} orphaned helper process(es) of a previous run")
    except Exception: pass
    rclone.start_in_background()                               # one rclone daemon for all listings (fast wizard, fast scans)
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):                 # a kill from outside still stops the rclone daemon with the app
        try: signal.signal(sig, lambda *_: _exit_app(0))
        except (ValueError, OSError): pass
    windowed = not a.no_window and not a.browser
    try: srv = LocalServer((a.listen, a.port), H)
    except OSError as e:
        if _already_running(a.port):
            _log(f"already running on port {a.port}; showing the running instance")
            if windowed: run_window(a.port, smoke=a.smoke)
            elif not a.no_window: _open_browser(a.port)
            return
        raise SystemExit(f"cannot listen on {a.listen}:{a.port}: {e} (another program uses this port; try --port)")
    _log(f"{APP_NAME} {VERSION} on http://{a.listen}:{a.port}{'  (network mode, sign-in required)' if network else ''}  (config: {paths.CONF_DIR})")
    if not a.smoke and not network: start_auto_update()                         # the automatic update setting (and the daily check)
    if not windowed:
        if not a.no_window: threading.Timer(0.6, lambda: _open_browser(a.port)).start()
        try: srv.serve_forever()
        except KeyboardInterrupt: pass
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try: title = run_window(a.port, smoke=a.smoke)
    except Exception as e:
        if a.smoke: _log(f"smoke: window failed: {e}"); _exit_app(2)
        _log(f"no app window available ({e}); falling back to the browser")
        _open_browser(a.port)
        try: threading.Event().wait()                        # the server thread keeps serving until Ctrl-C / Quit
        except KeyboardInterrupt: pass
        return
    if a.smoke:
        ok = bool(title) and APP_NAME in title
        _log(f"smoke: {'ok' if ok else 'FAILED'} (title {title!r})")
        _exit_app(0 if ok else 2)
    _log("window closed; stopping")
    _exit_app(0)

if __name__ == "__main__": main()
