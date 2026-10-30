# Open tasks (prioritised, self-contained; pick one, report back in the PR)

Repo: https://github.com/Niels-van-Haren-Holding-B-V/hyperbackup-desktop · read `docs/HANDOVER.md` and `docs/FORMAT.md` first.
Status snapshot at 2026-10-30 21:21: bucket-index download "dir 11 done: 2048 21:21:31"; mapper test (first 6144 buckets): "still running".

## T1 — Verify the full pool map (blocking for restores)
Run `hbk_poolmap.build_poolmap(source, ~/.hbk-explorer/index/<src>/pool)` once all bucket index files are local (the app's
"Build pool map" button does this). Acceptance: `mapped == total_buckets` in `/api/pool`, no "backtrack budget exhausted".
If it stops: `Mapper.run(verbose=True)` prints the first bucket with zero candidates and each writer's reason; usually a writer's
piece table is misaligned (see "frontier" rule in `hbk_poolmap.build_models`). Add the failing case to docs/FORMAT.md.

## T2 — End-to-end restore test to the NAS
Wizard → version 2333 → a small folder (e.g. one photo folder of NIKON D3400) → target `nas:GDriveRestore/restore-test`.
Acceptance: files present with correct sizes and mtimes; pause/resume works; errors list empty. Then restore everything.

## T3 — Packaging
Run `./build_app.sh` on an Apple-silicon Mac; fix PyInstaller issues (hidden imports, `static` path, bundled `rclone` permissions).
Acceptance: the .app starts, opens the browser, "About" shows the bundled rclone version, Quit works. Tag `v0.1.0` to publish via CI.

## T4 — Robustness of the pool map step inside the app
`Backup.build_pool` fetches indexes per directory with one rclone call each; add per-directory progress from rclone `--stats` output,
resume on failure, and a "stop" button. Acceptance: closing/reopening the app continues where it left off.

## T5 — Encrypted Hyper Backup tasks (client-side encryption)
Not supported. Investigate the encryption of `.db`/index files when `enable_data_encrypt=true` (password-derived key); out of scope
until T1–T3 are done.

## T6 — Quality
Flag heuristics produce noise on Synology Drive files (mode 711). Ideas: whitelist by share, per-extension lists in settings UI.
Unit tests for `hbk_pool` parsers against small fixture files (a 2-chunk file_pool object, one bucket index header).

## Do not
* Do not change the on-disk layout of `~/.hbk-explorer/index/<src>/pool/*` without a migration; the full map build takes hours of downloads.
* Do not commit anything from `~/.hbk-explorer` or user paths/remotes into the repo.
