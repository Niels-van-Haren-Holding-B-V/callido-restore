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
