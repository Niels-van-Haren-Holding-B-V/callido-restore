SPK_NAME = callido-restore
SPK_VERS = 1.3.0
SPK_REV = 1
SPK_ICON = src/callido-restore.png

PYTHON_PACKAGE = python314

WHEELS = src/requirements-crossenv.txt

DEPENDS = cross/callido-restore
# the same rclone release the desktop app ships; the app runs its daemon (rclone rcd), so DSM 7 kernels only
DEPENDS += cross/rclone

MAINTAINER = Callido B.V.
MAINTAINER_URL = https://github.com/Niels-van-Haren-Holding-B-V/callido-restore
# the maintainer is a company, not a GitHub user: no GitHub lookup for its name and URL
DISABLE_GITHUB_MAINTAINER = 1
# spksrc writes these into INFO through the shell: no parentheses in DESCRIPTION or CHANGELOG
DESCRIPTION = "Restore files from a Synology Hyper Backup without Hyper Backup: browse every backup run on a timeline and restore the folders you pick straight into the shared folders of this NAS. The backup can be on Google Drive or any rclone remote, on a share or on a disk. Not affiliated with or endorsed by Synology."
DISPLAY_NAME = Callido Restore
CHANGELOG = "First package: Callido Restore runs on the NAS and opens from the DSM main menu, for DSM administrators."

HOMEPAGE  = https://github.com/Niels-van-Haren-Holding-B-V/callido-restore
LICENSE   = MIT
STARTABLE = yes
REQUIRED_MIN_DSM = 7.0

WIZARDS_DIR = src/wizard

SERVICE_USER  = auto
SERVICE_SETUP = src/service-setup.sh
# the restore folder: DSM creates the shared folder if needed and gives the package user Read/Write on it (data share worker)
SERVICE_WIZARD_SHARENAME = wizard_restore_share

# Inside DSM: the app listens on the NAS's loopback only and DSM's web server is the way in (src/app/index.cgi, DSM login),
# so there is no port of its own, no firewall rule and no password; the DSM main menu entry is src/app/config.
DSM_UI_CONFIG = src/app/config
POST_STRIP_TARGET = callido_restore_dsm_app

# the download cache and rclone's temporary files: not /tmp, which is small on DSM
USE_ALTERNATE_TMPDIR = 1

include ../../mk/spksrc.spk-meta.mk

.PHONY: callido_restore_dsm_app
callido_restore_dsm_app:
	install -m 755 -d $(STAGING_DIR)/app
	install -m 755 src/app/index.cgi $(STAGING_DIR)/app/index.cgi
	install -m 644 src/app/dsm_cgi.py $(STAGING_DIR)/app/dsm_cgi.py
