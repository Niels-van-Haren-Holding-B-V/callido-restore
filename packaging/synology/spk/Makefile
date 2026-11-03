SPK_NAME = callido-restore
SPK_VERS = 1.3.0
SPK_REV = 1
SPK_ICON = src/callido-restore.png

PYTHON_PACKAGE = python312

WHEELS = src/requirements-crossenv.txt

DEPENDS = cross/callido-restore
# the same rclone release the desktop app ships; the app runs its daemon (rclone rcd), so DSM 7 kernels only
DEPENDS += cross/rclone

MAINTAINER = Niels-van-Haren-Holding-B-V
DESCRIPTION = "Restore files from a Synology Hyper Backup (.hbk) without Hyper Backup: browse every backup run on a timeline and restore the folders you pick straight into this NAS's shared folders. The backup can be on Google Drive or any rclone remote, on a share or on a disk. Not affiliated with or endorsed by Synology."
DISPLAY_NAME = Callido Restore
CHANGELOG = "First package: the app runs on the NAS and is opened from a browser (sign-in with the password from the installation)."

HOMEPAGE  = https://github.com/Niels-van-Haren-Holding-B-V/callido-restore
LICENSE   = MIT
STARTABLE = yes
REQUIRED_MIN_DSM = 7.0

WIZARDS_DIR = src/wizard

SERVICE_USER  = auto
SERVICE_SETUP = src/service-setup.sh

SERVICE_PORT  = 8765
SERVICE_PORT_TITLE = $(DISPLAY_NAME)
ADMIN_PORT = $(SERVICE_PORT)

# the download cache and rclone's temporary files: not /tmp, which is small on DSM
USE_ALTERNATE_TMPDIR = 1

include ../../mk/spksrc.spk-meta.mk
