# Callido Restore — notes for Claude Code

Product name: `APP_NAME` in `callido_restore/about.py` is the single source (build and smoke scripts read it). Never use "Hyper Backup" as
part of the product name or logo; descriptive use ("restore a Synology Hyper Backup") is fine. About/README carry the Synology trademark notice.

Purpose: explore a Synology Hyper Backup (`.hbk`) on a timeline and restore files from it without Hyper Backup, on macOS.
Read `docs/HANDOVER.md` first (current state and open items), then `docs/FORMAT.md` (reverse-engineered format) and `docs/ARCHITECTURE.md`.

Conventions
* Code lives in the `callido_restore` package, one folder per layer: `core` (.hbk format) → `pool` (pool map) → `sources` (read-only
  backup sources) → `restore` (restore engine, jobs) → `server` (HTTP API) → `app` (window, entry point); `paths.py` and `about.py` at
  the bottom. A layer never imports a higher one; `tests/` mirrors the layers. See `docs/ARCHITECTURE.md`.
* Python 3.12+, standard library only, except `lz4` and `pywebview` (see `requirements.txt`). No web framework; UI is `static/index.html`
  (vanilla JS, no build) shown in a native window by pywebview; `--browser` uses the system browser, `--no-window` runs the server only.
* Run locally: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m callido_restore`.
* Never hardcode user paths or remotes in code; everything user-specific lives in `~/.hbk-explorer/`. The repo is public: no real
  share/folder/host names, IPs or local paths anywhere, not even in tests, docs or PR text (neutral examples: shares "Documents",
  "Photos", "homes", host "nas", `backup_1.hbk`, version 1234, IPs 192.0.2.x); `scripts/check_private.sh` enforces a denylist in CI.
* Long operations are jobs (`start_job`) with `progress`/`message`; the UI polls `/api/job`. Restore jobs are persisted in `~/.hbk-explorer/jobs/`.
* All rclone calls go through the `RCLONE` constant (bundled binary in the app, else PATH).
* Format assumptions are documented in the `callido_restore/core/pool.py` docstring and `docs/FORMAT.md`; if something in a backup does not match, extend the
  detection rather than special-casing a path.
* Packaging: `./build_app.sh` (PyInstaller + hdiutil), CI in `.github/workflows/build.yml` (macos-15 arm64 + macos-15-intel).
