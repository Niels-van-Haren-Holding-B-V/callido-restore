# Open tasks (prioritised, self-contained; pick one, report back in the PR)

Repo: https://github.com/Niels-van-Haren-Holding-B-V/hyperbackup-desktop · read `docs/HANDOVER.md` and `docs/FORMAT.md` first.
Status snapshot at 2026-10-30 21:21: bucket-index download "dir 11 done: 2048 21:21:31"; mapper test (first 6144 buckets): "still running".
T4 (resumable index fetch, per-directory stats, Stop) and T6 (parser unit tests + ubuntu CI) are implemented in code. T1 and T2 still need the real backup and are not touched here.

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
**Done in code.** `Backup.build_pool` fetches indexes per directory with one rclone `copy` (remote → `~/.hbk-explorer/cache/<source>/` only; `--ignore-existing`, `--stats=1s --stats-one-line`). Files already at the listing size in that cache or in a configured source/mirror folder are reused and not refetched; those folders are not written. `index/<src>/pool/` is unchanged. A wrong-size cache file is renamed to `*.mismatch` inside the cache; a wrong-size source file is left in place and does not count. Either way the job errors before mapping until a correct copy exists. The restore wizard shows "Continue pool map" once any correct index file is readable, reports per-directory progress, and has Stop, which terminates the rclone copy and does not start mapping.
Acceptance still to confirm on the real download: quit mid-fetch, reopen, Continue, and check that finished index files are
unchanged and not downloaded again. Also confirm the installed rclone's `--stats` lines match the samples in
`tests/test_indexfetch.py` (parsed from rclone's current `stats.go` one-line form).

## T5 — Encrypted Hyper Backup tasks (client-side encryption)
Not supported. Investigate the encryption of `.db`/index files when `enable_data_encrypt=true` (password-derived key); out of scope
until T1–T3 are done.

## T6 — Quality
**Parser tests done.** `tests/test_hbk_pool.py` builds synthetic fixtures for a 2-chunk file_pool object, a bucket index
(header + entries + crc32), LZ4 decode with the `PoolReader.read_chunk` md5 check, and the 56-byte virtual file entry
(including a slice boundary). `tests/test_indexfetch.py` covers resume, stop, and "do not overwrite". CI is
`.github/workflows/test.yml` (ubuntu-latest, `python -m unittest`, on push and pull_request). Assumptions where the format
note is ambiguous are in the test module docstring and in `docs/HANDOVER.md`.
Flag heuristics still produce noise on Synology Drive files (mode 711). Not in this PR and still open: whitelist by share, per-extension lists in
the settings UI.

## Do not
* Do not change the on-disk layout of `~/.hbk-explorer/index/<src>/pool/*` without a migration; the full map build takes hours of downloads.
* Do not commit anything from `~/.hbk-explorer` or user paths/remotes into the repo.
