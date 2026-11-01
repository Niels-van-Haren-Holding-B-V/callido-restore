#!/bin/bash
# Fetch the pinned rclone for the end-to-end CI job (linux or osx, amd64 or arm64) into <dest>/rclone, the same way build_app.sh
# does for the app: SHA256SUMS must carry a valid signature by the rclone release key (packaging/rclone-release-signing-key.asc),
# and the zip must match its signed checksum.   usage: scripts/fetch_rclone_ci.sh <dest dir>
set -euo pipefail
cd "$(dirname "$0")/.."
RCLONE_VERSION=$(sed -n 's/^RCLONE_VERSION=//p' build_app.sh)
RCLONE_SIGNING_FPR=$(sed -n 's/^RCLONE_SIGNING_FPR=//p' build_app.sh)
DEST=${1:?usage: $0 <dest dir>}
case "$(uname -s)" in Linux) OS=linux ;; Darwin) OS=osx ;; *) echo "unsupported OS $(uname -s)" >&2; exit 1 ;; esac
case "$(uname -m)" in x86_64|amd64) ARCH=amd64 ;; arm64|aarch64) ARCH=arm64 ;; *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;; esac
ZIP="rclone-${RCLONE_VERSION}-${OS}-${ARCH}.zip"; BASE="https://downloads.rclone.org/${RCLONE_VERSION}"
DL=$(mktemp -d); GNUPGHOME=$(mktemp -d); export GNUPGHOME
trap 'rm -rf "$DL" "$GNUPGHOME"' EXIT
curl -fsSL "$BASE/SHA256SUMS" -o "$DL/SHA256SUMS"
curl -fsSL "$BASE/$ZIP" -o "$DL/$ZIP"
gpg --batch --import packaging/rclone-release-signing-key.asc >/dev/null 2>&1
gpg --batch --status-fd 1 --verify "$DL/SHA256SUMS" >"$DL/status.txt" 2>/dev/null || { echo "SHA256SUMS signature invalid" >&2; exit 1; }
grep -q "VALIDSIG ${RCLONE_SIGNING_FPR}" "$DL/status.txt" || { echo "SHA256SUMS not signed by ${RCLONE_SIGNING_FPR}" >&2; exit 1; }
gpg --batch --yes --output "$DL/SHA256SUMS.clear" --decrypt "$DL/SHA256SUMS" >/dev/null 2>&1
want=$(awk -v n="$ZIP" '$2==n {print $1}' "$DL/SHA256SUMS.clear")
got=$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$DL/$ZIP")
[ -n "$want" ] && [ "$want" = "$got" ] || { echo "sha256 mismatch for $ZIP" >&2; exit 1; }
unzip -qo "$DL/$ZIP" -d "$DL/unpack"
mkdir -p "$DEST"; install -m 0755 "$DL/unpack/rclone-${RCLONE_VERSION}-${OS}-${ARCH}/rclone" "$DEST/rclone"
"$DEST/rclone" version | head -1
