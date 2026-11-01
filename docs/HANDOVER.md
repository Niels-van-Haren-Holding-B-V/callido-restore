# Handover (state as of 2026-11-01, v1.0.0)

Read this first, then `docs/FORMAT.md` (the reverse-engineered format), `docs/ARCHITECTURE.md` and `docs/TASKS.md` (open work).
Per-session details are in `docs/handovers/` (the newest ones are the most useful: `2026-10-31-1800-claude-hbk-1.md` for the
chunk table, `2026-10-31-1530-claude-hbk6.md` for the end-to-end tests).

## Rules (owner)

* **No personal data in the repo (H18, 2026-10-31).** The repo is public. Never put real share, folder or file names, family
  names, the NAS hostname, IP addresses, local paths (`/Users/...`), session ids or version numbers of the real backup in code,
  tests, UI text, examples, PR descriptions or committed docs. Use neutral examples: shares "Documents", "Photos", "Projects",
  "homes"; users "alice"/"bob"; host "nas"; backup folder "backup_1.hbk"; IPs from 192.0.2.0/24; version 1234.
  `scripts/check_private.sh` runs in CI (`private-check`) with a denylist from the `PRIVATE_DENYLIST` secret (locally a
  gitignored `.private-denylist`); run it before pushing. Triage notes with real names stay uncommitted. History is not rewritten.
* **Never push to main.** Every change is a PR; the reviewer session (hbk-4) reviews, posts the verdict as a PR comment and
  merges what it approves (squash). Niels may merge himself too.
* **Releases only when Niels asks.** A version PR (`callido_restore/about.py`, `pyproject.toml`, `.github/RELEASE_NOTES.md`)
  is merged, then the session that opened it tags `vX.Y.Z` on the merge commit and builds the local DMG. CI (`build.yml`)
  publishes the GitHub release with both DMGs; `build-windows.yml` attaches the MSI to it. Don't wait on or babysit CI.
* **One team.** Ownership in `docs/TASKS.md` is coordination, not a fence: fix what blocks the user, note it for the owner.
* **Local smoke tests** use `HBK_SMOKE_PORT` (a free random port); 8765 is Niels's running app. Never kill `rclone rcd`
  processes while his app runs, and never copy a new DMG over one that is mounted.

## Team (Claude Code sessions, addressed by name)

| Session | Role |
| --- | --- |
| hbk-1 | pool format and reader: chunk table, inferred map (placer), record parsing |
| hbk-2 | preparation, sources, rclone daemon, packaging, releases |
| hbk-3 | UI (`static/index.html`) and the restore engine |
| hbk-4 | PR reviewer: reviews, comments and merges |
| hbk-5 | Windows port and MSI, SMB connect |
| hbk-6 | end-to-end tests on a synthetic backup (`tests/e2e/`) |

## State

**v1.0.0 (2026-11-01)**: restores from a real Synology Hyper Backup on Google Drive to a NAS work end to end, on macOS
(Apple silicon and Intel) and Windows 10/11 x64. Niels has run real restores on the Mac and on Windows 10 and 11 machines.

* **Locating data.** A backup carries its own chunk table, `Pool/chunk_index` (#152, #156, #157; format in `docs/FORMAT.md`).
  `PoolReader.plan()` reads it, so every chunk is located exactly and a backup with the table needs no preparation
  (`/api/pool` reports `chunk_index`, `complete`; no auto-start). For a backup without the table the inferred map
  (`callido_restore/pool/anchor.py`, `place.py`; 86.4 % of the bytes on the real backup) is the fallback; its preparation
  starts by itself and can be paused.
* **Records.** A file's chunk list is read through virtual-file pointer A; some records (photos) have an attribute TLV
  (`0xa93b0186`) in front of the chunk-list header (#161, #162).
* **Restore.** Never overwrites or deletes: anything already at a target is skipped, and a target inside the backup is refused.
  Pause/Continue survives a closed app (saved jobs in `~/.hbk-explorer/jobs/`). Every restored file is reported as verified,
  identical copy or unverified, and every chunk's md5 is checked on read. Errors are shown while a restore runs and go to `app.log`.
* **Sources and targets.** Google Drive (own OAuth client id/secret), SMB (connect with live progress and plain error messages,
  a saved connection can be edited), any rclone remote, local folders. One persistent `rclone rcd` serves listings and downloads
  (one backend per connection).
* **Packaging.** `./build_app.sh` (PyInstaller + hdiutil, unsigned, not notarized) and `packaging/windows/build_msi.py`
  (WiX 5 MSI, unsigned). CI: `test.yml` (unit tests on ubuntu and windows), `build.yml` (two DMGs, release on `v*` tags),
  `build-windows.yml` (MSI, attached to the release).
* **Tests.** `python -m unittest discover -s tests -t .`: unit tests per layer plus `tests/e2e/` on a synthetic backup with
  a real pool and ground truth (`HBK_E2E=1` runs the app over HTTP, `HBK_E2E_STRESS=1` the large layouts,
  `HBK_E2E_CHUNK_INDEX=1` the chunk-table path; `tests/e2e/ui_drive.py` drives the UI in headless Chrome).

## Known limitations

* Not signed: macOS Gatekeeper ("Open Anyway") and Windows SmartScreen ("Run anyway") on first start. Signing is parked by Niels.
* Client-side-encrypted backup tasks are not supported (T5).
* Chunk-table slices a restore reads are kept in the app cache (up to about 6.8 GB for a 1.5 TB backup); the user guide says so.
* Orphaned helper processes (rclone, mapper) are cleaned up at start on macOS only; on Windows that cleanup is a no-op.
* A backup without `Pool/chunk_index` depends on the inferred map; files it cannot place are reported as errors.

## How to test quickly
```
.venv/bin/python -m callido_restore --no-window --port "$HBK_SMOKE_PORT"   # then open http://127.0.0.1:$HBK_SMOKE_PORT
python -m unittest discover -s tests -t . -v
HBK_E2E=1 HBK_SMOKE_PORT=<free port> python -m unittest tests.e2e.test_app
```
