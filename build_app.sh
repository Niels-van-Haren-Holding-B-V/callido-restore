#!/bin/bash
# Build "<APP_NAME>.app" (APP_NAME from hbk_explorer.py, currently "Callido Restore") and a .dmg.
# Arch comes from RCLONE_ARCH (arm64 or amd64) or from uname.
# Needs python3, pip, curl, unzip, gpg, and hdiutil. Packaging only; the app stays cross-platform.
set -euo pipefail
cd "$(dirname "$0")"

RCLONE_VERSION=v1.75.2
RCLONE_SIGNING_FPR=FBF737ECE9F8AB18604BD2AC93935E02FF3B54FA
APP_NAME=$(sed -n 's/^APP_NAME = "\([^"]*\)".*/\1/p' hbk_explorer.py)
[ -n "$APP_NAME" ] || { echo "APP_NAME not found in hbk_explorer.py" >&2; exit 1; }
DMG_BASE=$(printf '%s' "$APP_NAME" | tr -d ' ')
ROOT=$(pwd)
ARCH=
APP=
DL=
GPG_HOME=

die() { echo "$*" >&2; exit 1; }

cleanup() {
  if [ -n "$DL" ]; then rm -rf "$DL"; fi
  if [ -n "$GPG_HOME" ]; then rm -rf "$GPG_HOME"; fi
}
trap cleanup EXIT

choose_arch() {
  if [ -n "${RCLONE_ARCH:-}" ]; then
    ARCH=$RCLONE_ARCH
  elif [ "$(uname -m)" = "arm64" ]; then
    ARCH=arm64
  else
    ARCH=amd64
  fi
  case "$ARCH" in
    arm64|amd64) ;;
    *) die "unsupported RCLONE_ARCH: $ARCH" ;;
  esac
}

rclone_version_line() {
  local out line
  out=$(mktemp "${TMPDIR:-/tmp}/hbk-rclone-ver.XXXXXX")
  if ! "$1" version >"$out" 2>&1; then
    cat "$out" >&2
    rm -f "$out"
    return 1
  fi
  line=$(head -n 1 "$out")
  rm -f "$out"
  printf '%s\n' "$line"
}

expect_rclone_version() {
  local line
  line=$(rclone_version_line "$1") || die "rclone failed to run: $1"
  case "$line" in
    "rclone ${RCLONE_VERSION}"*) printf '%s\n' "$line" ;;
    *) die "rclone at $1 is '$line', expected ${RCLONE_VERSION}" ;;
  esac
}

# SHA256SUMS is a clearsigned file. Verify the signature before trusting the body.
# Do not pipe gpg --decrypt into the checksum: a bad signature still prints the body.
verify_signed_sums() {
  local fpr
  gpg --batch --homedir "$GPG_HOME" --import "$ROOT/packaging/rclone-release-signing-key.asc" >/dev/null
  fpr=$(gpg --batch --homedir "$GPG_HOME" --with-colons --fingerprint "$RCLONE_SIGNING_FPR" | awk -F: '$1=="fpr" {print $10; exit}')
  [ "$fpr" = "$RCLONE_SIGNING_FPR" ] || die "signing key fingerprint is ${fpr:-missing}"
  if ! gpg --batch --status-fd 1 --homedir "$GPG_HOME" --verify "$DL/SHA256SUMS" >"$DL/status.txt" 2>"$DL/verify.txt"; then
    cat "$DL/verify.txt" >&2
    die "could not verify rclone SHA256SUMS"
  fi
  if ! grep -q "VALIDSIG ${RCLONE_SIGNING_FPR}" "$DL/status.txt"; then
    cat "$DL/verify.txt" >&2
    die "SHA256SUMS was not signed by ${RCLONE_SIGNING_FPR}"
  fi
  gpg --batch --yes --homedir "$GPG_HOME" --output "$DL/SHA256SUMS.clear" --decrypt "$DL/SHA256SUMS" >/dev/null
}

check_sha256() {
  python3 - "$DL/SHA256SUMS.clear" "$DL" "$@" <<'PY'
import hashlib, pathlib, sys
sums = pathlib.Path(sys.argv[1]).read_text()
root = pathlib.Path(sys.argv[2])
want = {}
for line in sums.splitlines():
    parts = line.split()
    if len(parts) == 2 and len(parts[0]) == 64:
        want[parts[1]] = parts[0].lower()
for name in sys.argv[3:]:
    digest = want.get(name)
    if not digest:
        raise SystemExit(f"no signed sha256 for {name}")
    got = hashlib.sha256((root / name).read_bytes()).hexdigest()
    if got != digest:
        raise SystemExit(f"sha256 mismatch for {name}: {got} != {digest}")
    print(f"sha256 ok {name}")
PY
}

extract_license() {
  local tar_name=$1
  mkdir -p "$ROOT/build/licenses"
  python3 - "$RCLONE_VERSION" "$DL/$tar_name" "$ROOT/build/licenses/rclone-COPYING" <<'PY'
import pathlib, sys, tarfile
version, tar_path, out = sys.argv[1:]
member = f"rclone-{version}/COPYING"
with tarfile.open(tar_path) as tf:
    extracted = tf.extractfile(member)
    if extracted is None:
        raise SystemExit(f"missing {member}")
    data = extracted.read()
if b"Permission is hereby granted" not in data:
    raise SystemExit("rclone COPYING does not look like the MIT license")
pathlib.Path(out).write_bytes(data)
PY
  cat > "$ROOT/build/licenses/THIRD-PARTY-NOTICES.txt" <<EOF
Third-party software bundled with ${APP_NAME}

rclone ${RCLONE_VERSION}
  License: MIT
  Copyright: Nick Craig-Wood and the rclone contributors
  Source: https://github.com/rclone/rclone
  License text: rclone-COPYING (this directory)

${APP_NAME} itself is MIT licensed. It is not affiliated with or endorsed by
Synology or with rclone. Hyper Backup is a trademark of Synology Inc.
EOF
}

# Always download the pinned release. A cached ./rclone is not trusted.
fetch_rclone() {
  local base zip_name tar_name src
  command -v gpg >/dev/null 2>&1 || die "gpg is required to verify rclone SHA256SUMS"
  [ -f "$ROOT/packaging/rclone-release-signing-key.asc" ] || die "missing packaging/rclone-release-signing-key.asc"
  DL=$(mktemp -d "${TMPDIR:-/tmp}/hbk-rclone.XXXXXX")
  GPG_HOME=$(mktemp -d "${TMPDIR:-/tmp}/hbk-gpg.XXXXXX")
  chmod 700 "$GPG_HOME"
  base="https://downloads.rclone.org/${RCLONE_VERSION}"
  zip_name="rclone-${RCLONE_VERSION}-osx-${ARCH}.zip"
  tar_name="rclone-${RCLONE_VERSION}.tar.gz"
  curl -fsSL "$base/SHA256SUMS" -o "$DL/SHA256SUMS"
  curl -fsSL "$base/$zip_name" -o "$DL/$zip_name"
  curl -fsSL "$base/$tar_name" -o "$DL/$tar_name"
  verify_signed_sums
  check_sha256 "$zip_name" "$tar_name"
  unzip -qo "$DL/$zip_name" -d "$DL/unpack"
  src=$(find "$DL/unpack" -type f -name rclone -print)
  src=${src%%$'\n'*}
  [ -n "$src" ] || die "rclone binary missing from $zip_name"
  rm -f "$ROOT/rclone"
  cp "$src" "$ROOT/rclone"
  chmod 755 "$ROOT/rclone"
  xattr -c "$ROOT/rclone" 2>/dev/null || true
  expect_rclone_version "$ROOT/rclone"
  extract_license "$tar_name"
}

setup_python() {
  local py=${PYTHON:-python3}
  rm -rf .buildenv
  "$py" -m venv .buildenv
  # shellcheck disable=SC1091
  . .buildenv/bin/activate
  python -m pip install -q -U pip
  python -m pip install -q -r requirements.txt 'pyinstaller>=6.0'
}

bundle_file() {
  [ -f "$APP/Contents/Resources/$1" ] || [ -f "$APP/Contents/Frameworks/$1" ]
}

build_bundle() {
  APP="$ROOT/dist/${APP_NAME}.app"
  rm -rf "$APP" "$ROOT/build/work" "$ROOT/build/${APP_NAME}.spec"
  # Absolute paths: --specpath resolves relative --add-data sources from the spec directory.
  pyinstaller --noconfirm --windowed \
    --name "$APP_NAME" \
    --osx-bundle-identifier nl.callido.restore \
    --specpath "$ROOT/build" \
    --distpath "$ROOT/dist" \
    --workpath "$ROOT/build/work" \
    --add-data "$ROOT/static:static" \
    --add-binary "$ROOT/rclone:." \
    --icon "$ROOT/assets/AppIcon.icns" \
    --add-data "$ROOT/assets/logo.png:assets" \
    --add-data "$ROOT/build/licenses/rclone-COPYING:Licenses" \
    --add-data "$ROOT/build/licenses/THIRD-PARTY-NOTICES.txt:Licenses" \
    --hidden-import callido_restore.core.pool \
    --hidden-import hbk_pool \
    --hidden-import callido_restore.pool.poolmap \
    --hidden-import callido_restore.pool.poolmap2 \
    --hidden-import callido_restore.pool.anchor \
    --hidden-import callido_restore.pool.indexfetch \
    --hidden-import hbk_poolmap \
    --hidden-import lz4 \
    --hidden-import lz4.block \
    --collect-submodules lz4 \
    --hidden-import webview \
    --hidden-import webview.platforms.cocoa \
    --collect-submodules webview \
    "$ROOT/hbk_explorer.py"
  bundle_file "static/index.html" || die "static/index.html missing from the app bundle"
  [ -f "$APP/Contents/Resources/AppIcon.icns" ] || die "AppIcon.icns missing from the app bundle"
  bundle_file "Licenses/rclone-COPYING" || die "rclone license missing from the app bundle"
  bundle_file "Licenses/THIRD-PARTY-NOTICES.txt" || die "third-party notice missing from the app bundle"
}

# PyInstaller rewrites collected binaries and can drop the executable bit.
install_bundled_rclone() {
  local bin
  find "$APP" -type f -name rclone -exec chmod 755 {} \;
  bin="$APP/Contents/Frameworks/rclone"
  if [ ! -e "$bin" ]; then
    bin=$(find "$APP" -type f -name rclone -print || true)
    bin=${bin%%$'\n'*}
  fi
  [ -n "$bin" ] && [ -e "$bin" ] || die "bundled rclone binary not found"
  bin=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$bin")
  chmod 755 "$bin"
  if ! rclone_version_line "$bin" >/dev/null; then
    echo "restoring the verified rclone binary into the bundle" >&2
    cp "$ROOT/rclone" "$bin"
    chmod 755 "$bin"
    xattr -c "$bin" 2>/dev/null || true
    codesign --force --sign - "$bin"
  fi
  expect_rclone_version "$bin" >/dev/null
}

sign_bundle() {
  if codesign --verify --deep --strict "$APP" >/dev/null 2>&1; then
    return
  fi
  echo "ad-hoc signing the app bundle" >&2
  codesign --force --deep --sign - "$APP"
}

make_dmg() {
  local stage="$ROOT/dist/dmg" image="$ROOT/dist/${DMG_BASE}-${ARCH}.dmg" rw="$ROOT/dist/${DMG_BASE}-${ARCH}-rw.dmg" mnt try
  rm -rf "$stage"
  mkdir -p "$stage"
  ditto "$APP" "$stage/${APP_NAME}.app"
  cp "$ROOT/build/licenses/rclone-COPYING" "$stage/rclone-COPYING"
  cp "$ROOT/build/licenses/THIRD-PARTY-NOTICES.txt" "$stage/THIRD-PARTY-NOTICES.txt"
  cp "$ROOT/assets/AppIcon.icns" "$stage/.VolumeIcon.icns"
  rm -f "$image" "$rw"
  for try in 1 2 3; do
    if hdiutil create -volname "$APP_NAME" -srcfolder "$stage" -ov -format UDRW "$rw" >/dev/null; then break; fi
    [ "$try" = 3 ] && die "hdiutil create failed"
    sleep 2
  done
  # The custom-icon flag does not survive -srcfolder: set it on the mounted volume, then compress.
  mnt=$(mktemp -d "${TMPDIR:-/tmp}/hbk-dmg-rw.XXXXXX")
  dev=$(hdiutil attach -nobrowse -readwrite -noverify -mountpoint "$mnt" "$rw" | awk 'NR==1 {print $1}')
  if [ -n "$dev" ]; then
    if command -v SetFile >/dev/null 2>&1; then
      SetFile -a C "$mnt"; SetFile -a V "$mnt/.VolumeIcon.icns"
    else
      echo "warning: SetFile not found (Xcode command line tools); the disk image gets no custom volume icon" >&2
    fi
    # The volume can still be busy for a moment after writing (seen on the Intel CI runner): sync, then retry the detach
    # by device node; the image must be detached before hdiutil convert can read it.
    sync
    detached=
    for try in 1 2 3 4 5 6; do
      if hdiutil detach "$dev" >/dev/null 2>&1; then detached=1; break; fi
      sleep 3
    done
    if [ -z "$detached" ]; then
      hdiutil detach -force "$dev" >/dev/null 2>&1 || die "could not detach $dev ($mnt) after retries"
    fi
  else
    die "could not attach $rw"
  fi
  rmdir "$mnt" 2>/dev/null || true
  hdiutil convert -quiet "$rw" -format UDZO -o "$image" || die "hdiutil convert failed"
  rm -f "$rw"
  echo "built dist/${DMG_BASE}-${ARCH}.dmg"
}

choose_arch
setup_python
fetch_rclone
build_bundle
install_bundled_rclone
sign_bundle
make_dmg
