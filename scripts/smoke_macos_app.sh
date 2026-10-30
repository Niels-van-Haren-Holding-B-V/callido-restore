#!/bin/bash
# Mount each disk image in dist/ and check the app, bundled rclone, and license notice.
# macOS only (hdiutil).
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="Hyper Backup Desktop"
PORT=${HBK_SMOKE_PORT:-8765}
MNT=
LOG=
PID=

die() {
  echo "$*" >&2
  if [ -n "$LOG" ] && [ -f "$LOG" ]; then
    cat "$LOG" >&2
  fi
  exit 1
}

release_mount() {
  if [ -n "$PID" ]; then
    kill "$PID" >/dev/null 2>&1 || true
    PID=
  fi
  if [ -n "$MNT" ]; then
    hdiutil detach "$MNT" >/dev/null 2>&1 || hdiutil detach -force "$MNT" >/dev/null 2>&1 || true
    rm -rf "$MNT"
    MNT=
  fi
  if [ -n "$LOG" ]; then
    rm -f "$LOG"
    LOG=
  fi
}
trap release_mount EXIT

# First match under the mounted app. macOS find has no -quit.
find_named() {
  local name=$1 path
  while IFS= read -r path; do
    if [ -n "$path" ]; then
      printf '%s\n' "$path"
      return 0
    fi
  done <<EOF
$(find "$MNT/$APP_NAME.app" -name "$name")
EOF
  return 1
}

executable_rclone() {
  local path real
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    real=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$path")
    if [ -x "$real" ]; then
      printf '%s\n' "$real"
      return 0
    fi
  done <<EOF
$(find "$MNT/$APP_NAME.app" -name rclone)
EOF
  return 1
}

wait_for_http() {
  local _
  for _ in {1..40}; do
    if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/"; then
      return 0
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
      die "app exited before listening"
    fi
    sleep 1
  done
  die "timed out waiting for http://127.0.0.1:${PORT}/"
}

wait_for_exit() {
  local _
  for _ in {1..20}; do
    if ! kill -0 "$PID" 2>/dev/null; then
      PID=
      return 0
    fi
    sleep 0.5
  done
  die "app did not exit after /api/quit"
}

check_about() {
  python3 - "$PORT" <<'PY'
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/about", timeout=15) as response:
    data = json.load(response)
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
}

smoke_one() {
  local dmg=$1 bin rclone copying notice page quit
  echo "smoke: $dmg"
  MNT=$(mktemp -d "${TMPDIR:-/tmp}/hbk-dmg.XXXXXX")
  LOG=$(mktemp "${TMPDIR:-/tmp}/hbk-smoke.XXXXXX")
  hdiutil attach -nobrowse -readonly -mountpoint "$MNT" "$dmg"
  bin="$MNT/$APP_NAME.app/Contents/MacOS/$APP_NAME"
  [ -x "$bin" ] || die "app binary missing or not executable: $bin"

  rclone=$(executable_rclone) || die "no executable rclone in $dmg"
  echo "bundle rclone: $rclone"
  "$rclone" version >"$LOG" 2>&1 || die "rclone version failed"
  head -n 1 "$LOG"

  copying=$(find_named rclone-COPYING) || die "rclone-COPYING missing from the app"
  notice=$(find_named THIRD-PARTY-NOTICES.txt) || die "THIRD-PARTY-NOTICES.txt missing from the app"
  grep -q "Permission is hereby granted" "$copying" || die "rclone-COPYING is not the MIT license"
  grep -q "License: MIT" "$notice" || die "third-party notice does not name the MIT license"

  : >"$LOG"
  "$bin" --no-browser --port "$PORT" >"$LOG" 2>&1 &
  PID=$!
  wait_for_http

  page=$(curl -sf "http://127.0.0.1:${PORT}/") || die "homepage request failed"
  case "$page" in
    *"Hyper Backup Desktop"*) ;;
    *) die "homepage did not contain the app UI" ;;
  esac
  check_about || die "about check failed"

  quit=$(curl -sf "http://127.0.0.1:${PORT}/api/quit") || die "quit request failed"
  case "$quit" in
    *'"ok"'*) ;;
    *) die "unexpected quit response: $quit" ;;
  esac
  wait_for_exit
  release_mount
  echo "smoke test passed: $dmg"
}

dmgs=(dist/HyperBackupDesktop-*.dmg)
if [ ${#dmgs[@]} -eq 0 ] || [ ! -f "${dmgs[0]}" ]; then
  die "missing dist/HyperBackupDesktop-*.dmg (run ./build_app.sh first)"
fi
for dmg in "${dmgs[@]}"; do
  smoke_one "$dmg"
done
echo "smoke test passed (${#dmgs[@]} disk image(s))"
