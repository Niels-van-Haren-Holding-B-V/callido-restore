# Open tasks (prioritised, self-contained; pick one, report back in the PR)

## Product goal

A macOS app anyone can use to restore a Synology Hyper Backup (`.hbk`) without Hyper Backup.
Google Drive is the first source; other sources come later. Windows support is a later goal, with macOS first.

1. A wizard sets up access to the source: rclone Google Drive auth, including your own Google client id and secret (rclone's shared client id is being retired).
2. Pick the backup, then browse a timeline per folder, with the latest version selected by default.
3. Select the folders to restore.
4. Restore to a destination on a NAS, with a wizard that explains how to prepare the NAS: an SMB share, a user and its permissions, or another protocol.

Packaging as a signed `.app` / `.dmg` (T3) is essential for usable by everyone.

**Portability:** avoid needless macOS-only choices (hardcoded paths, `/Volumes` assumptions, macOS-only packaging steps) so a Windows build stays feasible.

The Drive step already asks for your own client id and secret, opening a backup selects the latest run, and folders are chosen from Browse (or the whole backup) into the restore basket. T7 is the missing NAS preparation wizard on the restore destination.

Repo: https://github.com/Niels-van-Haren-Holding-B-V/hyperbackup-desktop · read `docs/HANDOVER.md` and `docs/FORMAT.md` first.
Status snapshot at 2026-10-30 21:21: bucket-index download "dir 11 done: 2048 21:21:31"; mapper test (first 6144 buckets): "still running".

## T1 — Verify the full pool map (blocking for restores)
Run `hbk_poolmap.build_poolmap(source, ~/.hbk-explorer/index/<src>/pool)` once all bucket index files are local (the app's
"Build pool map" button does this). Acceptance: `mapped == total_buckets` in `/api/pool`, no "backtrack budget exhausted".
If it stops: `Mapper.run(verbose=True)` prints the first bucket with zero candidates and each writer's reason; usually a writer's
piece table is misaligned (see "frontier" rule in `hbk_poolmap.build_models`). Add the failing case to docs/FORMAT.md.
Serves the goal: a complete pool map is what lets a restore read file contents, so steps 3 and 4 can return real files.

## T2 — End-to-end restore test to the NAS
Wizard → version 2333 → a small folder (e.g. one photo folder of NIKON D3400) → target `nas:GDriveRestore/restore-test`.
Acceptance: files present with correct sizes and mtimes; pause/resume works; errors list empty. Then restore everything.
Serves the goal: this is the end-to-end check that a chosen folder is restored onto a NAS (step 4).

## T3 — Packaging
Run `./build_app.sh` on an Apple-silicon Mac; fix PyInstaller issues (hidden imports, `static` path, bundled `rclone` permissions).
Acceptance: the .app starts, opens the browser, "About" shows the bundled rclone version, Quit works. Tag `v0.1.0` to publish via CI.
Serves the goal: shipping this as a signed `.app` / `.dmg` is what makes the app usable by everyone.

## T4 — Robustness of the pool map step inside the app
`Backup.build_pool` fetches indexes per directory with one rclone call each; add per-directory progress from rclone `--stats` output,
resume on failure, and a "stop" button. Acceptance: closing/reopening the app continues where it left off.
Serves the goal: the one-time pool map has to be resumable inside the app, so a restore can be finished from the signed app.

## T5 — Encrypted Hyper Backup tasks (client-side encryption)
Not supported. Investigate the encryption of `.db`/index files when `enable_data_encrypt=true` (password-derived key); out of scope
until T1–T3 are done.
Serves the goal: client-side-encrypted backups are the same restore, later, once the unencrypted path works for everyone.

## T6 — Quality
Flag heuristics produce noise on Synology Drive files (mode 711). Ideas: whitelist by share, per-extension lists in settings UI.
Unit tests for `hbk_pool` parsers against small fixture files (a 2-chunk file_pool object, one bucket index header).
Serves the goal: quieter flags and parser tests so someone restoring their own files can trust what the app shows.

## T7 — NAS destination preparation wizard
The restore target step lists an existing rclone remote, a share and a folder, or a local path. Add a wizard on that step that explains how to prepare the NAS, then connect the destination: an SMB share, a user and its permissions, or another protocol. The source wizard can already create an SMB remote; this wizard is for the restore destination. Acceptance: from Restore, a user with no destination remote yet can follow the steps to a target the restore job can write, and the text covers an SMB share, a user and its permissions, and another protocol.

## Do not
* Do not change the on-disk layout of `~/.hbk-explorer/index/<src>/pool/*` without a migration; the full map build takes hours of downloads.
* Do not commit anything from `~/.hbk-explorer` or user paths/remotes into the repo.
