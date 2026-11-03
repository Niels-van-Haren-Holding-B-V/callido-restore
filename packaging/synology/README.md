# Synology package (.spk)

A [SynoCommunity spksrc](https://github.com/SynoCommunity/spksrc) recipe for Callido Restore on DSM 7: the app runs on the
NAS as a service (network mode, `--listen 0.0.0.0 --port 8765`, see `docs/NAS.md`) and is opened from a browser.

* `cross/` → `spksrc/cross/callido-restore`: the app's source archive (`callido_restore/`, `static/`, `LICENSE`) of the
  release `PKG_VERS` (or of one commit with `CALLIDO_GIT_HASH=<sha>` in the environment), installed to `share/callido-restore`.
* `spk/` → `spksrc/spk/callido-restore`: the package. Python 3.14 is SynoCommunity's `python314` package (installed by
  Package Center as a dependency); `lz4` is cross-compiled as a wheel; rclone is spksrc's `cross/rclone`, the same release
  as the desktop apps. The install wizard asks for the sign-in password (kept in the package's `var/password`, mode 600; an
  upgrade keeps it) and explains the shared-folder permission for the package's system user `callido-restore`.

Settings, connections (`rclone.conf`), saved restores and the download cache live in `/var/packages/callido-restore/var`
(the service's `HOME`).

## Build

`.github/workflows/spk.yml` builds it: the x64 package on a pull request that changes the recipe, every DSM 7 architecture
(x64, aarch64, armv7) on a `v*` tag, attached to the GitHub release. By hand, in spksrc's build image:

```sh
git clone https://github.com/SynoCommunity/spksrc.git && cd spksrc
cp -R <this repo>/packaging/synology/spk spk/callido-restore
cp -R <this repo>/packaging/synology/cross cross/callido-restore
docker run --rm -v "$PWD:/spksrc" -w /spksrc ghcr.io/synocommunity/spksrc bash -c \
  'make setup && make -C cross/callido-restore digests && make -C spk/callido-restore arch-x64-7.1'
ls packages/      # callido-restore_x64-7.1_<version>-1.spk
```

On macOS the checkout must be on a case-sensitive file system (for example a Docker volume): Synology's toolchains hold
files whose names differ only in case.

## Install

1. Package Center → Settings → Package Sources → Add: name `SynoCommunity`, location `https://packages.synocommunity.com`.
2. Install **Python 3.14** from the *Community* tab (the package depends on it; a Manual Install does not download
   dependencies by itself and stops with "install python314 first").
3. Package Center → **Manual Install** → the `.spk` for the NAS's processor (`x64` for Intel/AMD, `aarch64` for 64-bit ARM,
   `armv7` for 32-bit ARM).

The password comes from the install wizard. To change it, edit `/var/packages/callido-restore/var/password` over SSH (one line)
and restart the package in Package Center. The package and the Docker image (`docs/NAS.md`) both use port 8765: use one.

## SynoCommunity

The same two folders are what a pull request to SynoCommunity/spksrc adds (`spk/callido-restore`, `cross/callido-restore`
with a `digests` file for the release archive, made by `make digests`). Their rules: fresh installs and upgrades must work.
