# User guide

1. Start the app (or `python3 -m callido_restore` from a checkout). It opens its own window; the UI is served on http://127.0.0.1:8765 only on this
   machine. Closing the window stops the app (running restores are saved and can be resumed). `--browser` opens the UI in your
   browser instead; `--no-window` runs only the server.
2. **+ Add backup** → choose where the `.hbk` lives: an rclone remote you already have, Google Drive (sign in; with or without a
   Google key of your own, see "Google Drive: a key of your own" below), a NAS over SMB, or a local folder → browse to the `.hbk`
   folder → name it.

   <img src="screenshots/guide-1-add-backup.png" width="560" alt="Add a backup, step 1: where the .hbk folder is">

3. The overview shows the task, the shares and all backup runs. Click a run in the chart (or type its number in "Go to run"); the
   latest run is selected by default. Each shared folder of a run has its own file list: click a chip to load it, or **Load all shares
   for this run** (a few MB each). The chip then shows the number of files and their size.

   <img src="screenshots/guide-2-load-lists.png" alt="Run 102 with one chip per shared folder, each still to be loaded">

4. Tabs: **What changed** (what that run added, changed or removed, with ransomware flags), **Folders**, **Search**, **Timeline**,
   **Compare runs** and **Restore**.
5. **Restore**, in four steps:
   * **Which backup run?** After an incident, pick the last run before it.
   * **What to restore?** Tick shared folders or subfolders in the tree (▸ opens a folder; ticking a folder takes everything under
     it; untick a subfolder to leave it out). The selected file count and size update as you go. Folders and files can also be added
     with the + buttons under Folders or Search. A shared folder whose file list is not loaded yet loads when you tick it.

     <img src="screenshots/guide-3-select.png" alt="The tree: Documents ticked, Photos partly ticked (2025 yes, 2026 no), homes not ticked">

   * **Where should the files go?** One row per selected shared folder, defaulting to the NAS share with the same name: the
     folder's contents land there directly, with no version or source folder in between. Pick another share or sub-folder per row,
     or choose "a mounted share / local path" as the connection and type a path per row. "Advanced" can put everything under one
     folder as `<folder>/<share>/<path>` instead. No NAS connection yet? Open "Prepare the NAS and connect": it explains the shared
     folder, user and SMB settings and connects from there. "Check targets and preview" tells you per row whether the folder is new,
     empty or already has files, and shows one sample file with its final path; a folder inside the backup itself (or around it) is
     refused.
   * **Check and start.** "Count files and size" shows what will be restored and where. A backup made by Hyper Backup records where
     every piece of every file is stored, so it is ready to restore at once:

     <img src="screenshots/guide-4-ready.png" alt="Step 4: Start restore, 'Ready to restore', 18 files and where they go">

     A backup without that record needs a one-time preparation first (the app downloads the backup's index files and builds a
     map; it starts by itself and shows its progress on one line). You can press **Start when the backup is prepared**: the
     restore then starts by itself as soon as the preparation is done.
   * **The result.** The bar shows the restore while it runs. Under it, "Earlier restores you can continue" lists the restore with
     **Pause**; a paused restore, or one cut short by a closed app, a crash or a restart, keeps its progress and goes on with
     **Continue**. When it ends, one line sums it up: restored, skipped (already at the target), errors, size, and how many files were
     **checked** (the file's place in the backup was confirmed and every block's checksum matched while it was read). Files that
     **couldn't be verified** were restored, but check them before you rely on them; files that could not be restored at all are
     listed under **Errors**.

     <img src="screenshots/guide-5-result.png" alt="Restore finished: 24 restored, 2 errors, 22 checked, 2 couldn't be verified, with both lists">

   Files already at the target are never overwritten or deleted. During a restore to a local folder, files are staged in a hidden
   `.hbk-restore-<job id>-…` folder inside the target and moved into place one by one; it is removed when the job ends or is resumed.
   If the app was killed mid-restore, that folder may remain; it is safe to delete.

The pictures show synthetic backups with made-up names; `scripts/guide_screenshots.py` regenerates them.

Encrypted (`.L0CK3D`) copies are detected and reported as unreadable.

## Google Drive: a key of your own (optional, recommended)

Google does not limit how fast you download, it limits how many *requests* an app may make per second. Without a key of your own
the app signs in through the public key of rclone, which every rclone user in the world shares, so Google slows it down. A backup
is tens of thousands of small index files, one request each: that is what turns the one-time preparation into hours. With your own
key the limit is yours alone, and the Google sign-in screen shows your own project name instead of "rclone". Everything works
without a key; it is only slower.

Getting one is free and takes about five minutes (the same steps are in the app, under *Add backup*):

1. Open console.cloud.google.com with the Google account that holds the backup and create a project (any name).
2. *APIs & Services → Library*: enable "Google Drive API".
3. *APIs & Services → OAuth consent screen*: user type *External*, app name "Callido Restore", your e-mail as contact; under
   *Test users* add your own account (the app stays in testing mode, which is fine for one user).
4. *APIs & Services → Credentials → Create credentials → OAuth client ID*, application type *Desktop app*.
5. Copy the Client ID and the Client secret into the app: either when you connect Google Drive, or later under *Add backup →
   a connection I already set up → Downloads slow? Give this Google Drive connection a key of your own*. The connection keeps its
   name, so your backups and everything already downloaded stay; you sign in to Google once more.

The key identifies the app towards Google and gives nobody access to your Drive. It is stored in rclone's configuration on this
computer together with the sign-in token.

## Disk space on this computer
Nothing is ever written to the backup. What the app downloads goes into its own cache folder (`~/.hbk-explorer/cache`):
the backup's chunk table (`Pool/chunk_index`, read in 8 MB slices as a restore needs them; up to the table's full size,
6.8 GB for a 1.5 TB backup, and kept for later restores), the snapshot file lists you load, and the bucket files a restore
reads (removed again once the cache holds more than 20 GB of them). You can delete the cache folder when the app is closed;
it is filled again on demand.
