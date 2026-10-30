## T13 App name + footer (Niels, 2026-10-30 22:31 CET) — claim in docs/TASKS.md first

- App name: **Callido Restore** (window title, .app/.dmg name, About, README; keep it in one constant). Subtitle/tagline: "Restore Synology Hyper Backup (.hbk) files". About screen + README: "Not affiliated with or endorsed by Synology. Hyper Backup is a trademark of Synology Inc." Never use "Hyper Backup"/"HyperBackup" as (part of) our product name or logo, and no Synology logos/look.
- Footer (subtle, part of the T12 clean design): small Callido raccoon logo (the one used on https://callido.nl; get the file from Niels or the site, store as assets/callido-raccoon.svg/png) + "Made by Callido · callido.nl · callido.dev", both clickable links (https://callido.nl, https://callido.dev) opening in the default browser.

## T12 UI design (Niels, 2026-10-30 22:28 CET) — claim in docs/TASKS.md first

The app must look like a clean 2026 macOS app, not an 80s app, and stay simple for beginners:
- Native macOS feel: system font (-apple-system / SF Pro), generous spacing, rounded corners, subtle shadows, light + dark mode (prefers-color-scheme).
- Colours from the logo (purple to coral) as accent only; neutral background.
- One clear step at a time (wizard style), plain-language labels, one primary button per screen, helpful empty states and progress bars with ETA.
- Hide advanced options behind "Advanced"; no jargon on the main path.
- No heavy framework needed; plain CSS variables are fine. Keep it accessible (contrast, keyboard focus).

## New tasks from Niels (2026-10-30 22:20 CET) — pick one, claim it in docs/TASKS.md first ("IN PROGRESS (hbk-N): ...")

- **T10 Logo**: use `assets/logo-source.jpg` (original design, no Synology look) everywhere needed: crop white margin to a transparent PNG, generate `assets/AppIcon.icns` (all macOS sizes via iconutil), use it in build_app.sh / the PyInstaller .app and the .dmg, add favicon + header logo in static/index.html, put it at the top of README.md.
- **T11 Desktop window**: the app must run as a real desktop app, not open a browser. Use pywebview to show static/index.html (local server on 127.0.0.1 only) in a native macOS window with the app name and logo. Remove webbrowser.open, stop the server when the window closes, bundle pywebview in the .app via build_app.sh, add a smoke test.
- Note: "Hyper Backup" is Synology's product name; consider an own app name before v0.1.0 (Niels to decide).

# Handover (state as of 2026-10-30 evening)

## Situation
NAS "droopy" was hit by L0CK3D ransomware on 2026-10-28 and has since been wiped and reinstalled. The Hyper Backup target on Google
Drive (`gdrive:Synology/droopy_1.hbk`, ~1.5 TB, daily since 2020, 2333 versions) is intact; version 2333 = 2026-10-27 is the last
clean run. Goal: restore all user files of version 2333 to the new NAS with this tool (Synology's own restore is slow and opaque).

## What works (tested)
* Explorer: versions chart, changes, timeline, browse, search/flags, compare, file details, download of single files.
* Pool decoding: file_pool files and chunked files reconstruct byte-exact with md5 verification (tested on buckets 0–5).
* Restore job to a local folder and to an rclone remote (`nas:`), with prefetch, LRU cache, retries, persisted/pausable/resumable jobs.
* Source wizard (Drive OAuth via `rclone config create`, SMB, existing remote, local).

## In progress / to verify
1. **Full pool map.** Needs every bucket index (36,132 files, ~7 GB). A background download into `~/nas/gdrive_droopy_1.hbk/Pool/0/<d>/`
   was at directory 10/18 at 21:15; a chained job then runs `hbk_poolmap.build_poolmap`. The mapper was fixed to prefer the writer
   with an exact size match (`strong` candidates) — re-verify it completes without exhausting the backtrack budget. If it gets stuck,
   run `Mapper.run(verbose=True)` to see the first bucket with no candidate and inspect that writer's piece (see `docs/FORMAT.md`).
   The in-app "Build pool map" button does the same (bulk index fetch + mapping) with a progress bar.
2. A 3-file restore test to `nas:GDriveRestore/hbk-restore-test` was interrupted by a server restart; rerun it from the wizard.
3. Packaging (`.app` + `.dmg` via PyInstaller, GitHub Actions `build.yml`) is written but not yet run end to end.
4. rclone's shared Google client id is being retired in 2026: users should enter their own client id/secret in the Drive step.

## Known limitations
* Files whose chunks fall outside the mapped buckets fail with "not covered"; map must be complete.
* Dedup references to chunks in buckets not yet mapped are skipped in size checks (fine) but required for extraction.
* Record sizes for files that only exist in old versions are unknown → weaker checks there; map still works via other files.
* macOS TCC: a server started from a sandboxed process cannot read `/Volumes/*`; start the app from Terminal/Finder.

## How to test quickly
```
.venv/bin/python hbk_explorer.py --no-browser     # then open http://127.0.0.1:8765
curl 'localhost:8765/api/open?src=<id>' ; curl 'localhost:8765/api/pool?src=<id>'
```
Analysis scripts used during reverse engineering are not part of the repo; `hbk_poolmap.build_models` prints per-writer statistics.
