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


def check_update_helper(exe):
    """The update helper started from the windowed exe itself (no console), the way v1.2.x starts it before quitting: it must
    confirm it runs, and keep running after the exe has exited (v1.2.0 quit without a helper on a real Windows 11 PC)."""
    with tempfile.TemporaryDirectory() as d:
        res = Path(d) / "helper.result"
        r = subprocess.run([str(exe), "--helper-smoke", str(res)], capture_output=True, text=True, timeout=120)
        first = res.read_text(errors="replace") if res.exists() else ""
        print(f"update helper smoke: rc={r.returncode} result={first.strip()!r}")
        if r.returncode != 0 or "helper started" not in first: die(f"the windowed exe could not start the update helper: {first.strip()!r}")
        for _ in range(40):
            if "helper done" in res.read_text(errors="replace"): break
            time.sleep(0.5)
        else: die("the update helper did not outlive the app that started it")
    print("update helper check passed")


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
    check_update_helper(exe)
    check_window(exe)


def msiexec(*args):
    log = Path(tempfile.gettempdir()) / f"callido-msi-{args[0].strip('/')}.log"
    r = subprocess.run(["msiexec", *args, "/qn", "/norestart", "/l*v", str(log)])
    if r.returncode not in (0, 3010):
        try: print(log.read_text(encoding="utf-16", errors="replace")[-6000:])
        except OSError: pass
        die(f"msiexec {' '.join(args)} returned {r.returncode}")


UPGRADE_CODE = "{FC1126F1-1B40-48ED-9E1B-F366AA822764}"          # packaging/windows/CallidoRestore.wxs


def related_products():
    """ProductCodes installed under our UpgradeCode (one after a clean upgrade, none after uninstall)."""
    ps = f"$i = New-Object -ComObject WindowsInstaller.Installer; $i.RelatedProducts('{UPGRADE_CODE}') | ForEach-Object {{ $_ }}"
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def check_auto_update(old_msi, old_version, new_msi, app_name, version, folder):
    """The real auto-update path: the previous release installed, then callido_restore.update_windows's helper (wait for the
    app's pid, msiexec /passive, start the app) with the new MSI. The restarted app must report the new version, and only one
    product may be registered under the UpgradeCode (an in-place upgrade, not a second install)."""
    vt = lambda v: tuple(int(x) for x in v.split("."))
    if vt(old_version) >= vt(version):
        print(f"auto-update check skipped: previous release {old_version} is not older than {version}"); return
    sys.path.insert(0, str(ROOT))
    from callido_restore import update_windows as uw
    old_msi = Path(old_msi).resolve()                      # msiexec needs a full path with backslashes ("prev/x.msi" fails)
    msiexec("/i", str(old_msi))
    exe = folder / f"{app_name}.exe"
    if not exe.is_file(): die(f"previous release {old_version} did not install {exe}")
    new_msi = Path(new_msi).resolve()

    def run_helper(sha, tag):
        """One helper run, attached (the "app" is a dummy), its output and result file printed; then wait for the app it
        restarts on the default port and return the version that app reports (and quit it)."""
        dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])     # stands in for the running app
        log = Path(tempfile.gettempdir()) / f"callido-update-{tag}.log"
        # started exactly as the app starts it (hidden, CREATE_NO_WINDOW), and confirmed running before the "app" goes
        helper = uw.start_helper(uw.helper_command(new_msi, exe, dummy.pid, log, sha), str(log) + ".result")
        dummy.wait()
        try: helper.wait(timeout=600)
        except subprocess.TimeoutExpired: helper.kill(); helper.wait()
        print(f"[{tag}] helper exit", helper.returncode)
        try: result = Path(str(log) + ".result").read_text(); print(result)
        except OSError: result = ""; print("no helper result file")
        global PORT
        saved, PORT = PORT, 8765                                                       # the restarted app uses its default port
        try:
            got = None
            for _ in range(240):
                try: got = json.loads(get("/api/about")).get("version")
                except (OSError, ValueError): got = None
                if got: break
                time.sleep(1)
            if got:
                get("/api/quit"); time.sleep(3)
            elif tag == "upgrade":
                try: print(log.read_text(encoding="utf-16", errors="replace")[-4000:])
                except OSError: pass
            return got, result
        finally: PORT = saved

    # 1. A file that does not match the verified hash (swapped in the cache) is refused; the old version stays and restarts.
    got, result = run_helper("0" * 64, "tampered")
    if "the MSI changed after it was verified" not in result: die(f"a tampered MSI was not refused: {result!r}")
    if got != old_version: die(f"after a refused update the app reports {got!r}, expected the old {old_version}")
    print(f"tampered MSI refused, {old_version} restarted")
    # 2. The real update: the verified hash, an in-place upgrade, the new version restarts.
    got, result = run_helper(uw._sha256_of(new_msi), "upgrade")
    if got != version: die(f"after the update the app reports {got!r}, expected {version}")
    print(f"auto-update: {old_version} -> {got}, app restarted")
    prods = related_products()
    if len(prods) != 1: die(f"expected one installed product after the upgrade, found {prods}")
    print("auto-update check passed (in-place upgrade, one product)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--msi", help="install this MSI, test the installed app, uninstall")
    ap.add_argument("--upgrade-from", help="previous release's MSI: first test the auto-update from it to --msi")
    ap.add_argument("--upgrade-from-version", help="version of --upgrade-from (x.y.z)")
    a = ap.parse_args()
    if os.name != "nt": die("Windows only")
    app_name, version = about_py()
    if not a.msi:
        check_app(ROOT / "dist" / app_name / f"{app_name}.exe", app_name, version); print("smoke test passed"); return
    msi = Path(a.msi).resolve()
    folder = Path(os.environ.get("ProgramW6432") or os.environ["ProgramFiles"]) / "Callido" / app_name
    start_menu = Path(os.environ["ProgramData"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{app_name}.lnk"
    desktop = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop" / f"{app_name}.lnk"
    if a.upgrade_from: check_auto_update(a.upgrade_from, a.upgrade_from_version, msi, app_name, version, folder)
    msiexec("/i", str(msi))
    try:
        for lnk in (start_menu, desktop):
            if not lnk.is_file(): die(f"shortcut missing: {lnk}")
        check_app(folder / f"{app_name}.exe", app_name, version)
    finally:
        msiexec("/x", str(msi))
    if (folder / f"{app_name}.exe").exists(): die(f"uninstall left {folder}")
    if start_menu.exists() or desktop.exists(): die("uninstall left a shortcut behind")
    if related_products(): die(f"uninstall left products registered: {related_products()}")
    print("smoke test passed (install, run, uninstall)")


if __name__ == "__main__":
    main()
