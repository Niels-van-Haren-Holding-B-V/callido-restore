#!/usr/bin/env python3
"""Smoke test of the Windows build (the counterpart of scripts/smoke_macos_app.sh). Windows only.

    python packaging/windows/smoke_windows.py --msi dist/CallidoRestore-x64.msi
installs the MSI silently (per machine; needs an elevated shell, as on the CI runner), checks the installed app and its
shortcuts, then uninstalls it and checks that the program folder is gone. Without --msi it tests dist/<APP_NAME>/ in place.

App checks: the server answers with the app UI, /api/about reports this version and the bundled rclone.exe, /api/quit stops it;
a pool-mapper child (--mapper) can write to a pipe from the windowed exe (the preparation reads its progress that way); the
desktop window (Edge WebView2) opens, loads the page and closes itself (--smoke).
"""
import argparse, json, os, re, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get("HBK_SMOKE_PORT", "8791"))


def die(msg):
    raise SystemExit(f"smoke: FAILED: {msg}")


def about_py():
    t = (ROOT / "callido_restore" / "about.py").read_text(encoding="utf-8")
    return re.search(r'^APP_NAME = "([^"]+)"', t, re.M).group(1), re.search(r'^VERSION = "([^"]+)"', t, re.M).group(1)


def get(path, timeout=10):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r: return r.read().decode("utf-8")


def check_server(exe, app_name, version):
    p = subprocess.Popen([str(exe), "--no-window", "--port", str(PORT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try: page = get("/"); break
            except OSError: time.sleep(0.5)
            if p.poll() is not None: die(f"app exited with {p.returncode} before serving: {p.stdout.read()!r}")
        else: die("timed out waiting for the app's server")
        if app_name not in page: die("homepage did not contain the app UI")
        data = json.loads(get("/api/about", timeout=30)); print("about:", data)
        if data.get("version") != version: die(f"version {data.get('version')!r}, expected {version}")
        if not (data.get("rclone") or "").lower().startswith("rclone v"): die("about did not report the bundled rclone")
        rp = Path(data.get("rclone_path") or "")
        if rp.name.lower() != "rclone.exe" or exe.parent.resolve() not in rp.resolve().parents: die(f"rclone_path is not the bundled rclone.exe: {rp}")
        if '"ok"' not in get("/api/quit"): die("unexpected /api/quit response")
        p.wait(20)
    except subprocess.TimeoutExpired: die("app did not exit after /api/quit")
    finally:
        if p.poll() is None: p.kill()
    print("server check passed")


def check_child_pipe(exe):
    """The windowed exe started as a mapper child must reach its stdout pipe: the pool preparation reads progress from it."""
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run([str(exe), "--mapper", "no-such-source", d, "-"], capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    print(f"mapper child: rc={r.returncode} output={out.strip()[-300:]!r}")
    if "unknown source" not in out: die("a --mapper child of the windowed exe wrote nothing to its pipe")
    print("child pipe check passed")


def check_window(exe):
    r = subprocess.run([str(exe), "--smoke", "--port", str(PORT)], capture_output=True, text=True, timeout=120)
    print("window smoke:", r.returncode, (r.stdout or "").strip()[-400:])
    if r.returncode != 0: die("window smoke exited with an error (is the WebView2 runtime installed?)")
    print("window check passed")


def check_app(exe, app_name, version):
    if not exe.is_file(): die(f"missing {exe}")
    internal = exe.parent / "_internal"
    for rel in ("rclone.exe", "static/index.html", "Licenses/rclone-COPYING", "Licenses/THIRD-PARTY-NOTICES.txt"):
        if not (internal / rel).is_file(): die(f"missing {internal / rel}")
    line = subprocess.run([str(internal / "rclone.exe"), "version"], capture_output=True, text=True).stdout.splitlines()[:1]
    print("bundled", line)
    check_server(exe, app_name, version)
    check_child_pipe(exe)
    check_window(exe)


def msiexec(*args):
    log = Path(tempfile.gettempdir()) / f"callido-msi-{args[0].strip('/')}.log"
    r = subprocess.run(["msiexec", *args, "/qn", "/norestart", "/l*v", str(log)])
    if r.returncode not in (0, 3010):
        try: print(log.read_text(encoding="utf-16", errors="replace")[-6000:])
        except OSError: pass
        die(f"msiexec {' '.join(args)} returned {r.returncode}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--msi", help="install this MSI, test the installed app, uninstall")
    a = ap.parse_args()
    if os.name != "nt": die("Windows only")
    app_name, version = about_py()
    if not a.msi:
        check_app(ROOT / "dist" / app_name / f"{app_name}.exe", app_name, version); print("smoke test passed"); return
    msi = Path(a.msi).resolve()
    folder = Path(os.environ.get("ProgramW6432") or os.environ["ProgramFiles"]) / "Callido" / app_name
    start_menu = Path(os.environ["ProgramData"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{app_name}.lnk"
    desktop = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop" / f"{app_name}.lnk"
    msiexec("/i", str(msi))
    try:
        for lnk in (start_menu, desktop):
            if not lnk.is_file(): die(f"shortcut missing: {lnk}")
        check_app(folder / f"{app_name}.exe", app_name, version)
    finally:
        msiexec("/x", str(msi))
    if (folder / f"{app_name}.exe").exists(): die(f"uninstall left {folder}")
    if start_menu.exists() or desktop.exists(): die("uninstall left a shortcut behind")
    print("smoke test passed (install, run, uninstall)")


if __name__ == "__main__":
    main()
