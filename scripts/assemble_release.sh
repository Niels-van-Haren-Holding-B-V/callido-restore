#!/bin/bash
# Write SHA256SUMS for the arm64 and Intel disk images in a directory.
# Usage: scripts/assemble_release.sh <directory>
set -euo pipefail
dir=${1:?release directory}
python3 - "$dir" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
names = ["HyperBackupDesktop-arm64.dmg", "HyperBackupDesktop-amd64.dmg"]
missing = [name for name in names if not (root / name).is_file()]
if missing:
    raise SystemExit("missing disk images: " + ", ".join(missing))

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

lines = [f"{sha256(root / name)}  {name}" for name in names]
text = "\n".join(lines) + "\n"
(root / "SHA256SUMS").write_text(text)
sys.stdout.write(text)
PY
