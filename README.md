# Hyper Backup Desktop (macOS, Apple silicon)

Browse a Synology **Hyper Backup** (`.hbk`) on a timeline and restore files from it **without** Hyper Backup or a working NAS.
Works on a `.hbk` that sits on Google Drive (or any rclone remote), on a NAS share over SMB, or on a local disk, complete or partial.
Built after a ransomware incident; the backup's own metadata databases are read directly, only the data you restore is downloaded.

* Timeline of every backup run (new/modified/removed), per-run changes, file timeline by mtime/ctime, folder browser, search with
  suspicious-file flags, version compare, single-file download.
* Restore wizard: pick a run, pick everything or folders, choose the target (back into the original NAS shares, a folder on a share,
  or a local path). Resilient: retries, parallel prefetch, pause/resume, survives restarts.
* Detects ransomware-encrypted copies (`.L0CK3D`) and reports them as unreadable.

Requires no client-side encryption on the backup task (encrypted tasks are not supported yet).

## Install
Download the `.dmg` from Releases, drag the app to Applications and open it. The app is not notarized, so macOS blocks the first start:
on macOS 15 and later click *Done*, then go to System Settings → Privacy & Security and click *Open Anyway*; on older macOS right-click the app → Open.
Settings, caches and `app.log` live in `~/.hbk-explorer/`.
Or from source: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python hbk_explorer.py` (opens the app
window; add `--browser` to use your browser instead).
`rclone` is bundled in the app (MIT; `rclone-COPYING` and `THIRD-PARTY-NOTICES.txt` are inside the app under `Contents/Resources/Licenses/` and on the disk image). From source, install rclone with Homebrew.

## Docs
`docs/USER-GUIDE.md`, `docs/FORMAT.md` (the reverse-engineered format), `docs/ARCHITECTURE.md`, `docs/HANDOVER.md`.

MIT licensed. Not affiliated with Synology.
