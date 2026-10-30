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

## T1 — Verify the full pool map (blocking for restores)
Run `hbk_poolmap.build_poolmap(source, ~/.hbk-explorer/index/<src>/pool)` once all bucket index files are local (the app's
"Build pool map" button does this). Acceptance: `mapped == total_buckets` in `/api/pool`, no "backtrack budget exhausted".
If it stops: `Mapper.run(verbose=True)` prints the first bucket with zero candidates and each writer's reason; usually a writer's
piece table is misaligned (see "frontier" rule in `hbk_poolmap.build_models`). Add the failing case to docs/FORMAT.md.
Serves the goal: a complete pool map is what lets a restore read file contents, so steps 3 and 4 can return real files.

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
`Backup.build_pool` fetches indexes per directory with one rclone call each; add per-directory progress from rclone `--stats` output,
resume on failure, and a "stop" button. Acceptance: closing/reopening the app continues where it left off.
Serves the goal: the one-time pool map has to be resumable inside the app, so a restore can be finished from the app.

## T5 — Encrypted Hyper Backup tasks (client-side encryption)
Not supported. Investigate the encryption of `.db`/index files when `enable_data_encrypt=true` (password-derived key); out of scope
until T1–T3 are done.
Serves the goal: client-side-encrypted backups are the same restore, later, once the unencrypted path works for everyone.

## T6 — Quality
Flag heuristics produce noise on Synology Drive files (mode 711). Ideas: whitelist by share, per-extension lists in settings UI.
Unit tests for `hbk_pool` parsers against small fixture files (a 2-chunk file_pool object, one bucket index header).
Serves the goal: quieter flags and parser tests so someone restoring their own files can trust what the app shows.

## T7 — NAS destination preparation wizard
The restore target step lists an existing rclone remote, a share and a folder, or a local path. Add a wizard on that step that explains how to prepare the NAS, then connect the destination: an SMB share, a user and its permissions, or another protocol. The source wizard can already create an SMB remote; this wizard is for the restore destination. Acceptance: from Restore, a user with no destination remote yet can follow the steps to a target the restore job can write, and the text covers an SMB share, a user and its permissions, and another protocol. The destination is a new or empty folder, or the restore skips existing files. The wizard and the restore never overwrite or delete existing NAS data, and they refuse a destination inside the backup source.

## T8 — Never overwrite on restore
Requirement, not current behaviour. A local file is skipped only when size and mtime both match, and is otherwise written over; a remote file whose size differs is replaced; the restore wizard has an overwrite checkbox. Remove that checkbox. Skip every file that already exists, and never overwrite or delete anything already at the destination, including a restore back into the original shares. Acceptance: an existing file with a different size or mtime is left unchanged and counted as skipped, there is no overwrite control, and the destination loses no files. Abort if the destination listing fails (never treat it as an empty target).

## T10 — Logo (see docs/HANDOVER.md, 2026-10-30 22:20)
DONE (hbk-2): PR #18 (`hbk2/logo-main`, approved; replaces #13/#8). Regenerate assets with `scripts/make_logo.py --embed`.

## T11 — Desktop window (pywebview)
DONE in PR #7 (hbk-3, branch hbk3/desktop-window, stacked on #5): show static/index.html in a native macOS window (pywebview), local server on 127.0.0.1 only, no webbrowser.open, server stops when the window closes, pywebview bundled via build_app.sh, smoke test.

## T12 — UI design (see docs/HANDOVER.md, 2026-10-30 22:28)
DONE (hbk-2): T12 branch `hbk2/ui-design-main` on top of T13 (replaces #10 after review), only `static/index.html`.

## T13 — App name + footer (see docs/HANDOVER.md, 2026-10-30 22:31)
DONE in PR #9 (hbk-3, branch hbk3/app-name, stacked on #7 + #8): name "Callido Restore" in one constant (window title, .app/.dmg, About, README), tagline, Synology trademark notice, footer with Callido raccoon logo + "Made by Callido · callido.nl · callido.dev" links opening in the default browser.

## Do not
* Do not change the on-disk layout of `~/.hbk-explorer/index/<src>/pool/*` without a migration; the full map build takes hours of downloads.
* Do not commit anything from `~/.hbk-explorer` or user paths/remotes into the repo.
