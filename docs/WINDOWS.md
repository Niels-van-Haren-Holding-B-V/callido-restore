# Callido Restore on Windows 10 and 11 (T14)

Goal: the same app as on macOS, installable on Windows 10 (21H2+) and 11 (x64) from an MSI. Arm64 Windows runs the x64 build
through emulation; a native arm64 MSI can follow later (rclone ships `windows-arm64`).

## How it is built

| macOS                                | Windows                                                        |
|--------------------------------------|----------------------------------------------------------------|
| `build_app.sh`                       | `packaging/windows/build_msi.py`                               |
| PyInstaller `.app` (`--windowed`)    | PyInstaller one-folder `Callido Restore\` (`--windowed`)       |
| `rclone` osx zip, gpg + sha256       | `rclone.exe` windows-amd64 zip, same key, same pin, gpg + sha256 |
| `AppIcon.icns`                       | `assets/AppIcon.ico` (`scripts/make_windows_art.py`)           |
| `hdiutil` DMG                        | WiX Toolset 5.0.2 MSI (`packaging/windows/CallidoRestore.wxs`) |
| `scripts/smoke_macos_app.sh`         | `packaging/windows/smoke_windows.py --msi …`                   |
| `build.yml` (macos-15, macos-15-intel) | `build-windows.yml` (windows-latest), separate on purpose    |

The MSI: per-machine into `C:\Program Files\Callido\Callido Restore`, Start-menu and desktop shortcut
(`DESKTOPSHORTCUT=0` skips the latter), Programs and Features entry with the app icon and callido.nl, in-place upgrades
(fixed UpgradeCode, `MajorUpgrade`), and "Open Callido Restore now" on the last page. Installer look: the app logo, the brand
gradient (`--grad` in `static/index.html`) and the Callido raccoon on the Welcome page; WiX draws the text, so no fonts are
embedded. Install, upgrade and uninstall never touch the user's data in `%USERPROFILE%\.hbk-explorer`.

The window is pywebview on Edge WebView2. Windows 11 ships the WebView2 runtime; Windows 10 gets it with Edge updates. Without
it the app falls back to the default browser (`run_window` failure path in `callido_restore/app/main.py`).

## Code changes needed (PR A)

Found by reading the code; each is a small, guarded change that leaves macOS unchanged.

1. **Local paths look like remotes.** `is_remote_path` (`sources/source.py`) and the same test in `server/api.py`
   (`/api/dest/check`) and `sources/rclone.py` (`copyfile`) say `":" in p and not p.startswith("/")`, so `C:\Restore` is
   treated as an rclone remote. On Windows a drive letter (`X:\`, `X:/`, `X:`) and a UNC path are local; rclone itself reads
   them the same way.
2. **Process liveness.** `rclone._alive` calls `os.kill(pid, 0)`; on Windows signal 0 is `CTRL_C_EVENT`. Use
   `OpenProcess` + `GetExitCodeProcess` (ctypes) there.
3. **`resource` module.** `pool/anchor.py:_rss_mb` (also used by `pool/place.py`) imports `resource`, which does not exist on
   Windows. Read `PeakWorkingSetSize` through `GetProcessMemoryInfo`, same name and MB return.
4. **Stop of the mapper.** `pool/poolmap.py` starts the mapper with `start_new_session=True` (ignored on Windows) and stops
   it with `os.killpg` (missing). On Windows: `CREATE_NEW_PROCESS_GROUP` and `taskkill /T /F /PID` so the scan workers stop too.
5. **Console windows.** Every `rclone.exe` call from the windowed app would flash a console window: pass
   `CREATE_NO_WINDOW` (one helper in `paths.py`, used at each `subprocess` call).
6. **Orphan cleanup** (`kill_orphans`, `kill_orphan_helpers`, `kill_stale`) relies on `ps` and ppid 1; it is off on Windows
   today (returns nothing). Later: the parent's liveness instead of ppid 1.
7. **Folder picker / placeholders** in `static/index.html`: the source wizard's "up" goes to `/`, and the restore-target
   placeholders say `/Volumes/...`. Server paths come back as the server sees them; the UI follows (coordinated with hbk-3).
8. `_exit_app` skips `pkill -P` on Windows; the rclone daemon is stopped explicitly, mapper groups by (4).

## Not done yet

* **Code signing.** The MSI and exe are unsigned, so SmartScreen shows "Windows protected your PC" (More info → Run anyway).
  A code-signing certificate (OV/EV, or Azure Trusted Signing) is needed to remove that; `signtool` hooks into `build_msi.py`
  after PyInstaller and after `wix build`.
* **Releases.** On a `v*` tag, `build-windows` attaches `CallidoRestore-x64.msi` and its `.sha256` to the release that
  `build-macos` publishes (from 0.4.0 on). Unsigned until there is a certificate.
* **Real-machine test** on Windows 10 and 11 with a real backup (Drive source, local NAS share as `\\nas\share` or `Z:\`).
