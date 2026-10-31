# Architecture

Package `callido_restore/`, one folder per layer; a layer never imports a higher one, `tests/` mirrors the layers.

* `paths.py` — where things live: `~/.hbk-explorer/` (`sources.json`, `index/`, `cache/`, `jobs/`), bundled resources (`static/`,
  rclone) in a checkout or a PyInstaller bundle, the `RCLONE` binary. Code reads the folders through the module (`paths.INDEX_DIR`),
  so tests point them at a temp folder. `about.py` — `APP_NAME`, tagline, notice, `VERSION` (build_app.sh reads `APP_NAME` from here).
* `core/` — the .hbk format, pure parsing: `pool.py` (sliced streams, virtual file index, chunk records, buckets, LZ4, `PoolReader`
  that reconstructs files), `hbkmeta.py` (file revisions, share-root id, ransomware markers, flag extension sets).
* `pool/` — the pool map: `poolmap.py` (v1, sequential, see `docs/FORMAT.md`), `poolmap2.py` (v2, tag-based), `anchor.py` (anchor
  scan with subprocess workers, `python -m callido_restore.pool.anchor --worker`; the frozen app uses one in-process worker, an
  in-app anchor scan will need a `--worker` dispatch in the entry point), `indexfetch.py` (resumable bucket-index download).
* `sources/transfer.py` — the one place for how rclone moves data: shared resilience and Drive pacing (`COMMON`, `DAEMON`) and a
  profile per kind of work (`index_files` 64 transfers, `index_slices` 8 workers, `restore` 8 workers + 16 prefetched); the measurements
  behind the numbers are in its docstring. The daemon start, the copyto fallback, `pool/indexfetch.py`, `core/pool.py` and
  `restore/backup.py` read from it; no transfer number lives anywhere else.
* `sources/source.py` — `Source`: a local .hbk folder plus optional rclone remotes (first wins) and extra read-only roots; `ensure`
  downloads a missing file into `cache/<source>/` only, nothing in a source is ever modified; `contains_dest` refuses restore
  destinations inside the backup; the registry (`SOURCES`, `load_sources`, `add_source`).
* `restore/` — `destination.py` (never overwrite or delete: `place_file`, per-folder targets `file_target`/`restore_roots`,
  `restore_target_error`, `list_destination`), `jobs.py` (`start_job`, `JOBS`), `backup.py` (`Backup`: discovery of task, shares
  and versions, derived SQLite snapshot index, queries for the explorer tabs, pool access, `restore_plan`/`restore` with prefetch,
  LRU cache, retries, persisted and resumable jobs).
* `server/api.py` — the JSON API and static UI on 127.0.0.1 (`H`), thin handlers over `sources` and `restore`; `backup_for` registry.
* `app/main.py` — command line, logging to `app.log` when frozen, the pywebview window (`run_window`), `main()`;
  `callido_restore/__main__.py` is the entry point (`python -m callido_restore`) and the PyInstaller script.
* `static/index.html` — single-page UI, no build step, no external dependencies.

Data flow: `Source.ensure(rel)` returns a file from the app cache or a source folder, downloading from the remote into the cache only.
`Backup` scans the version list and shares; loading a snapshot fetches `<ver>.db` and builds a derived SQLite with resolved paths and
flags; timeline/search/compare query that SQLite. Restore: `restore_plan` (rows) → `PoolReader.extract` per file (file_pool or chunks
via the pool map) into a per-root staging folder → `place_file` (local) or `rclone moveto --ignore-existing` (remote); buckets are
prefetched 4-wide and evicted LRU (≤ `HBK_CACHE_GB`, default 20). Jobs persist every 50 files; pause = cancel flag; resume restarts
at `next_index` and skips files already at the destination.

API (all GET unless noted): `/api/sources`, `POST /api/sources`, `/api/open`, `/api/snapshot_info`, `/api/index` (job), `/api/query`,
`/api/children`, `/api/histogram`, `/api/file`, `/api/diff`, `/api/extract`, `/api/download`, `/api/pool`, `/api/pool/build` (job),
`/api/restore_plan`, `/api/restore_count`, `/api/dest/check`, `POST /api/restore` (job), `POST /api/restore/resume`, `/api/job`,
`/api/jobs`, `/api/jobs/saved`, `/api/job/cancel`, `/api/remotes`, `/api/rclone/ls`, `POST /api/rclone/connect` (job), `/api/about`,
`/api/quit`.
