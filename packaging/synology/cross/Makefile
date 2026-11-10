PKG_NAME = callido-restore
PKG_VERS = 1.3.1
PKG_EXT = tar.gz
PKG_DIST_SITE = https://github.com/Niels-van-Haren-Holding-B-V/callido-restore/archive
ifeq ($(strip $(CALLIDO_GIT_HASH)),)
# a release: the tag's source archive
PKG_DIST_NAME = v$(PKG_VERS).$(PKG_EXT)
PKG_DIST_FILE = $(PKG_NAME)-$(PKG_VERS).$(PKG_EXT)
PKG_DIR = $(PKG_NAME)-$(PKG_VERS)
else
# a test build of one commit (CI on a pull request): CALLIDO_GIT_HASH=<full sha> in the environment
PKG_DIST_NAME = $(CALLIDO_GIT_HASH).$(PKG_EXT)
PKG_DIST_FILE = $(PKG_NAME)-$(CALLIDO_GIT_HASH).$(PKG_EXT)
PKG_DIR = $(PKG_NAME)-$(CALLIDO_GIT_HASH)
endif

HOMEPAGE = https://github.com/Niels-van-Haren-Holding-B-V/callido-restore
COMMENT  = Restore files from a Synology Hyper Backup without Hyper Backup, in the browser.
LICENSE  = MIT

INSTALL_TARGET = callido_restore_install

include ../../mk/spksrc.cross-install.mk

.PHONY: callido_restore_install
callido_restore_install:
	mkdir -p $(STAGING_INSTALL_PREFIX)/share/$(PKG_NAME)
	tar -cf - -C $(WORK_DIR)/$(PKG_DIR) callido_restore static LICENSE | tar -xf - -C $(STAGING_INSTALL_PREFIX)/share/$(PKG_NAME)
