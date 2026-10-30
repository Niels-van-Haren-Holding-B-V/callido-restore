# Hyper Backup Desktop (macOS, Apple silicon)

Browse a Synology **Hyper Backup** (`.hbk`) on a timeline and restore files from it **without** Hyper Backup or a working NAS.
Works on a `.hbk` that sits on Google Drive (or any rclone remote), on a NAS share over SMB, or on a local disk, complete or partial.
Built after a ransomware incident; the backup's own metadata databases are read directly, only the data you restore is downloaded.

## Product goal

A macOS app anyone can use to restore a Synology Hyper Backup (`.hbk`) without Hyper Backup.
Google Drive is the first source; other sources come later. Windows support is a later goal, with macOS first.

1. A wizard sets up access to the source: rclone Google Drive auth, including your own Google client id and secret (rclone's shared client id is being retired).
2. Pick the backup, then browse a timeline per folder, with the latest version selected by default.
3. Select the folders to restore.
4. Restore to a destination on a NAS, with a wizard that explains how to prepare the NAS: an SMB share, a user and its permissions, or another protocol.

Packaging as a signed `.app` / `.dmg` ([T3](docs/TASKS.md)) is essential for usable by everyone.

**Portability:** avoid needless macOS-only choices (hardcoded paths, `/Volumes` assumptions, macOS-only packaging steps) so a Windows build stays feasible.

* Timeline of every backup run (new/modified/removed), per-run changes, file timeline by mtime/ctime, folder browser, search with
  suspicious-file flags, version compare, single-file download.
* Restore wizard: pick a run, pick everything or folders, choose the target (back into the original NAS shares, a folder on a share,
  or a local path). Resilient: retries, parallel prefetch, pause/resume, survives restarts.
* Detects ransomware-encrypted copies (`.L0CK3D`) and reports them as unreadable.

Requires no client-side encryption on the backup task (encrypted tasks are not supported yet).

## Install
Download the `.dmg` from Releases, drag the app to Applications, open it (first start: right-click → Open, it is not notarized).
Or from source: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python hbk_explorer.py`.
`rclone` is bundled in the app; from source install it with Homebrew.

## Docs
`docs/USER-GUIDE.md`, `docs/FORMAT.md` (the reverse-engineered format), `docs/ARCHITECTURE.md`, `docs/HANDOVER.md`, `docs/TASKS.md`.

MIT licensed. Not affiliated with Synology.
