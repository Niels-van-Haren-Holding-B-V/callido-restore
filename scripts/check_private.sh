#!/bin/bash
# Fail when a tracked file contains personal data (H18). Two pattern sources:
#  * built-in generic patterns (home folder paths, private IPs, e-mail addresses; /Volumes/<name> is a normal macOS path, so real
#    volume names belong in the denylist);
#  * a denylist of extended regexes, one per line, with the real names that must never appear: the PRIVATE_DENYLIST
#    environment variable (a GitHub Actions secret in CI) or, if that is empty, the gitignored file .private-denylist in the
#    repo, or ~/.hbk-explorer/private-denylist.
# The denylist itself is never committed. Lines starting with # and blank lines are ignored.
# Usage: scripts/check_private.sh            (exit 1 on a hit, 0 when clean)
set -euo pipefail
cd "$(dirname "$0")/.."

builtin_patterns=(
  '/Users/[A-Za-z]'
  '\b(10\.[0-9]{1,3}|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
)
# Files that legitimately carry a name or address (licence, code owners, rclone's release signing key, binary assets).
excluded=(':!LICENSE' ':!.github/CODEOWNERS' ':!assets/*' ':!packaging/*.asc' ':!scripts/check_private.sh')
# Matches of the built-in patterns that are not personal data.
allow='git@github\.com'

patterns=("${builtin_patterns[@]}")
source="built-in patterns only"
if [ -n "${PRIVATE_DENYLIST:-}" ]; then
  source="PRIVATE_DENYLIST"
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    patterns+=("$line")
  done <<< "$PRIVATE_DENYLIST"
else
  for f in .private-denylist "$HOME/.hbk-explorer/private-denylist"; do
    [ -f "$f" ] || continue
    source="$f"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|'#'*) continue ;; esac
      patterns+=("$line")
    done < "$f"
    break
  done
fi

hits=0
for pat in "${patterns[@]}"; do
  if out=$(git grep -n -I -E -i -- "$pat" -- . "${excluded[@]}" | grep -v -E "$allow"); [ -n "$out" ]; then
    echo "private data matching '$pat':"; echo "$out" | cut -c1-200; hits=1
  fi
done
if [ "$hits" -ne 0 ]; then
  echo "check_private: FAILED ($source)"; exit 1
fi
echo "check_private: clean ($source, ${#patterns[@]} patterns)"
if [ "$source" = "built-in patterns only" ]; then
  echo "note: no denylist found (PRIVATE_DENYLIST secret or .private-denylist file); only the generic patterns ran"
fi
