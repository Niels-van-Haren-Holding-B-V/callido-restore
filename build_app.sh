#!/bin/bash
# Build "Hyper Backup Desktop.app" and a .dmg (Apple silicon). Needs: python3, pip, and internet for rclone.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
$PY -m venv .buildenv && . .buildenv/bin/activate
pip install -q -r requirements.txt pyinstaller
if [ ! -f rclone ]; then
  curl -sSL https://downloads.rclone.org/rclone-current-osx-arm64.zip -o /tmp/rclone.zip
  unzip -qo /tmp/rclone.zip -d /tmp/rclone_x && cp /tmp/rclone_x/rclone-*/rclone ./rclone && chmod +x rclone
fi
pyinstaller --noconfirm --windowed --name "Hyper Backup Desktop" --osx-bundle-identifier nl.vanharen.hyperbackupdesktop \
  --add-data "static:static" --add-binary "rclone:." --hidden-import lz4.block hbk_explorer.py
rm -rf dist/dmg && mkdir -p dist/dmg && cp -R "dist/Hyper Backup Desktop.app" dist/dmg/ && ln -s /Applications dist/dmg/Applications
hdiutil create -volname "Hyper Backup Desktop" -srcfolder dist/dmg -ov -format UDZO "dist/HyperBackupDesktop-arm64.dmg"
echo "built dist/HyperBackupDesktop-arm64.dmg"
