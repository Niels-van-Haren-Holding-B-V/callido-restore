#!/bin/bash
# Mount the built disk image and check the app serves the UI and bundled rclone.
# macOS only (hdiutil). Invoked by .github/workflows/build.yml.
set -euo pipefail
cd "$(dirname "$0")/.."
MNT="/tmp/hbk-dmg-$$"
LOG="${TMPDIR:-/tmp}/hbk-smoke.log"
APP_NAME="Hyper Backup Desktop"
PORT=8765
PID=
DMG=$(echo dist/HyperBackupDesktop-*.dmg)
if [ ! -f "$DMG" ]; then
  echo "missing dist/HyperBackupDesktop-*.dmg (run ./build_app.sh first)" >&2
  ls -l dist >&2 || true
  exit 1
fi
rm -rf "$MNT"
mkdir -p "$MNT"
cleanup() {
  if [ -n "${PID}" ]; then
    kill "$PID" >/dev/null 2>&1 || true
  fi
  hdiutil detach "$MNT" >/dev/null 2>&1 || hdiutil detach -force "$MNT" >/dev/null 2>&1 || true
  rm -rf "$MNT"
}
trap cleanup EXIT

hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$DMG"
BIN="$MNT/$APP_NAME.app/Contents/MacOS/$APP_NAME"
if [ ! -x "$BIN" ]; then
  echo "app binary missing or not executable: $BIN" >&2
  find "$MNT" -maxdepth 4 -print >&2
  exit 1
fi

rclone_ok=
while IFS= read -r f; do
  [ -n "$f" ] || continue
  real=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$f")
  if [ -x "$real" ]; then
    rclone_ok=1
    echo "bundle rclone: $real"
    "$real" version >"$LOG.rclone" 2>&1 || { echo "rclone version failed" >&2; cat "$LOG.rclone" >&2; exit 1; }
    head -n 1 "$LOG.rclone"
    break
  fi
done <<EOF
$(find "$MNT/$APP_NAME.app" -name rclone)
EOF
if [ -z "${rclone_ok}" ]; then
  echo "no executable rclone in the disk image" >&2
  find "$MNT/$APP_NAME.app" -name rclone -exec ls -l {} \; >&2
  exit 1
fi

: >"$LOG"
"$BIN" --no-browser --port "$PORT" >"$LOG" 2>&1 &
PID=$!

ready=
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40; do
  if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/"; then ready=1; break; fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "app exited before listening" >&2
    cat "$LOG" >&2
    exit 1
  fi
  sleep 1
done
if [ -z "${ready}" ]; then
  echo "timed out waiting for http://127.0.0.1:${PORT}/" >&2
  cat "$LOG" >&2
  exit 1
fi

if ! page=$(curl -sf "http://127.0.0.1:${PORT}/"); then
  echo "homepage request failed" >&2
  cat "$LOG" >&2
  exit 1
fi
case "$page" in
  *"Hyper Backup Desktop"*) ;;
  *) echo "homepage did not contain the app UI" >&2; printf '%s\n' "$page" >&2; exit 1 ;;
esac

if ! python3 - "$PORT" <<'PY'
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/about", timeout=15) as r:
    data = json.load(r)
print(data)
if data.get("version") != "0.1.0":
    raise SystemExit(f"unexpected app version: {data!r}")
rclone = data.get("rclone") or ""
if not rclone.lower().startswith("rclone v"):
    raise SystemExit(f"about did not report a bundled rclone version: {data!r}")
path = (data.get("rclone_path") or "").replace("\\", "/")
if ".app/" not in path or not (path.endswith("/rclone") or path.endswith("/rclone.exe")):
    raise SystemExit(f"about rclone_path is not the bundled binary: {data!r}")
PY
then
  echo "about check failed; app log:" >&2
  cat "$LOG" >&2
  exit 1
fi

if ! quit=$(curl -sf "http://127.0.0.1:${PORT}/api/quit"); then
  echo "quit request failed" >&2
  cat "$LOG" >&2
  exit 1
fi
case "$quit" in
  *'"ok"'*) ;;
  *) echo "unexpected quit response: $quit" >&2; exit 1 ;;
esac
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if ! kill -0 "$PID" 2>/dev/null; then
    PID=
    echo "smoke test passed"
    exit 0
  fi
  sleep 0.5
done
echo "app did not exit after /api/quit" >&2
cat "$LOG" >&2
exit 1
