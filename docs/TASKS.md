# Open tasks (prioritised, self-contained; pick one, report back in the PR)

## Product goal

A macOS app anyone can use to restore a Synology Hyper Backup (`.hbk`) without Hyper Backup.
Google Drive is the first source; other sources come later. Windows support is a later goal, with macOS first.

1. A wizard sets up access to the source: rclone Google Drive auth, including your own Google client id and secret (rclone's shared client id is being retired).
2. Pick the backup, then browse a timeline per folder. The latest run (highest version id, regardless of status) is selected by default; pick the last clean run before an incident.
3. Select the folders to restore.
4. Restore to a destination on a NAS, into a new/empty folder; existing NAS data is never overwritten or deleted. A wizard explains how to prepare the NAS: an SMB share, a user and its permissions, or another protocol.

Packaging as a `.app` / `.dmg` (T3) is essential for making it usable by everyone.

**Portability:** avoid needless macOS-only choices (hardcoded paths, `/Volumes` assumptions, macOS-only packaging steps) so a Windows build stays feasible.

The Drive step already asks for your own client id and secret. Opening a backup selects the latest run (highest version id, regardless of status); pick the last clean run before an incident. Folders are chosen from Browse (or the whole backup) into the restore basket. T7 is the NAS preparation wizard on the restore destination. T8 is never overwriting existing files.

Repo: https://github.com/Niels-van-Haren-Holding-B-V/hyperbackup-desktop · read `docs/HANDOVER.md` and `docs/FORMAT.md` first.
Status snapshot at 2026-10-30 21:21: bucket-index download "dir 11 done: 2048 21:21:31"; mapper test (first 6144 buckets): "still running".
T4 (resumable index fetch, per-directory stats, Stop) and T6 (parser unit tests + ubuntu CI) are implemented in code. T1 and T2 still need the real backup and are not touched here.

## T1 — Full pool map (blocking for restores) — owner: claude-hbk-1, in progress
Approach changed on 2026-10-30 23:30 (see docs/handovers): the sequential mapper (`hbk_poolmap.py`) is obsolete. The map is now built
**anchor-first** by `hbk_anchor.py`: (1) `scan_anchors` finds every tagged record's first chunk in the bucket indexes
(`version_list.tag` = md5 of the first K chunk md5s + their total length, K ≤ 9; records whose leading chunks are dedup refs are
skipped); (2) `resolve_and_place` derives bucket owners from unique anchors, resolves copies (multi-hit records) by stream neighbours,
fills gaps between consecutive anchors with the writer's untagged records by chunk count, assigns anchor-less buckets by count, and
exports `poolmap2.json` (per-record placement segments + id ranges) that `PoolReader` already reads.
Acceptance: `/api/pool` reports the map; a sample of files from every share restores byte-exact (tag/size verified). Do not run
`build_poolmap` (v1); do not edit `hbk_anchor.py` / `hbk_poolmap2.py` without coordinating with hbk-1.

## T2 — End-to-end restore test to the NAS
Wizard → version 2333 → <a small folder> → target `<remote>:<restore-test-folder>`.
Acceptance: files present with correct sizes and mtimes; pause/resume works; errors list empty. Then restore everything.
Serves the goal: this is the end-to-end check that a chosen folder is restored onto a NAS (step 4).

## T3 — Packaging
Run `./build_app.sh` on an Apple-silicon Mac; fix PyInstaller issues (hidden imports, `static` path, bundled `rclone` permissions).
Acceptance: the .app starts in its own window, "About" shows the bundled rclone version, Quit works. Tag `v0.1.0` to publish via CI.
Serves the goal: shipping this as a `.app` / `.dmg` is what makes the app usable by everyone.
Signing and notarisation are a later step.

## T4 — Robustness of the pool map step inside the app
**Done in code.** `Backup.build_pool` fetches indexes per directory with one rclone `copy` (remote → `~/.hbk-explorer/cache/<source>/` only; `--ignore-existing`, `--stats=1s --stats-one-line`). Files already at the listing size in that cache or in a configured source/mirror folder are reused and not refetched; those folders are not written. `index/<src>/pool/` is unchanged. A wrong-size cache file is renamed to `*.mismatch` inside the cache; a wrong-size source file is left in place and does not count. Either way the job errors before mapping until a correct copy exists. The restore wizard shows "Continue pool map" once any correct index file is readable, reports per-directory progress, and has Stop, which terminates the rclone copy and does not start mapping.
Acceptance still to confirm on the real download: quit mid-fetch, reopen, Continue, and check that finished index files are
unchanged and not downloaded again. Also confirm the installed rclone's `--stats` lines match the samples in
`tests/test_indexfetch.py` (parsed from rclone's current `stats.go` one-line form).
Serves the goal: the one-time pool map has to be resumable inside the app, so a restore can be finished from the app.

## T5 — Encrypted Hyper Backup tasks (client-side encryption)
Not supported. Investigate the encryption of `.db`/index files when `enable_data_encrypt=true` (password-derived key); out of scope
until T1–T3 are done.
Serves the goal: client-side-encrypted backups are the same restore, later, once the unencrypted path works for everyone.

## T6 — Quality
**Parser tests done.** `tests/test_hbk_pool.py` builds synthetic fixtures for a 2-chunk file_pool object, a bucket index
(header + entries + crc32), LZ4 decode with the `PoolReader.read_chunk` md5 check, and the 56-byte virtual file entry
(including a slice boundary). `tests/test_indexfetch.py` covers resume, stop, and "do not overwrite". CI is
`.github/workflows/test.yml` (ubuntu-latest, `python -m unittest`, on push and pull_request). Assumptions where the format
note is ambiguous are in the test module docstring and in `docs/HANDOVER.md`.
Flag heuristics still produce noise on Synology Drive files (mode 711). Not in this PR and still open: whitelist by share, per-extension lists in
the settings UI.
Serves the goal: quieter flags and parser tests so someone restoring their own files can trust what the app shows.

## T7 — NAS destination preparation wizard
DONE (hbk-3): merged as #41; follow-ups (SMB password over POST + stdin, dest check vs restore agreement) in PR `hbk3/t7-h11-fixes`. "Prepare the NAS and connect" guide on the target step (shared folder, user + permissions, SMB, other protocols), inline SMB connect, "Check target" (new/empty/has files/inside the backup). One helper, `Source.contains_dest(dest, shares)`, checks the roots the restore writes (`dest/<share>`) against every local root and source remote, so a restore back into the original shares passes unless the backup lives in a restored share.
The restore target step lists an existing rclone remote, a share and a folder, or a local path. Add a wizard on that step that explains how to prepare the NAS, then connect the destination: an SMB share, a user and its permissions, or another protocol. The source wizard can already create an SMB remote; this wizard is for the restore destination. Acceptance: from Restore, a user with no destination remote yet can follow the steps to a target the restore job can write, and the text covers an SMB share, a user and its permissions, and another protocol. The destination is a new or empty folder, or the restore skips existing files. The wizard and the restore never overwrite or delete existing NAS data, and they refuse a destination inside the backup source.

## T8 — Never overwrite on restore
DONE (hbk-2): PR #35 (`hbk2/no-overwrite-main`, on main; replaces #31): overwrite control removed, every existing file skipped, listing failure aborts, destination inside the source refused.
Skip every file that already exists, and never overwrite or delete anything already at the destination, including a restore back into the original shares. Acceptance: an existing file with a different size or mtime is left unchanged and counted as skipped, there is no overwrite control, and the destination loses no files. Abort if the destination listing fails (never treat it as an empty target).

## T10 — Logo (see docs/HANDOVER.md, 2026-10-30 22:20)
DONE (hbk-2): merged in #18; review nits merged in #23/#28/#30. Regenerate assets with `scripts/make_logo.py --embed`.

## T11 — Desktop window (pywebview)
DONE (hbk-3): merged as #17. show static/index.html in a native macOS window (pywebview), local server on 127.0.0.1 only, no webbrowser.open, server stops when the window closes, pywebview bundled via build_app.sh, smoke test.

## T12 — UI design (see docs/HANDOVER.md, 2026-10-30 22:28)
DONE (hbk-2): branch `hbk2/ui-design-main4` on main (replaces #29/#27/#21/#10 after squash merges), only `static/index.html`.

## T13 — App name + footer (see docs/HANDOVER.md, 2026-10-30 22:31)
DONE (hbk-3): PR #25. name "Callido Restore" in one constant (window title, .app/.dmg, About, README), tagline, Synology trademark notice, footer with Callido raccoon logo + "Made by Callido · callido.nl · callido.dev" links opening in the default browser.

## H5 — First release v0.1.0 (see docs/handovers/2026-10-30-2318-nas-triage.md)
IN PROGRESS (hbk-2): release notes (`.github/RELEASE_NOTES.md`: both dmgs, Gatekeeper "Open Anyway" steps, Synology notice) and
`body_path` in the publish job. Checked: `assemble_release.sh` attaches `CallidoRestore-arm64.dmg` + `CallidoRestore-amd64.dmg` with
`SHA256SUMS`, app/dmg are named from `APP_NAME` ("Callido Restore"), `VERSION = "0.1.0"`. Tagging (`git tag v0.1.0 origin/main && git push origin v0.1.0`)
is Niels's call once main is green.

## H8 — Per-folder destination mapping (Niels, 2026-10-30 23:29; see docs/handovers/2026-10-30-2318-nas-triage.md)
IN REVIEW (hbk-3): PR #49 (branch `hbk3/dest-mapping4`, on main after #44/#45; replaces #46/#42/#40). Each local root gets its own `.hbk-restore-<job>` staging folder and `place_file`; remote roots are listed up front. Target step is a table "Folder from backup → Restore to", one row per
shared folder in the selection, defaulting to the NAS share with the same name; selected subfolders keep their relative path under
it (H13 rule), no version or source folder in between. "Check targets and preview" shows one sample path per row. Each item carries its `dest`
(`/api/restore`, persisted in the job, kept across pause/resume); `file_target()` / `restore_roots()` in `hbk_explorer.py`.
Advanced keeps "everything under one folder" (`<folder>/<share>/<path>`). Tests: `tests/test_restore_mapping.py`.

## H13 — Folder tree selection (Niels, 2026-10-31 00:01; see docs/handovers/2026-10-30-2318-nas-triage.md)
IN PROGRESS (hbk-3): after H8. The destination rule (per shared folder, subfolders keep their relative path) is already in H8;
the tree with checkboxes (lazy children, indeterminate parents, size + file count before start, selection kept across pause/resume)
comes as a separate PR stacked on H8.

## Do not
* Do not change the on-disk layout of `~/.hbk-explorer/index/<src>/pool/*` without a migration; the full map build takes hours of downloads.
* Do not commit anything from `~/.hbk-explorer` or user paths/remotes into the repo.
