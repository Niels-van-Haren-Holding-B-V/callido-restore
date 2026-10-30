# User guide

1. Start the app (or `python3 hbk_explorer.py`). It opens http://127.0.0.1:8765.
2. **+ Add backup** → choose where the `.hbk` lives: an rclone remote you already have, Google Drive (sign in; optionally use your own
   OAuth client id to get your own API quota), a NAS over SMB, or a local folder → browse to the `.hbk` folder → name it.
3. The overview shows the task, the shares and all backup runs. Click a run in the chart (or "Go to version"). The latest run is
   selected by default. Click the share chips to load snapshots (downloads a few MB each).
4. Tabs: **Changes** (what that run added/changed), **File timeline**, **Browse folders**, **Search & flags** (suspicious files),
   **Compare versions**, **Restore wizard**.
5. **Restore wizard**: pick the run → pick everything or folders (+ buttons) → build the pool map once (downloads ~200 KB per bucket;
   progress bar; Stop keeps index files already downloaded, and running it again continues) → choose the target: back into the
   original NAS shares, one folder on a NAS share, or a local path → Preview → Start.
   The bar at the top shows running jobs. Pause/Resume from the wizard; after a crash or reboot the job is listed for resuming.
Encrypted (`.L0CK3D`) copies are detected and reported as unreadable.
