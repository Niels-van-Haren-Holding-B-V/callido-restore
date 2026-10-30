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

## Cloud agent handover, 2026-10-30 (review)

Wrong-size bucket indexes are not treated as complete, and `Backup.build_pool` does not start the mapper while any required index is missing or the wrong size. Remotes and configured source folders (the `.hbk` itself, extra roots, and a user-made mirror) are read-only: nothing in this change writes, renames, or deletes there.

New downloads go to `~/.hbk-explorer/cache/<source-id>/` (same relative paths as in the backup). `index/<src>/pool/*` is unchanged. Indexes already on disk in the configured local path or an extra root — including the ~7 GB download in progress — are **read from that path and not fetched again**, and they are not moved or linked. That is the migration: the old location stays valid. If that local path was only a mirror you created for the tool, it is handled the same way (still read-only from now on; new files join the app cache, existing files are reused in place). A wrong-size file **in the app cache** is renamed to `<name>.mismatch` inside the cache and fetched again. A wrong-size file **in a source/mirror folder** is left byte-for-byte untouched; a correct copy is fetched into the cache instead. `indexes_local` counts only files whose size matches the listing, in the cache or in a source folder.

T6 here is parser and fetch tests only. Flag-heuristic noise (mode 711, whitelist by share) is still open and not in this PR. T1/T2 still need the real backup.

How to test: `python -m unittest discover -s tests -t . -v`. On the real backup, quit mid-fetch, reopen, Continue: finished indexes in the old folder must be unchanged and not downloaded again, and that folder must gain no new files.



## Situation
NAS "droopy" was hit by L0CK3D ransomware on 2026-10-28 and has since been wiped and reinstalled. The Hyper Backup target on Google
Drive (`gdrive:Synology/droopy_1.hbk`, ~1.5 TB, daily since 2020, 2333 versions) is intact; version 2333 = 2026-10-27 is the last
clean run. Goal: restore all user files of version 2333 to the new NAS with this tool (Synology's own restore is slow and opaque).

## What works (tested)
* Explorer: versions chart, changes, timeline, browse, search/flags, compare, file details, download of single files.
* Pool decoding: file_pool files and chunked files reconstruct byte-exact with md5 verification (tested on buckets 0–5).
* Restore job to a local folder and to an rclone remote (`nas:`), with prefetch, LRU cache, retries, persisted/pausable/resumable jobs.
* Source wizard (Drive OAuth via `rclone config create`, SMB, existing remote, local).
* Pool-map index fetch resumes. A file already at the listing size (in `~/.hbk-explorer/cache/<source>/` or in a configured source/mirror folder) is skipped. rclone only copies from the remote into the app cache (`--ignore-existing`, no delete). Stop cancels that copy. Source folders are not modified. `index/<src>/pool/` is unchanged.

## In progress / to verify
1. **Full pool map.** Needs every bucket index (36,132 files, ~7 GB). A background download into `~/nas/gdrive_droopy_1.hbk/Pool/0/<d>/`
   was at directory 10/18 at 21:15; a chained job then runs `hbk_poolmap.build_poolmap`. The mapper was fixed to prefer the writer
   with an exact size match (`strong` candidates) — re-verify it completes without exhausting the backtrack budget. If it gets stuck,
   run `Mapper.run(verbose=True)` to see the first bucket with no candidate and inspect that writer's piece (see `docs/FORMAT.md`).
   The in-app "Build pool map" / "Continue pool map" button does the same (bulk index fetch + mapping). The fetch skips finished
   directories and can be stopped; this has unit tests but has not been run against the live download (see below).
2. A 3-file restore test to `nas:GDriveRestore/hbk-restore-test` was interrupted by a server restart; rerun it from the wizard.
3. Packaging (`.app` + `.dmg` via PyInstaller, GitHub Actions `build.yml`) is written but not yet run end to end.
4. rclone's shared Google client id is being retired in 2026: users should enter their own client id/secret in the Drive step.

## Known limitations
* Files whose chunks fall outside the mapped buckets fail with "not covered"; map must be complete.
* Dedup references to chunks in buckets not yet mapped are skipped in size checks (fine) but required for extraction.
* Record sizes for files that only exist in old versions are unknown → weaker checks there; map still works via other files.
* macOS TCC: a server started from a sandboxed process cannot read `/Volumes/*`; start the app from Terminal/Finder.
* A wrong-size index in a source folder is left untouched and does not count. One in the app cache is renamed to `*.mismatch` inside the cache. The build fails before mapping until a correct-sized copy is readable.
* Stop is honored between pool-map stages and while the bucket-index rclone copy is running. It does not interrupt the chunk-record
  parse or the mapper once that stage has started.
* Parser tests (T6) assume the current code where the format note is ambiguous: bucket-index crc32 is not checked; virtual-file `set`
  is taken from pointer B (word 12) and `mtime` is word 9 only, not the u64 in words 9..10; pointer B is not required to be pointer A
  minus 12. The pure-python LZ4 fallback does not check the uncompressed size; the `lz4` package does.

## How to test quickly
```
.venv/bin/python hbk_explorer.py --no-browser     # then open http://127.0.0.1:8765
curl 'localhost:8765/api/open?src=<id>' ; curl 'localhost:8765/api/pool?src=<id>'
python -m unittest discover -s tests -t . -v      # parser + index-fetch tests (no real backup)
```
Analysis scripts used during reverse engineering are not part of the repo; `hbk_poolmap.build_models` prints per-writer statistics.
