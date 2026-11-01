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
(Apple silicon and Intel) and Windows 10/11 x64. Niels has run partial real restores (Mac, and Windows 10 and 11 machines); a full restore of one share is T2 in `docs/TASKS.md`.

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
  (one backend per connection); the bucket-index download, writes to a NAS target and connection setup still run as separate
  rclone processes.
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
* What `Pool/chunk_index` holds for unused or purged chunk ids is not checked yet.
* On a slow line a restore is download-bound and fetches whole buckets (see "Restore speed" in `docs/TASKS.md`).

## How to test quickly
```
.venv/bin/python -m callido_restore --no-window --port "$HBK_SMOKE_PORT"   # then open http://127.0.0.1:$HBK_SMOKE_PORT
python -m unittest discover -s tests -t . -v
HBK_E2E=1 HBK_SMOKE_PORT=<free port> python -m unittest tests.e2e.test_app
```

## Release signing and updates

The app checks GitHub for a newer release once a day (`callido_restore/update.py`) and installs it only when the release is
signed with the Callido release key. The setting in the app: "only mention" (default: shown, installed on a click), "install
automatically" (the same download and checks without a click, started every 10 minutes by `server/api.py` `auto_update_tick`
only when no restore or preparation runs, and asked again right before the app closes; a failed automatic install is not
retried for that release and stays offered for a click; on macOS it never opens the disk image unasked) or "don't check" (also
`HBK_NO_UPDATE_CHECK=1`).

* **Who holds the key.** An Ed25519 key pair. The private key exists only as the GitHub Actions secret `RELEASE_SIGNING_KEY`
  (Niels adds it; nobody else keeps a copy, the local file is deleted after adding it). The public key is in
  `callido_restore/about.py`, `UPDATE_PUBLIC_KEYS` (hex, 32 bytes).
* **What is signed.** `build.yml` signs `SHA256SUMS` (the two disk images) and `build-windows.yml` signs
  `CallidoRestore-x64.msi.sha256`; the signatures are attached to the release as `<list>.sig`. The signed data is `callido-restore <tag>\n` followed by the list (`update.signed_message`), so a validly signed list of an old release cannot be re-published under a newer tag. The app verifies the list's
  signature (`callido_restore/ed25519.py`, pure Python, RFC 8032), then the installer's SHA-256 against the signed list, and
  refuses a tag that is not strictly newer than its own version or a file that is not this platform's. HTTPS only, to
  `api.github.com` and GitHub's release-asset hosts. Any failure deletes the download; every step is logged to `app.log`.
* **Creating the key** (once, by Niels, OpenSSL 3, e.g. `brew install openssl@3`):
  ```
  openssl genpkey -algorithm ed25519 -out release-signing.pem
  gh secret set RELEASE_SIGNING_KEY -R Niels-van-Haren-Holding-B-V/callido-restore < release-signing.pem
  openssl pkey -in release-signing.pem -pubout -outform DER | tail -c 32 | xxd -p -c 64
  rm -P release-signing.pem
  ```
  The last line but one prints the public key: it goes into `UPDATE_PUBLIC_KEYS` in a PR. Updates become installable from the
  first release built after that PR, for apps that carry the key.
* **Rotating the key.** Create a new pair, add its public key to `UPDATE_PUBLIC_KEYS` next to the old one, release that version
  signed with the old key (installed apps trust it), then replace the secret with the new private key and remove the old public
  key in a later release. If the private key leaks: remove its public key from `UPDATE_PUBLIC_KEYS` at once and release; apps
  that still trust it can be updated only from the release page.
* **macOS install.** The app downloads the disk image itself (no quarantine flag, checked on a real Mac), mounts it, copies the
  new app next to the running one and restarts through a helper that swaps the bundles after the app has quit (the old one is
  put back if the swap fails). When the app's folder is not writable it opens the verified disk image for a manual drag.
  **Windows install** (`update_windows.py`, hbk-5): `msiexec /i … /passive` upgrades in place (same UpgradeCode), then restarts.
* **Never during work.** `/api/update/install` refuses while a restore, a preparation or another update runs; a paused restore
  continues after the update.

