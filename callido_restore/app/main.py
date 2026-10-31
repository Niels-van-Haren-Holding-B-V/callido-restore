"""Entry point: start the local server, show the UI in a native window (or the browser with --browser), log to app.log when
frozen. Usage: python -m callido_restore [--port 8765] [--browser | --no-window] [--smoke]"""
import argparse, os, subprocess, sys, threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from callido_restore import paths
from callido_restore.about import APP_NAME, VERSION
from callido_restore.paths import RCLONE, _bundled, _resource_roots
from callido_restore.restore.jobs import JOBS
from callido_restore.server.api import H, _exit_app
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
    webview.settings["PRIVATE_MODE"] = False                     # keep the page's local storage (restore selection, last run) across restarts
    url = f"http://127.0.0.1:{port}"
    win = webview.create_window(APP_NAME, url, width=1360, height=880, min_size=(900, 600), text_select=True)
    result = {}
    def closing():                                               # warn before closing while a job (e.g. a restore) runs
        running = [j["label"] for j in JOBS.values() if j["state"] == "running"]
        if not running or smoke: return True
        return bool(win.create_confirmation_dialog(APP_NAME, f"{len(running)} job(s) still running: {running[0]}. "
                                                   "Quit anyway? Restore jobs are saved and can be resumed later."))
    win.events.closing += closing
    if smoke:
        guard = threading.Timer(30, lambda: (_log("smoke: window never loaded"), _exit_app(2)))
        def loaded():
            guard.cancel()
            try: result["title"] = win.evaluate_js("document.title")
            finally: _log(f"smoke: page loaded, title={result.get('title')!r}"); win.destroy()
        win.events.loaded += loaded
        guard.start()
    webview.start()
    return result.get("title")

def main():
    ap = argparse.ArgumentParser(description=f"{APP_NAME}: explore a Synology Hyper Backup and restore files from it")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", "--no-window", dest="no_window", action="store_true", help="server only (no window, no browser)")
    ap.add_argument("--browser", action="store_true", help="use the system browser instead of the app window")
    ap.add_argument("--smoke", action="store_true", help="open the window, wait for the page, close it and exit (build check)")
    a = ap.parse_args()
    if getattr(sys, "frozen", False) and not a.smoke:      # .app bundle: no terminal, so keep a log file next to the settings (smoke: stdout)
        try:
            paths.CONF_DIR.mkdir(parents=True, exist_ok=True)
            sys.stdout = sys.stderr = open(paths.CONF_DIR / "app.log", "a", buffering=1)
        except OSError as e: _log(f"warning: cannot write {paths.CONF_DIR / 'app.log'} ({e}); logging to the console instead")
    _verify_frozen()
    load_sources()
    rclone.start_in_background()                               # one rclone daemon for all listings (fast wizard, fast scans)
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):                 # a kill from outside still stops the rclone daemon with the app
        try: signal.signal(sig, lambda *_: _exit_app(0))
        except (ValueError, OSError): pass
    windowed = not a.no_window and not a.browser
    try: srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    except OSError as e:
        if _already_running(a.port):
            _log(f"already running on port {a.port}; showing the running instance")
            if windowed: run_window(a.port, smoke=a.smoke)
            elif not a.no_window: _open_browser(a.port)
            return
        raise SystemExit(f"cannot listen on 127.0.0.1:{a.port}: {e} (another program uses this port; try --port)")
    _log(f"{APP_NAME} {VERSION} on http://127.0.0.1:{a.port}  (config: {paths.CONF_DIR})")
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
