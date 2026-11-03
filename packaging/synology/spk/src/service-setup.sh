PYTHON_DIR="/var/packages/python314/target/bin"
PATH="${SYNOPKG_PKGDEST}/env/bin:${SYNOPKG_PKGDEST}/bin:${PYTHON_DIR}:${PATH}"
PYTHON="${SYNOPKG_PKGDEST}/env/bin/python3"
APP_DIR="${SYNOPKG_PKGDEST}/share/${SYNOPKG_PKGNAME}"
# the shared folder chosen in the install wizard (its path, saved at installation); the app offers it as the restore target
# (the app reads the file: a folder name may hold spaces, which the service command line cannot carry)
RESTORE_ROOT_FILE="${SYNOPKG_PKGVAR}/restore_root"

SVC_BACKGROUND=y
SVC_WRITE_PID=y
SVC_CWD="${APP_DIR}"

# HOME: the app keeps its settings, connections (rclone.conf), saved restores and download cache in the package's var folder
# CALLIDO_DSM=1: reached through DSM's web server (app/index.cgi checks the DSM login); listens on the loopback only
SERVICE_COMMAND="env HOME=${SYNOPKG_PKGVAR} CALLIDO_DSM=1 CALLIDO_RESTORE_ROOT_FILE=${RESTORE_ROOT_FILE} PYTHONPATH=${APP_DIR} LANG=en_US.UTF-8 LC_ALL=en_US.utf8 ${PYTHON} -m callido_restore --no-window --listen 127.0.0.1 --port 18765"

service_postinst ()
{
    install_python_virtualenv
    install_python_wheels

    # the package's own rclone (the release the app is tested with), found next to the app before any other rclone
    ln -sf "${SYNOPKG_PKGDEST}/bin/rclone" "${APP_DIR}/rclone"

    # the restore folder's path (SHARE_PATH: the wizard's shared folder, resolved by the installer); an upgrade keeps it
    if [ -n "${SHARE_PATH}" ]; then
        printf '%s\n' "${SHARE_PATH}" > "${RESTORE_ROOT_FILE}"
    fi
}
