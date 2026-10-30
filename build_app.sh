#!/bin/bash
# Build "Hyper Backup Desktop.app" and a .dmg.
# Architecture follows RCLONE_ARCH (arm64 or amd64) or the machine's uname.
# Needs: python3, pip, curl, unzip, hdiutil, and internet for rclone.
# Platform-specific packaging lives here; the app itself stays cross-platform.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
rm -rf .buildenv
"$PY" -m venv .buildenv
# shellcheck disable=SC1091
. .buildenv/bin/activate
python -m pip install -q -U pip
python -m pip install -q -r requirements.txt 'pyinstaller>=6.0'

ARCH=${RCLONE_ARCH:-$([ "$(uname -m)" = "arm64" ] && echo arm64 || echo amd64)}
case "$ARCH" in
  arm64) FILE_ARCH=arm64 ;;
  amd64) FILE_ARCH=x86_64 ;;
  *) echo "unsupported RCLONE_ARCH: $ARCH" >&2; exit 1 ;;
esac
if [ -f rclone ] && ! file -b rclone | grep -q "$FILE_ARCH"; then
  rm -f rclone
fi
if [ ! -f rclone ]; then
  curl -fsSL "https://downloads.rclone.org/rclone-current-osx-${ARCH}.zip" -o /tmp/rclone.zip
  rm -rf /tmp/rclone_x
  unzip -qo /tmp/rclone.zip -d /tmp/rclone_x
  cp /tmp/rclone_x/rclone-*/rclone ./rclone
fi
chmod 755 rclone
# Quarantine xattrs survive some copies and make macOS kill the binary.
xattr -c rclone 2>/dev/null || true

APP_NAME="Hyper Backup Desktop"
APP="dist/${APP_NAME}.app"
rm -rf "$APP" build/work "build/${APP_NAME}.spec"
pyinstaller --noconfirm --clean --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier nl.vanharen.hyperbackupdesktop \
  --specpath build \
  --distpath dist \
  --workpath build/work \
  --add-data "static:static" \
  --add-binary "rclone:." \
  --hidden-import hbk_pool \
  --hidden-import hbk_poolmap \
  --hidden-import lz4 \
  --hidden-import lz4.block \
  --collect-submodules lz4 \
  hbk_explorer.py

# PyInstaller chmod's BINARY entries, then cross-links them. Set the bit on
# every real copy in case a later copy dropped it.
find "$APP" -type f -name rclone -exec chmod 755 {} \;

if [ ! -f "$APP/Contents/Frameworks/static/index.html" ] && [ ! -f "$APP/Contents/Resources/static/index.html" ]; then
  echo "static/index.html missing from the app bundle" >&2
  find "$APP" -name index.html -print >&2 || true
  exit 1
fi

RCLONE_BIN="$APP/Contents/Frameworks/rclone"
if [ ! -e "$RCLONE_BIN" ]; then
  RCLONE_BIN=$(find "$APP" -type f -name rclone -print || true)
  RCLONE_BIN=${RCLONE_BIN%%$'\n'*}
fi
if [ -z "${RCLONE_BIN}" ] || [ ! -e "$RCLONE_BIN" ]; then
  echo "bundled rclone binary not found" >&2
  find "$APP" -name rclone -print >&2 || true
  exit 1
fi
# Follow a cross-link so chmod/codesign hit the real Mach-O file.
RCLONE_BIN=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$RCLONE_BIN")
chmod 755 "$RCLONE_BIN"
_rclone_version() {
  _out=$(mktemp)
  if ! "$1" version >"$_out" 2>&1; then
    echo "rclone version failed for $1" >&2
    cat "$_out" >&2
    rm -f "$_out"
    return 1
  fi
  head -n 1 "$_out"
  rm -f "$_out"
}
if ! _rclone_version "$RCLONE_BIN" >/dev/null; then
  echo "processed rclone did not run; restoring the downloaded binary" >&2
  cp rclone "$RCLONE_BIN"
  chmod 755 "$RCLONE_BIN"
  xattr -c "$RCLONE_BIN" 2>/dev/null || true
  codesign --force --sign - "$RCLONE_BIN"
fi
_rclone_version "$RCLONE_BIN"

if ! codesign --verify --deep --strict "$APP" >/dev/null 2>&1; then
  echo "bundle signature did not verify; ad-hoc signing" >&2
  codesign --force --deep --sign - "$APP" || echo "codesign failed; the smoke test will decide if the app runs" >&2
fi

rm -rf dist/dmg
mkdir -p dist/dmg
# ditto keeps bundle symlinks, permissions, and signature metadata intact.
ditto "$APP" "dist/dmg/${APP_NAME}.app"
ln -s /Applications dist/dmg/Applications
rm -f "dist/HyperBackupDesktop-${ARCH}.dmg"
created=
for _ in 1 2 3; do
  if hdiutil create -volname "Hyper Backup Desktop" -srcfolder dist/dmg -ov -format UDZO "dist/HyperBackupDesktop-${ARCH}.dmg"; then
    created=1
    break
  fi
  sleep 2
done
if [ -z "${created}" ]; then
  echo "hdiutil create failed" >&2
  exit 1
fi
echo "built dist/HyperBackupDesktop-${ARCH}.dmg"
