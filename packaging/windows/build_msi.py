#!/usr/bin/env python3
"""Build Callido Restore for Windows 10/11 (x64): a PyInstaller one-folder app with the bundled rclone.exe, packed into an MSI
with WiX Toolset 5. The Windows counterpart of build_app.sh; the app code itself stays cross-platform.

Run on Windows from the repo root, in a Python 3.12 environment with requirements.txt and pyinstaller installed:
    python packaging/windows/build_msi.py              -> dist/CallidoRestore-x64.msi
    python packaging/windows/build_msi.py --no-msi     PyInstaller folder only (dist/Callido Restore/)
    python packaging/windows/build_msi.py --rclone-only  download + verify rclone.exe only (works on any OS; build check)
Needs: gpg (Git for Windows ships one), and for the MSI the WiX 5 .NET tool with the UI and Util extensions:
    dotnet tool install --global wix --version 5.0.2
    wix extension add -g WixToolset.UI.wixext/5.0.2 WixToolset.Util.wixext/5.0.2
"""
import argparse, hashlib, os, re, shutil, subprocess, sys, tarfile, tempfile, urllib.request, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "packaging" / "windows"
BUILD = ROOT / "build" / "windows"
DIST = ROOT / "dist"

RCLONE_VERSION = "v1.75.2"                                         # same pin as build_app.sh
RCLONE_SIGNING_FPR = "FBF737ECE9F8AB18604BD2AC93935E02FF3B54FA"
RCLONE_ZIP = f"rclone-{RCLONE_VERSION}-windows-amd64.zip"
RCLONE_TAR = f"rclone-{RCLONE_VERSION}.tar.gz"                     # source tarball: only for the COPYING file
WIX_VERSION = "5.0.2"
MSI_NAME = "CallidoRestore-x64.msi"


def die(msg):
    raise SystemExit(f"build_msi: {msg}")


def about():
    """APP_NAME and VERSION from callido_restore/about.py (the single source, as in build_app.sh)."""
    text = (ROOT / "callido_restore" / "about.py").read_text(encoding="utf-8")
    vals = {k: re.search(rf'^{k} = "([^"]+)"', text, re.M) for k in ("APP_NAME", "VERSION")}
    if not all(vals.values()): die("APP_NAME/VERSION not found in callido_restore/about.py")
    name, version = vals["APP_NAME"].group(1), vals["VERSION"].group(1)
    if not re.fullmatch(r"\d+\.\d+\.\d+", version): die(f"VERSION {version!r} is not x.y.z (an MSI needs a numeric version)")
    return name, version


def run(cmd, **kw):
    print("+", " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


# ---- rclone: always the pinned release, signature and checksum verified (a cached binary is not trusted) ----

def find_gpg():
    gpg = shutil.which("gpg")
    if gpg: return gpg
    for c in (r"C:\Program Files\Git\usr\bin\gpg.exe", r"C:\Program Files (x86)\GnuPG\bin\gpg.exe", r"C:\Program Files\GnuPG\bin\gpg.exe"):
        if Path(c).is_file(): return c
    die("gpg is required to verify rclone's SHA256SUMS (install Git for Windows or Gpg4win)")


def gpg_path(p):
    """Git for Windows' gpg is an MSYS program: give it forward slashes, which both it and a native gpg accept."""
    return str(p).replace("\\", "/")


def download(url, out):
    print("download", url, flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "callido-restore-build"})   # the default Python agent gets a 403
    with urllib.request.urlopen(req, timeout=300) as r, open(out, "wb") as f: shutil.copyfileobj(r, f)


def verified_sums(dl):
    """The body of the clearsigned SHA256SUMS, only after gpg says it was signed by the rclone release key."""
    gpg, home = find_gpg(), dl / "gnupg"
    home.mkdir(mode=0o700)
    base = ["--batch", "--homedir", gpg_path(home)]
    run([gpg, *base, "--import", gpg_path(ROOT / "packaging" / "rclone-release-signing-key.asc")], capture_output=True)
    fp = subprocess.run([gpg, *base, "--with-colons", "--fingerprint", RCLONE_SIGNING_FPR], capture_output=True, text=True).stdout
    got = next((ln.split(":")[9] for ln in fp.splitlines() if ln.startswith("fpr:")), "")
    if got != RCLONE_SIGNING_FPR: die(f"signing key fingerprint is {got or 'missing'}")
    v = subprocess.run([gpg, *base, "--status-fd", "1", "--verify", gpg_path(dl / "SHA256SUMS")], capture_output=True, text=True)
    if v.returncode != 0 or f"VALIDSIG {RCLONE_SIGNING_FPR}" not in v.stdout:
        die("SHA256SUMS signature check failed:\n" + v.stderr)
    out = dl / "SHA256SUMS.clear"
    run([gpg, *base, "--yes", "--output", gpg_path(out), "--decrypt", gpg_path(dl / "SHA256SUMS")], capture_output=True)
    return out.read_text()


def check_sha256(sums, path):
    want = {p[1]: p[0].lower() for p in (ln.split() for ln in sums.splitlines()) if len(p) == 2 and len(p[0]) == 64}
    if path.name not in want: die(f"no signed sha256 for {path.name}")
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != want[path.name]: die(f"sha256 mismatch for {path.name}: {got} != {want[path.name]}")
    print("sha256 ok", path.name)


def fetch_rclone():
    """rclone.exe and the license files into build/windows; returns the rclone.exe path."""
    BUILD.mkdir(parents=True, exist_ok=True)
    lic = BUILD / "licenses"; lic.mkdir(exist_ok=True)
    base = f"https://downloads.rclone.org/{RCLONE_VERSION}"
    with tempfile.TemporaryDirectory(prefix="hbk-rclone-") as d:
        dl = Path(d)
        for name in ("SHA256SUMS", RCLONE_ZIP, RCLONE_TAR): download(f"{base}/{name}", dl / name)
        sums = verified_sums(dl)
        check_sha256(sums, dl / RCLONE_ZIP); check_sha256(sums, dl / RCLONE_TAR)
        with zipfile.ZipFile(dl / RCLONE_ZIP) as z:
            member = next((n for n in z.namelist() if n.endswith("/rclone.exe")), None)
            if not member: die(f"rclone.exe missing from {RCLONE_ZIP}")
            exe = BUILD / "rclone.exe"; exe.write_bytes(z.read(member))
        with tarfile.open(dl / RCLONE_TAR) as tf:
            f = tf.extractfile(f"rclone-{RCLONE_VERSION}/COPYING")
            data = f.read() if f else b""
        if b"Permission is hereby granted" not in data: die("rclone COPYING does not look like the MIT license")
        (lic / "rclone-COPYING").write_bytes(data)
    app_name, _ = about()
    (lic / "THIRD-PARTY-NOTICES.txt").write_text(
        f"Third-party software bundled with {app_name}\n\n"
        f"rclone {RCLONE_VERSION}\n  License: MIT\n  Copyright: Nick Craig-Wood and the rclone contributors\n"
        "  Source: https://github.com/rclone/rclone\n  License text: rclone-COPYING (this directory)\n\n"
        f"{app_name} itself is MIT licensed. It is not affiliated with or endorsed by\n"
        "Synology or with rclone. Hyper Backup is a trademark of Synology Inc.\n", encoding="utf-8")
    if os.name == "nt":                                            # the binary must run and be the pinned version
        line = subprocess.run([str(exe), "version"], capture_output=True, text=True).stdout.splitlines()[:1]
        if not line or not line[0].startswith(f"rclone {RCLONE_VERSION}"): die(f"rclone.exe reports {line!r}, expected {RCLONE_VERSION}")
        print(line[0])
    return exe


# ---- PyInstaller ----

def version_file(app_name, version):
    """Windows version resource (Explorer > Properties > Details, and the name in Task Manager)."""
    n = tuple(int(x) for x in version.split(".")) + (0,)
    p = BUILD / "version_info.txt"
    p.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={n}, prodvers={n}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Callido'),
      StringStruct('FileDescription', '{app_name}'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{app_name}'),
      StringStruct('LegalCopyright', 'MIT License. Not affiliated with Synology.'),
      StringStruct('OriginalFilename', '{app_name}.exe'),
      StringStruct('ProductName', '{app_name}'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
    return p


def build_app(app_name, version, rclone_exe):
    sep = os.pathsep                                               # PyInstaller SOURCE<sep>DEST
    out = DIST / app_name
    shutil.rmtree(out, ignore_errors=True); shutil.rmtree(BUILD / "work", ignore_errors=True)
    lic = BUILD / "licenses"
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--onedir",
         "--name", app_name,
         "--icon", ROOT / "assets" / "AppIcon.ico",
         "--version-file", version_file(app_name, version),
         "--specpath", BUILD, "--distpath", DIST, "--workpath", BUILD / "work",
         "--add-data", f"{ROOT / 'static'}{sep}static",
         "--add-binary", f"{rclone_exe}{sep}.",
         "--add-data", f"{ROOT / 'assets' / 'logo.png'}{sep}assets",
         "--add-data", f"{lic / 'rclone-COPYING'}{sep}Licenses",
         "--add-data", f"{lic / 'THIRD-PARTY-NOTICES.txt'}{sep}Licenses",
         "--paths", ROOT,
         "--collect-submodules", "callido_restore",
         "--hidden-import", "lz4.block", "--collect-submodules", "lz4",
         # pywebview on Windows: Edge WebView2 through pythonnet (WinForms host)
         "--hidden-import", "webview.platforms.edgechromium", "--hidden-import", "webview.platforms.winforms",
         "--collect-all", "webview", "--collect-all", "clr_loader", "--collect-all", "pythonnet",
         ROOT / "callido_restore" / "__main__.py"])
    internal = out / "_internal"
    for rel in ("static/index.html", "rclone.exe", "Licenses/rclone-COPYING", "Licenses/THIRD-PARTY-NOTICES.txt"):
        if not (internal / rel).is_file(): die(f"{rel} missing from the app folder ({internal})")
    if not (out / f"{app_name}.exe").is_file(): die(f"{app_name}.exe missing from {out}")
    return out


# ---- MSI ----

def rtf(text):
    """Plain text as a minimal RTF document (the license page of the installer)."""
    esc = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    esc = "".join(c if ord(c) < 128 else f"\\u{ord(c)}?" for c in esc)
    body = "\\par\n".join(esc.splitlines())
    return "{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0\\fswiss Segoe UI;}}\\f0\\fs18\n" + body + "\\par\n}\n"


def build_msi(app_name, version, app_dir):
    wix = shutil.which("wix") or die("wix not found (dotnet tool install --global wix --version " + WIX_VERSION + ")")
    lic = BUILD / "License.rtf"
    lic.write_text(rtf((ROOT / "LICENSE").read_text(encoding="utf-8") + "\n\n" +
                       (BUILD / "licenses" / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")), encoding="ascii")
    msi = DIST / MSI_NAME
    msi.unlink(missing_ok=True)
    run([wix, "build", HERE / "CallidoRestore.wxs", "-arch", "x64",
         "-ext", f"WixToolset.UI.wixext/{WIX_VERSION}", "-ext", f"WixToolset.Util.wixext/{WIX_VERSION}",
         "-d", f"Version={version}", "-d", f"AppDir={app_dir}", "-d", f"IconFile={ROOT / 'assets' / 'AppIcon.ico'}",
         "-d", f"LicenseRtf={lic}", "-d", f"ArtDir={HERE}",
         "-intermediatefolder", BUILD / "wixobj", "-o", msi])
    digest = hashlib.sha256(msi.read_bytes()).hexdigest()
    (DIST / (MSI_NAME + ".sha256")).write_text(f"{digest}  {MSI_NAME}\n")
    print(f"built dist/{MSI_NAME} ({msi.stat().st_size // (1 << 20)} MB, sha256 {digest})")
    return msi


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-msi", action="store_true", help="stop after the PyInstaller folder")
    ap.add_argument("--rclone-only", action="store_true", help="only download and verify rclone.exe (any OS)")
    a = ap.parse_args()
    app_name, version = about()
    exe = fetch_rclone()
    if a.rclone_only: print("rclone ok:", exe); return
    if os.name != "nt": die("the app and MSI build must run on Windows (use --rclone-only elsewhere)")
    app_dir = build_app(app_name, version, exe)
    if not a.no_msi: build_msi(app_name, version, app_dir)


if __name__ == "__main__":
    main()
