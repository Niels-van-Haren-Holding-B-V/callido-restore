**Callido Restore** restores files from a Synology Hyper Backup (`.hbk`) without Hyper Backup or a working NAS. It reads the backup's own metadata, shows every backup run on a timeline, and restores the folders you pick to a NAS share or a local folder. Files already at the destination are never overwritten or deleted, and the backup source is only read.

## What's new in 0.2.5

- Every job bar with a known size shows bytes, speed and time left, the restore included; the preparation reports the download of each index set ("downloading index set 2/4 · 123 of 560 MB").
- A progress bar whose value has not moved for a few seconds shows a shimmer and "still working · last update N s ago", so a slow phase still reads as working.

## What's new in 0.2.4

- Stop takes effect within a second during every phase of the preparation, including the chunk-record parsing and the index-set downloads; what was already parsed is simply redone next time.
- The preparation's progress bar moves during the chunk-record parsing (per set and by bytes parsed) instead of sitting at 10 %.
- Running two copies of the app, or a development server next to it, no longer stops the other one's internal rclone helper.

## What's new in 0.2.3

- The restore selection and the last chosen run survive quitting the app (the window no longer runs in private mode).
- Opening a backup you already opened is instant: the backup's folder listings are kept on disk and refreshed after an hour or with the ↻ button, instead of being read from Google Drive on every start.
- After the preparation's folder listing is done but nothing is downloaded yet, step 3 says so instead of showing 0 %.
- A job you start (preparation, restore, loading the file lists) shows a moving progress bar immediately, with its phase and the seconds elapsed, until real progress figures arrive; the button is disabled from the click on, so a slow first answer no longer looks like a dead click.

## What's new in 0.2.2

Progress you can see, from the first-run feedback:

- Restore step 3 ("Prepare the backup for reading") shows one progress bar while the preparation runs: the phase, the percentage, how much is downloaded ("1.2 of 6.9 GB"), the download speed, the time left and a Stop button. "Prepare now" reads "Preparing…" and is disabled until the job is done, failed or stopped.
- Buttons that start a job (Start restore, Continue a saved restore, Load all shares) are disabled while such a job is already running and come back when it ends, also after a failure.
- "Count files and size" explains that counting needs no preparation; only the restore itself does.

## What's new in 0.2.1

Two fixes on top of 0.2.0, found in the first hours of use:

- If the app's internal rclone helper process dies or is killed, the app no longer shows "Could not open: connection refused"; it falls back to running rclone directly and restarts the helper on the next action.
- On a NAS target, files that are already there are again recognised before the restore starts (0.2.0 still never overwrote them, but counted and re-processed them).

## What's new in 0.2.0

A bugfix release from the first real test run of the app.

- **Faster everywhere.** The app keeps one rclone connection open instead of starting a new one for every folder listing, which makes browsing Google Drive and NAS shares several times faster, and reuses listings for 30 seconds.
- **Google Drive sign-in:** the app tells you a browser window opens and what to do when it shows "Success! All done"; the new connection is selected afterwards, and the wizard no longer gets stuck if you were in the browser for a while.
- **Adding a backup:** the backups found in a folder are listed with a one-click "Use …" button; Next picks the detected backup instead of asking to "use anyway"; a wrong folder gets a clear explanation.
- **Every wait shows what is happening:** a small activity indicator in the header ("Listing NAS shares…", "Loading from Google Drive…", "Counting files…"), opening a backup shows its progress step by step and survives switching to another window.
- **Restore selection:** a "Restore selection" button is always visible, with the number of items and the size, a list to remove items or clear all, and a way to continue to the restore. Adding or removing a folder shows a confirmation with "View selection" and "Undo"; the "+" buttons show whether a folder is already in the restore and remove it on a second click.
- **Restore step 3** says "Not started · N GB of index files to download" instead of "0% · 0 of 0 blocks", and shows how much is already downloaded.
- The "@AppConfig" entry is labelled "Application settings (DSM, not restorable here)".
- Errors appear as notifications instead of blocking pop-ups.

## Download

| Mac | File |
| --- | --- |
| Apple silicon (M1 and later) | `CallidoRestore-arm64.dmg` |
| Intel | `CallidoRestore-amd64.dmg` |

`SHA256SUMS` lists the checksum of both images; `shasum -a 256 -c SHA256SUMS` in the download folder verifies them.

## Install and first start

1. Open the `.dmg` and drag **Callido Restore** to Applications.
2. Open the app once. The app is not notarized, so macOS blocks this first start.
3. On macOS 15 and later: click **Done** in the warning, open **System Settings → Privacy & Security**, scroll down and click **Open Anyway** next to Callido Restore, then open the app again and confirm.
   On older macOS: right-click (Control-click) the app in Applications and choose **Open**, then **Open** again.

Only the first start needs this. Settings, caches and `app.log` live in `~/.hbk-explorer/`. rclone is bundled; its licence and the other third-party notices are inside the app under `Contents/Resources/Licenses/` and on the disk image.

## Known limitations

* Not signed with a Developer ID and not notarized, hence the steps above.
* Google Drive is the first supported source (also any rclone remote, an SMB share or a local folder). Use your own Google OAuth client id and secret in the Drive step; rclone's shared client id is being retired.
* Backup tasks with client-side encryption are not supported yet.
* A full pool map is built once per backup before the first restore; it downloads a few hundred KB per bucket index.

## Notice

Not affiliated with or endorsed by Synology. Hyper Backup is a trademark of Synology Inc. The logo is original artwork made for this project. MIT licensed.
