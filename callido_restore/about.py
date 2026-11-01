"""Product identity. ``APP_NAME`` is the one place the name lives: build_app.sh, the smoke script, the window title, About
and the README all take it from here."""
APP_NAME = "Callido Restore"

TAGLINE = "Restore Synology Hyper Backup (.hbk) files"

NOTICE = "Not affiliated with or endorsed by Synology. Hyper Backup is a trademark of Synology Inc."

VERSION = "1.1.0"

REPO = "Niels-van-Haren-Holding-B-V/callido-restore"   # the only place updates come from (GitHub releases of this repository)

# Ed25519 public keys (hex) whose signature on a release's checksum list makes an update installable (update.py). The private
# key exists only as the GitHub Actions secret RELEASE_SIGNING_KEY. Rotation: add the new key here in a release signed with the
# old one, sign later releases with the new one, then drop the old key (docs/HANDOVER.md, "Release signing"). Empty: updates are
# only offered as a link to the release page.
UPDATE_PUBLIC_KEYS = []
