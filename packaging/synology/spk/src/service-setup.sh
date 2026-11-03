PYTHON_DIR="/var/packages/python312/target/bin"
PATH="${SYNOPKG_PKGDEST}/env/bin:${SYNOPKG_PKGDEST}/bin:${PYTHON_DIR}:${PATH}"
PYTHON="${SYNOPKG_PKGDEST}/env/bin/python3"
APP_DIR="${SYNOPKG_PKGDEST}/share/${SYNOPKG_PKGNAME}"
PASSWORD_FILE="${SYNOPKG_PKGVAR}/password"

SVC_BACKGROUND=y
SVC_WRITE_PID=y
SVC_CWD="${APP_DIR}"

# HOME: the app keeps its settings, connections (rclone.conf), saved restores and download cache in the package's var folder
SERVICE_COMMAND="env HOME=${SYNOPKG_PKGVAR} PYTHONPATH=${APP_DIR} LANG=en_US.UTF-8 LC_ALL=en_US.utf8 ${PYTHON} -m callido_restore --listen 0.0.0.0 --port 8765 --password-file ${PASSWORD_FILE}"

service_postinst ()
{
    install_python_virtualenv
    install_python_wheels

    # the package's own rclone (the release the app is tested with), found next to the app before any other rclone
    ln -sf "${SYNOPKG_PKGDEST}/bin/rclone" "${APP_DIR}/rclone"

    # the password from the installation wizard; an upgrade keeps the existing one
    if [ -n "${wizard_password}" ]; then
        (umask 077; printf '%s\n' "${wizard_password}" > "${PASSWORD_FILE}")
    fi
}
