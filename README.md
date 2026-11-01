<img src="assets/logo.png" width="96" align="left" alt="Callido Restore logo">

# Callido Restore

Restore files from a Synology Hyper Backup, without Hyper Backup. For macOS and Windows.

<br clear="left">

Callido Restore opens a Synology **Hyper Backup** (`.hbk`) and lets you browse every backup run on a timeline, find what changed,
and restore the files you need to a NAS or a local folder. It works **without** Hyper Backup and **without** a working NAS: the
backup can sit on Google Drive (or any rclone remote), on a NAS share, or on a local disk, complete or partial.

It was built after a ransomware incident, for exactly that situation: find the last clean backup run, and get the files back.
The app reads the backup's own metadata directly and downloads only the data you restore.

## How it looks

Every backup run on a timeline: files new, modified and removed per run. A burst of red and blue at the end is what an incident
looks like.

![Timeline of backup runs, the incident run selected](docs/screenshots/overview.png)

<table>
<tr>
<td width="50%"><b>What changed in a run.</b> Encrypted copies and ransom notes are flagged.<br><img src="docs/screenshots/changes.png" alt="Changed files of the incident run with ransomware flags"></td>
<td width="50%"><b>Browse the last clean run</b> and add folders or single files to the restore.<br><img src="docs/screenshots/browse.png" alt="Folder browser of the last clean run"></td>
</tr>
<tr>
<td width="50%"><b>Restore selection.</b> Always visible, with file counts and sizes; items can be removed again.<br><img src="docs/screenshots/basket.png" alt="Restore selection panel"></td>
<td width="50%"><b>Restore.</b> Pick the run, the folders, and one target folder per shared folder.<br><img src="docs/screenshots/restore.png" alt="Restore steps"></td>
</tr>
</table>

The pictures show a small demo backup with made-up names (`scripts/make_demo_backup.py`); `scripts/screenshots.py` regenerates them.

## What it does

* **Every backup run on a timeline**, with the files new, modified and removed in each run, a per-run list of changes, a file
  timeline, a folder browser, search, and a comparison of two runs.
* **Finds the damage.** Ransomware-encrypted copies (for example `.L0CK3D`) and ransom notes are flagged, and suspicious files
  (scripts, double extensions, changed timestamps) can be searched for.
* **Restores what you pick**: everything from a run, whole shared folders, subfolders or single files, back into the original
  shares on a NAS, into a folder of your choice, or to a local path.
* **Never overwrites or deletes.** Files already at the target are skipped, and a target inside the backup itself is refused.
  After a restore, every file is reported as verified, restored from an identical copy, or unverified.
* **Survives long restores.** If the computer sleeps, the network drops or the app is closed, press **Continue** on the earlier
  restore: it resumes at the next file.
* **Downloads only what it needs** from Google Drive, with your own Google key for speed if you like (the app explains how to get
  one in five minutes).

## How a restore goes

1. **Add the backup.** Connect Google Drive (a browser window opens for the Google sign-in; read access only), a NAS over SMB, or
   pick a folder on this computer, then choose the folder whose name ends in `.hbk`.
2. **Pick the backup run.** The latest run is selected; after an incident, click the last clean run in the chart.
3. **Load the file lists** of the shared folders you need (one click per share, or all at once). Backups that carry their own
   chunk table are ready to restore right away; older ones get a one-time preparation that runs by itself in the background.
4. **Select what to restore** in the tree, or with the + buttons in Folders and Search.
5. **Choose where it goes.** Connect the NAS once (address, user, password) and pick a target folder per shared folder. A saved
   NAS connection can be edited later, for example when its address changes.
6. **Start the restore** and follow it in the progress bar.

The [user guide](docs/USER-GUIDE.md) has the details.

## Install

**macOS** (Apple silicon: `CallidoRestore-arm64.dmg`, Intel: `CallidoRestore-amd64.dmg`): download the `.dmg` from
[Releases](https://github.com/Niels-van-Haren-Holding-B-V/callido-restore/releases), drag the app to Applications and open it.
The app is not notarized, so macOS blocks the first start: on macOS 15 and later click *Done*, then go to System Settings →
Privacy & Security and click *Open Anyway*; on older macOS right-click the app → Open.

**Windows 10 and 11** (x64; Windows on Arm runs it through emulation): download `CallidoRestore-x64.msi` from
[Releases](https://github.com/Niels-van-Haren-Holding-B-V/callido-restore/releases), save it on the PC's own disk (not a network
or VM shared folder: Windows Installer cannot open it there) and double-click it. The installer is not code-signed yet, so
SmartScreen shows "Windows protected your PC": click *More info* → *Run anyway*. Compare the file with
`CallidoRestore-x64.msi.sha256` first if you like (`Get-FileHash CallidoRestore-x64.msi`). The app installs to
`C:\Program Files\Callido\Callido Restore` with a Start menu and desktop shortcut, opens in its own window (Microsoft Edge
WebView2, built into Windows 11 and current Windows 10) and is removed under Settings → Apps.

Settings, caches and `app.log` live in `~/.hbk-explorer/` (on Windows `%USERPROFILE%\.hbk-explorer`); updating or uninstalling the
app keeps them, and so keeps your connections and earlier restores.

## Limits

* Backup tasks with **client-side encryption** are not supported yet.
* Backups **without their own chunk table** are restored through a map the app infers once; on a real backup it covers most of
  the data, and files it cannot place are reported, never guessed.
* The apps are **not signed** yet (no Apple notarization, no Windows code-signing certificate), hence the first-start steps above.
* Keep the computer **awake and plugged in** during a long restore; if it stops, press Continue.

## From source

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m callido_restore
```

On Windows: `py -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`, `.venv\Scripts\python -m callido_restore`.
This opens the app window; add `--browser` to use your browser instead. Install rclone with Homebrew (macOS) or
`winget install Rclone.Rclone` (Windows). Tests: `python -m unittest discover -s tests -t .`.

Packaging: `./build_app.sh` builds the macOS `.app` and `.dmg`, `packaging/windows/build_msi.py` the Windows `.msi`
([docs/WINDOWS.md](docs/WINDOWS.md)).

## Docs

[User guide](docs/USER-GUIDE.md) · [the reverse-engineered backup format](docs/FORMAT.md) · [architecture](docs/ARCHITECTURE.md) ·
[Windows build](docs/WINDOWS.md) · [tasks](docs/TASKS.md) · [handover notes](docs/HANDOVER.md)

## Licence and credits

MIT licensed, © Callido B.V. rclone is bundled (MIT; `rclone-COPYING` and `THIRD-PARTY-NOTICES.txt` are inside the app under
`Contents/Resources/Licenses/` and on the disk image, on Windows under `_internal\Licenses\` in the install folder). The logo is
original artwork made for this project (see `assets/README.md`).

Not affiliated with or endorsed by Synology. Hyper Backup is a trademark of Synology Inc.

Made by [Callido](https://callido.nl) · [callido.nl](https://callido.nl) · [callido.dev](https://callido.dev)
