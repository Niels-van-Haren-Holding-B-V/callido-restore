**Callido Restore** restores files from a Synology Hyper Backup (`.hbk`) without Hyper Backup or a working NAS. It reads the backup's own metadata, shows every backup run on a timeline, and restores the folders you pick to a NAS share or a local folder. Files already at the destination are never overwritten or deleted, and the backup source is only read.

## What's new in 0.4.0

- **Windows 10 and 11.** Callido Restore now comes as a Windows installer, `CallidoRestore-x64.msi`, next to the two Mac disk images. It installs to Program Files with a Start menu and desktop shortcut, opens in its own window (Microsoft Edge WebView2, built into Windows 11 and current Windows 10), and can be removed under Settings → Apps. Your settings and downloaded backup data are kept on uninstall (#108).
- **The installer is not code-signed yet.** Windows SmartScreen shows "Windows protected your PC": click **More info → Run anyway**. Check the download against `CallidoRestore-x64.msi.sha256` first.
- Windows paths (`C:\Restore`, `Z:\`, `\\nas\share`) work as restore destinations and backup folders; rclone runs without console windows, and Stop ends the preparation on Windows too (#112).

## What's new in 0.3.9

- **The preparation starts by itself.** It is not optional, so the app runs it in the background as soon as a backup is open and a share's file list is loaded, and resumes it after a restart. Pause keeps it paused until you resume; the header shows what it is doing. A preparation made by an older version of the app is redone automatically.

## What's new in 0.3.8

- **The pool map places far more of the backup, and only what it can check.** The map builder now knows that a file backed up a second time (another share, a move, a later run) stores no data of its own, that bucket numbers are reused over time, and that every chunk has its own length, so a file's place in the pool can be checked against its size. On a real backup it places 74.5 % of all bytes and 1,279 of 1,593 files over 100 MB (it was 57.6 % and 696), and 454,000 files are verified by their size. Where the data is too alike to decide, it leaves the file out rather than guess (#98, #99, #100).
- **Restore results say what was checked.** Every restored file is counted as checked (its place in the backup matched the file size and every block's checksum was verified on the way out; this is not a whole-file checksum), from an identical copy, or couldn't be verified. The unverified files are listed with a plain reason, and a restore with such files is marked "needs checking" in the earlier-restores list (#101, #102).
- The preparation's memory use is capped at every step of the mapping, and the mapping phase itself needs about 2 GB.

## What's new in 0.3.7

- **The pool-map scan reads the index files from where they are.** For a backup kept on Google Drive it looked for them under a local folder that does not exist; it now resolves every file through the app cache. Without this the mapper could not run on a Drive-backed backup at all.
- The preparation builds the record tags the mapper needs from the loaded snapshots (hbk-1, #96), with smaller writer tables (#95), and refuses to map when no snapshot has been loaded yet instead of producing an anchor-less map.
- Measured on a real backup: the whole mapping takes about 17 minutes with four scan workers; the map it produces is partial (see the task list), so some files, mostly large ones, cannot be restored yet.

## What's new in 0.3.6

- **Stop ends the pool-map scan at once**, workers included: the mapper runs in its own process group and Stop and Quit kill the group. Helpers left behind by a crashed app are ended at the next start.
- **The parallel scan shows its position** ("scanned 12,400 of 36,132 buckets") every two seconds; it used to sit at 70 % until a whole part finished.
- No more "less than a minute left" when nothing has moved for ten seconds; the estimate waits for real movement.
- The placement step reports too: "resolving copies: pass 1/3, 120,000 of 400,000 records" and "placing writer 2: 8,000 of 30,000 anchors", every two seconds.

## What's new in 0.3.5

- **The pool-map scan runs with four worker processes in the packaged app too.** It used to fall back to a single process there, because the workers were started through a Python interpreter the app does not carry; the app binary now serves as the worker.

## What's new in 0.3.4

- **The preparation now builds the anchor-first pool map** (the one the project's task list designates; the old sequential search it still ran sat at bucket 3000 of 36132 for a quarter of an hour on a real backup and could not be stopped). Progress shows the scan position.
- **The pool map is computed in a separate process.** The app and its buttons stay responsive while the search runs at full CPU, and Stop ends it at once (it could not interrupt the computation before).
- The pool-map phase reports its position every two seconds, also while it backtracks (it used to go silent for minutes between two reports a thousand buckets apart).
- Once the preparation has every index file on disk and is computing the pool map, the bar says "5.7 GB downloaded" and the time left follows the mapping progress, instead of showing 0 B/s as if a download had stalled.

## What's new in 0.3.3

- **Restores download 8 bucket files at a time instead of 4** (43 MB/s instead of 29 on a measured line), and the index-set slices of the preparation 8 instead of 4.
- **One place for every transfer setting.** How many files move at once per kind of work (bucket indexes, index-set slices, restore) and the retries and Google Drive pacing shared by all of them now live in one module, with the measurements that chose them.

## What's new in 0.3.2

- **The bucket-index download runs 64 transfers in parallel instead of 16.** Index files are small and Google's per-file start latency is around a second, so parallelism is what sets the speed: measured on a real backup with an own key, three times the throughput; beyond 64 Google's rate limit caps it.
- **You can see that your Google key is in use.** Once a Drive connection has a key of its own, the overview and the preparation step say so ("Your own Google key is in use") instead of still offering the link; the key panel says the same and offers to replace it.
- **The key is checked before the sign-in.** A client id that does not look like one is refused at once, and the pair is checked with Google before the browser opens, with a clear message when Google does not know it.
- After the sign-in the page updates itself and the confirmation stays visible longer.

## What's new in 0.3.1

- **The Google key is one click away.** The overview and the preparation step show "Downloads slow? Give your Google Drive connection a key of your own". It opens straight on your existing connection with the key panel unfolded, the button reads "Sign in again with this key", and the dialog closes when the sign-in is done. Nothing is added or re-downloaded: the connection keeps its name, and all index files and listings stay.

## What's new in 0.3.0

- **Your own Google key, explained and optional.** Google limits requests per second per app, not bandwidth; without a key of your own the app signs in through rclone's public key that every rclone user shares, and a backup is tens of thousands of small index files. The sign-in dialog now explains this, takes your client id and secret up front with a five-step how-to, and a key can be added later to an existing Google Drive connection ("Downloads slow? Give this connection a key of your own"). Everything still works without one.
- **The page after the Google consent is the app's own**: "Signed in, you can close this tab and go back to the app", instead of rclone's plain white page. The sign-in asks for read-only Drive access only.
- **Safer connections.** A name that already belongs to a connection of another type is refused, so an SMB restore target can never be replaced by a Drive connection or the other way round; an existing Drive connection is updated in place, keeping options set outside the app.
- **Crash leftovers are cleaned up at start.** An app that died together with its disk image left its rclone helper and a download running for hours at full CPU on your Drive quota; the app now stops such orphans of its own (force-kill if they ignore a normal terminate) and never touches other rclone processes.
- **True speed during the bucket-index download.** The byte counter follows rclone's live transfer instead of moving only when a batch of files is complete, so the speed no longer reads a few KB/s while megabytes per second are flowing.

## What's new in 0.2.6

- Downloads through the app's rclone helper are throttled the same way the old one-process downloads were (a transactions-per-second cap, Google Drive pacing, more low-level retries). Four index-set slices at a time without that cap made Google rate-limit the preparation.
- Bytes done and time left during a download no longer run ahead of the real transfer (in-flight bytes were counted twice).
- README: screenshots of the app, taken from a small demo backup.

## What's new in 0.2.5

- Every job bar with a known size shows bytes, speed and time left, the restore included; the preparation reports the download of each index set ("downloading index set 2/4 · 123 of 560 MB").
- A progress bar whose value has not moved for a few seconds shows a shimmer and "still working · last update N s ago", so a slow phase still reads as working.
- Downloads from Google Drive (and every other rclone remote) go through the app's internal rclone helper instead of one process per file: the index sets of the preparation are fetched four at a time, with live bytes and speed, a "rate-limited, retrying" note when Google slows the transfer down, and Stop takes effect at once.
- Job bars have fixed slots (phase · done of total · speed · time left · elapsed) that do not jump around; the speed is a steady 30-second average.

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

| Computer | File |
| --- | --- |
| Mac with Apple silicon (M1 and later) | `CallidoRestore-arm64.dmg` |
| Mac with Intel | `CallidoRestore-amd64.dmg` |
| Windows 10 (21H2 and later) or 11, x64; Arm64 Windows runs it through emulation | `CallidoRestore-x64.msi` |

`SHA256SUMS` lists the checksums of the two Mac images (`shasum -a 256 -c SHA256SUMS` in the download folder verifies them);
`CallidoRestore-x64.msi.sha256` that of the Windows installer (`Get-FileHash CallidoRestore-x64.msi` in PowerShell shows it).

## Install and first start on a Mac

1. Open the `.dmg` and drag **Callido Restore** to Applications.
2. Open the app once. The app is not notarized, so macOS blocks this first start.
3. On macOS 15 and later: click **Done** in the warning, open **System Settings → Privacy & Security**, scroll down and click **Open Anyway** next to Callido Restore, then open the app again and confirm.
   On older macOS: right-click (Control-click) the app in Applications and choose **Open**, then **Open** again.

Only the first start needs this. Settings, caches and `app.log` live in `~/.hbk-explorer/`. rclone is bundled; its licence and the other third-party notices are inside the app under `Contents/Resources/Licenses/` and on the disk image.

## Install and first start on Windows

1. Save the `.msi` on the PC's own disk first: Windows Installer cannot open it from a network or virtual-machine shared folder ("This installation package could not be opened").
2. Run `CallidoRestore-x64.msi`. The installer is not signed yet, so Windows SmartScreen shows "Windows protected your PC": click **More info**, then **Run anyway**.
3. Follow the installer; it installs into `C:\Program Files\Callido\Callido Restore`, adds a Start-menu and a desktop shortcut, and can open the app on its last page. The app opens in its own window (Microsoft Edge WebView2, built into Windows 11 and current Windows 10).

Installing a newer version replaces the old one; uninstalling (Settings → Apps) never touches your settings, caches and `app.log` in `%USERPROFILE%\.hbk-explorer`. rclone is bundled.

## Known limitations

* Not signed: on a Mac not notarized with a Developer ID, on Windows no code-signing certificate; hence the first-start steps above.
* Google Drive is the first supported source (also any rclone remote, an SMB share or a local folder). Use your own Google OAuth client id and secret in the Drive step; rclone's shared client id is being retired.
* Backup tasks with client-side encryption are not supported yet.
* A full pool map is built once per backup before the first restore; it downloads a few hundred KB per bucket index.

## Notice

Not affiliated with or endorsed by Synology. Hyper Backup is a trademark of Synology Inc. The logo is original artwork made for this project. MIT licensed.
