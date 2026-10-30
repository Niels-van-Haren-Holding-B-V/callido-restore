**Callido Restore** restores files from a Synology Hyper Backup (`.hbk`) without Hyper Backup or a working NAS. It reads the backup's own metadata, shows every backup run on a timeline, and restores the folders you pick to a NAS share or a local folder. Files already at the destination are never overwritten or deleted, and the backup source is only read.

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
