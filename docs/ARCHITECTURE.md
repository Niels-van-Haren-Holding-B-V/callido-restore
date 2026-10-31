# Architecture

Package layout (H16, in progress): `callido_restore/core/` holds the .hbk format (pure parsing and file reconstruction);
`pool/` (pool map v1/v2, index fetch), `restore/` (plan, destinations, no-overwrite writer, jobs), `sources/` (local folders and
rclone remotes, read-only), `server/` (HTTP API) and `app/` (desktop window, entry point) follow, one layer per PR; `tests/`
mirrors the packages. Lower layers never import higher ones. Until a layer has moved, its code still lives in the root modules below.

* `hbk_explorer.py` — HTTP server (stdlib `http.server`), JSON API, static UI, sources, snapshot indexing, jobs, restore.
* `callido_restore/core/pool.py` — pool format: sliced streams, virtual file index, chunk records, buckets, LZ4, `PoolReader` (reconstructs files).
  (`hbk_pool.py` at the root is a shim that returns this module; it goes away with the last H16 step.)
* `hbk_poolmap.py` — offline mapping of chunk ids to buckets (see `docs/FORMAT.md`).
* `static/index.html` — single-page UI, no build step, no external dependencies. Shown in a native window (pywebview, WebKit on macOS)
  by `run_window`; the HTTP server listens on 127.0.0.1 only and the process exits when the window closes (`_exit_app`).
* `~/.hbk-explorer/` — `sources.json`, `index/<source>/<share>__<ver>.sqlite` (derived, queryable snapshot), `index/<source>/pool/`
  (`listing.json`, `runs_set*.bin`, `recsize_set*.json`, `poolmap.json`), `cache/<source>/` (bytes fetched from a remote;
  same relative paths as in the `.hbk`), `jobs/` (persisted restore jobs).

Data flow: a **Source** is a local folder plus optional rclone remotes (first wins) plus extra local roots. Those folders and
the remotes are read-only. `Source.ensure(rel)` returns a file that is already in the app cache or in a source folder, and
downloads a missing file from the remote into `cache/<source>/` only. **Backup** scans version list and shares. Loading a snapshot = fetching
`<ver>.db` and building a derived SQLite with resolved paths and flags. Timeline/search/compare query that SQLite.
Restore: `restore_plan` (rows) → `PoolReader.extract` per file (file_pool or chunks via pool map) → local path or `rclone moveto` to a remote;
buckets are prefetched 4-wide and evicted LRU (≤ `HBK_CACHE_GB`, default 20). Jobs are persisted every 50 files; pause = cancel flag;
resume restarts at `next_index` and also skips files already present with the same size.

API (all GET unless noted): `/api/sources`, `POST /api/sources`, `/api/open`, `/api/snapshot_info`, `/api/index` (job), `/api/query`,
`/api/children`, `/api/histogram`, `/api/file`, `/api/diff`, `/api/extract`, `/api/download`, `/api/pool`, `/api/pool/build` (job),
`/api/restore_plan`, `POST /api/restore` (job), `POST /api/restore/resume`, `/api/job`, `/api/jobs`, `/api/jobs/saved`, `/api/job/cancel`,
`/api/remotes`, `/api/rclone/ls`, `/api/rclone/connect` (job), `/api/about`, `/api/quit`.
