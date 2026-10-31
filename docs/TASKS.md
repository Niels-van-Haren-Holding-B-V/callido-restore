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
Approach changed on 2026-10-30 23:30 (see docs/handovers): the sequential mapper (`callido_restore/pool/poolmap.py`) is obsolete. The map is now built
**anchor-first** by `callido_restore/pool/anchor.py`: (1) `scan_anchors` finds every tagged record's first chunk in the bucket indexes
(`version_list.tag` = md5 of the first K chunk md5s + their total length, K ≤ 9; records whose leading chunks are dedup refs are
skipped); (2) `resolve_and_place` derives bucket owners from unique anchors, resolves copies (multi-hit records) by stream neighbours,
fills gaps between consecutive anchors with the writer's untagged records by chunk count, assigns anchor-less buckets by count, and
exports `poolmap2.json` (per-record placement segments + id ranges) that `PoolReader` already reads.
Acceptance: `/api/pool` reports the map; a sample of files from every share restores byte-exact (tag/size verified). Do not run
`build_poolmap` (v1); do not edit `callido_restore/pool/anchor.py` / `poolmap2.py` without coordinating with hbk-1.
Wired into the app by hbk-2 on 2026-10-31 (0.3.4): `Backup.build_pool` runs `poolmap.build_map` in a child process
(`build_poolmap_detached`, app entry `--mapper`), which is `build_poolmap_anchor` = `poolmap2.build_writers` → `anchor.scan_anchors`
→ `anchor.resolve_and_place` → `anchor.export_map` (poolmap2.json); the v1 search only with `HBK_MAPPER=v1`. Until then the app
still ran v1, which sat at bucket 3000/36132 for 15 minutes on the real backup. hbk-2 also edited anchor.py for 0.3.6 (Niels: one team, fix what is in the way): workers print `progress k` to stderr and the
parent logs `scanned i/N` every 2 s; `resolve_and_place` logs `resolving copies: pass p/3, i/N records` and
`placing writer w: k/N anchors` every 2 s (the job maps them to 95-100 %). The `--scan-worker` dispatch is in the app entry since 0.3.5 (`anchor._worker_command`), so the frozen app scans with
MAX_WORKERS processes like a checkout; the one-worker fallback is gone (hbk-2, coordinated through this note).

**Measured on the real backup, 2026-10-31 11:50 (hbk-2, full pipeline from the app's data, 4 scan workers):** record meta 2 s;
writer tables 3 s; anchor scan 454 s (277,198 of 277,493 tagged records anchored, 11,742 multi-hit); placement 545 s;
result **13,656 of 36,132 buckets owned** (directories 5-7 of 18 almost entirely unowned: 0, 1, 6 buckets), placed 260,220
records + 12,107 gap-filled, unplaced 9,082, gap-unfilled 41,301, free-assigned 3,519, owner-conflicts 2,440. A random
sample of files per share plans (all chunks placed) for 78-91 % of the files but only 11-51 % of the bytes: the large
files sit in unplaced records. Two mid-sized files per share restored through that map byte-exact (every chunk md5
verified). `rhythm(b)` agrees with the anchored owners in directory 0 only (1539/1554) and 37 % overall; consecutive owned
buckets of one writer are mostly adjacent (gap 1: 4,906 of 13,652), so writers write runs, not round-robin. This is the
open core of T1: ownership of anchor-less bucket runs. hbk-2 keeps the scratch map and timing log on request.

2026-10-31 13:05 (hbk-1, docs/handovers/2026-10-31-1305-claude-hbk-1.md): the placer's assumptions were wrong in three ways, measured on
the real backup. (1) Content dedup: a file of two or more chunks backed up again gets new chunk ids but stores nothing; 145 k
tagged records (63 M chunks of the 240 M listed) are such phantoms, which shifted every stream offset after them. (2) Bucket ids
are reused; time order is `(index rev, bucket id)`, and a record whose ids did not exist at its bucket's revision is a phantom of
an untagged copy. (3) With phantoms removed the stream-offset pins are consistent (13,911 against 538) and the anchorless
buckets, the interiors of large files, are accounted for by the pins to within a few percent. v3 of `resolve_and_place` (time
order, phantom aliasing, pins, a byte-and-count partition of the anchorless buckets, every placed record verified by its ulen
sum against the file size) is in `callido_restore/pool/place.py` (default; `HBK_PLACER=v2` for the old placer). Measured on
the real backup: 618 k of 845 k records placed, 74.5 % of the bytes, 454 k records verified by their byte sum, 1,279 of 1,593
files > 100 MB (the old placer: 57.6 % of bytes, 696 large files). What is left sits mostly in the initial backup, where the
buckets are so alike that the interior search of a large file is ambiguous and is left unplaced rather than guessed; next:
a model of the initial backup's bucket allocation (ids handed out four at a time, round-robin), then T2.

## T2 — End-to-end restore test to the NAS
Wizard → version 1234 (the last clean run) → <a small folder> → target `<remote>:<restore-test-folder>`.
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
**Parser tests done.** `tests/core/test_pool.py` builds synthetic fixtures for a 2-chunk file_pool object, a bucket index
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
is the owner's call once main is green.

## H18 — No personal data in the repo (owner, 2026-10-31 00:35; rule in docs/HANDOVER.md)
DONE (hbk-2): #51 (docs, handovers, `tests/test_hbk_pool.py`, `scripts/check_private.sh`, CI job `private-check`); hbk-3 scrubbed
the tests, `hbk_explorer.py` (now `callido_restore/`) and `static/index.html` in #49/#52. Open for the owner: add the `PRIVATE_DENYLIST` repository secret
(one extended regex per line); until then CI runs only the built-in patterns. Run `scripts/check_private.sh` locally before pushing.

## H17 — Exact folder selection, safe resume (Reviewer, 2026-10-31 00:34)
DONE (hbk-3): #53. `restore_plan` and `restore_count` select a folder by an exact,
case-sensitive prefix (`substr(path, 1, n) = 'dir/'`) instead of `LIKE 'dir/%'` (case-insensitive, `_`/`%` wildcards). One
`restore_target_error()` validates targets for `/api/restore` (creates roots) and `/api/restore/resume` (roots must still be
there, source check again); a resume that fails keeps the job file. Neutral example names in the files H8 owns (H18 rule).

## H8 — Per-folder destination mapping (owner, 2026-10-30 23:29)
DONE (hbk-3): #49. "Folder from backup → Restore to" table, one row per shared folder (default: the NAS share with the same name);
subfolders keep their relative path (H13 rule); each item carries its `dest` (persisted, kept across pause/resume); one
`.hbk-restore-<job>` staging folder and `place_file` per local root. Tests in `tests/test_restore_mapping.py`.

## H13 — Folder tree selection (owner, 2026-10-31 00:01)
DONE (hbk-3): #52. Tree with checkboxes in step 2 (children on demand, indeterminate parents, inclusion-list basket), live
"Selected: N files · size" via `/api/restore_count`.

## H16 — Restructure into packages (owner, 2026-10-31 01:01; docs/handovers/2026-10-31-0101-nas-triage-H16-now.md)
DONE (hbk-2): #55 (core), #57 (pool), and one PR for the rest (owner asked for it in one go): `callido_restore/` with `paths.py`,
`about.py`, `core/` (`pool.py`, `hbkmeta.py`), `pool/`, `sources/source.py`, `restore/` (`destination.py`, `jobs.py`, `backup.py`),
`server/api.py`, `app/main.py`; entry point `python -m callido_restore` (`callido_restore/__main__.py`, also the PyInstaller script);
root shims and `hbk_explorer.py` removed; `tests/` mirrors the layers; `pyproject.toml` with the `callido-restore` script. Pure moves,
no behaviour change. Follow-up candidates: split `Backup` (snapshot index/queries vs restore engine), an in-app anchor scan needs a
`--worker` dispatch in the entry point.

## H21 — Persistent rclone daemon (hbk-2, 2026-10-31 01:50)
DONE (hbk-2): the app starts one `rclone rcd` on a random 127.0.0.1 port with random credentials (`callido_restore/sources/rclone.py`)
and sends listings through its RC API (`ls`, `remotes`, `version`): 0.3 s instead of 1.3-5 s per Google Drive listing. Subprocess
fallback when the daemon is not running (tests, failed start). 30 s listing cache for UI renders (`cache=True`, `forget()` after a new
connection). Copies, config changes and the index fetch still run as separate rclone processes; moving them to the daemon
(`operations/copyfile` with `_async` + `job/status` for progress) is the next step.
DONE (hbk-2, #79): downloads through the daemon (`copyfile`), four record-set slices at a time with live bytes. Follow-up from the
triage note of 2026-10-31 09:17 (hbk-2, claimed 09:35): the daemon gets the same Drive throttling as the one-process fallback
(`--tpslimit 8 --drive-pacer-min-sleep 100ms --low-level-retries 20`, `rclone.THROTTLE`), progress counts rclone's group bytes once
(they already include the in-flight bytes), `rclone.Cancelled` instead of a string compare, tests.
Crash leftovers (hbk-2, 2026-10-31 10:10): an app that died with its disk image left its `rclone rcd` and a `copyto` running for
six hours at full CPU, on the user's Drive quota, and they ignored SIGTERM; `kill_orphans()` at start stops bundled rclone
processes whose parent is gone, `_terminate()` follows SIGTERM with SIGKILL. The bucket-index phase now feeds rclone's
transferred bytes into the job between completed files (the UI speed read 6.7 KB/s while rclone pulled 2 MB/s).

## v0.2.0 — first-run fixes and UI responsiveness (Niels's DMG test, 2026-10-31)
DONE: #59 (hbk-3: Google sign-in guidance, throttle-proof job waits, one-click pick of the detected backup, open-backup job with
step progress, one listing call per click, @AppConfig labelled), #61 (hbk-3: header activity pill, toasts with Undo, always-visible
restore selection panel, state-aware + buttons, step-3 "Not started" text), #63 (hbk-2: persistent rclone daemon with 30 s cache,
pid file, VERSION 0.2.0, release notes). Open: auto-start of the pool preparation (Niels decides), OAuth client "Callido Restore".

## Do not
* Do not change the on-disk layout of `~/.hbk-explorer/index/<src>/pool/*` without a migration; the full map build takes hours of downloads.
* Do not commit anything from `~/.hbk-explorer` or user paths/remotes into the repo, and no real share/folder/host names (H18 rule in docs/HANDOVER.md).
