# Tasks (state as of 2026-11-01, v1.0.0)

Pick an open task, claim it here ("IN PROGRESS (hbk-N): ..."), work on a branch, open a PR; the reviewer (hbk-4) reviews and
merges. Read `docs/HANDOVER.md` (rules, team, state) and `docs/FORMAT.md` first.
Repo: https://github.com/Niels-van-Haren-Holding-B-V/callido-restore

## Product goal

A macOS and Windows app anyone can use to restore a Synology Hyper Backup (`.hbk`) without Hyper Backup.
Google Drive is the first source; other sources come later. macOS (Apple silicon and Intel) and Windows 10/11 (x64) are built from the same code.

1. A wizard sets up access to the source: rclone Google Drive auth, including your own Google client id and secret (rclone's shared client id is being retired).
2. Pick the backup, then browse a timeline per folder. The latest run (highest version id, regardless of status) is selected by default; pick the last clean run before an incident.
3. Select the folders to restore.
4. Restore to a destination on a NAS, into a new/empty folder; existing NAS data is never overwritten or deleted. A wizard explains how to prepare the NAS: an SMB share, a user and its permissions, or another protocol.

**Portability:** avoid needless macOS-only choices (hardcoded paths, `/Volumes` assumptions, macOS-only packaging steps); Windows is built from the same code.

## Open

### T5 — Encrypted Hyper Backup tasks (client-side encryption)
Not supported. Investigate the encryption of `.db`/index files when `enable_data_encrypt=true` (password-derived key).
Serves the goal: client-side-encrypted backups are the same restore, once someone needs it.

### T6 — Flag noise
Parser tests are done (`tests/core/test_pool.py`, `tests/pool/`). Still open: the ransomware/suspicious-file heuristics are
noisy on Synology Drive files (mode 711). Ideas: whitelist by share, per-extension lists in a settings screen.

### T14 — Windows (follow-up)
Done: MSI (#108), portability (#112), real-machine test on Windows 10 and 11 (Niels). Code signing is parked by Niels.
Open: orphan cleanup on Windows (`kill_orphans`, `kill_orphan_helpers`, `kill_stale` are no-ops there; port to CIM
`Win32_Process` with parent liveness instead of ppid 1). See `docs/WINDOWS.md`.

### Smaller follow-ups (from reviews)
* Chunk-table slices are never evicted from the app cache (documented in the user guide); evict them like bucket data if the
  cache limit (`HBK_CACHE_GB`) should cover them.
* `/api/job` returns the whole `errors` list on every poll; trim it to the last 500 for a running job.
* A slow remote `listdir` in `PoolReader.bucket_name()` holds its lock for all threads (once per 2048-bucket directory).
* Signing and notarisation (macOS) and Windows code signing: parked by Niels.

## Done (summary; details in the PRs and `docs/handovers/`)

* **T1 Locating data** (hbk-1): the backup's own chunk table `Pool/chunk_index` (#152, #156, #157) locates every chunk exactly,
  so no preparation is needed. The inferred map (anchor-first scan + placer v3, #99–#151) is the fallback for backups without
  the table: 86.4 % of the bytes on the real backup.
* **T2 End-to-end restore** to a NAS: done by Niels on the real backup (v0.4.x/v1.0.0); fixes from it: chunk lists via pointer A
  for records with an attribute TLV (#161, #162), errors shown live and in `app.log` (#160).
* **T3 Packaging**: `.app`/`.dmg` (`build_app.sh`, CI `build.yml`), releases v0.1.0 … v1.0.0.
* **T4 Resumable preparation**: index fetch resumes, Stop/Pause, auto-start with a persisted pause (#105/#106).
* **T7 NAS preparation wizard**, **T8 never overwrite**, **T10 logo**, **T11 desktop window**, **T12 UI design**,
  **T13 app name + footer**.
* **T14 Windows**: see Open for the one follow-up.
* **T15 End-to-end tests** (hbk-6): `tests/e2e/` (synthetic backup with ground truth, the chunk-table path, the app over HTTP,
  headless UI drive).
* **H-items** (H5 first release, H8 per-folder targets, H13 folder tree, H16 packages, H17 exact selection, H18 no personal
  data, H21 rclone daemon): done.
* **v1.0.0 extras**: editing a saved NAS connection (#164, #167), restore step 4 queue (#140, #144), SMB connect feedback and
  errors (#138, #139, #141, #143), one-line header (#166), README for v1.0.0 (#165).

## Do not
* Do not change the on-disk layout of `~/.hbk-explorer/index/<src>/pool/*` without a migration.
* Do not commit anything from `~/.hbk-explorer` or user paths/remotes into the repo, and no real share/folder/host names (H18 rule in docs/HANDOVER.md).
* Do not release unless Niels asks.
