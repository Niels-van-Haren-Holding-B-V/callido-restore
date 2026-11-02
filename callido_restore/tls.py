"""HTTPS context for the app's own requests (update check and download, Google key check); certificates are always verified.

The packaged app carries its own OpenSSL, which looks for root certificates where the Python that built it had them: for a
CI build that is ``/Library/Frameworks/Python.framework/Versions/3.x/etc/openssl``, a folder most Macs do not have. Every HTTPS
request then failed with CERTIFICATE_VERIFY_FAILED (the update check reported nothing newer than the last good check). On
macOS the system's own list of root certificates, ``/etc/ssl/cert.pem``, is therefore loaded as well. Windows: Python reads the
Windows certificate store itself.
"""
import os, ssl, sys

MAC_ROOTS = "/etc/ssl/cert.pem"
_ctx = None


def context():
    """A verifying SSL context: Python's defaults (including SSL_CERT_FILE / SSL_CERT_DIR) plus, on macOS, the system's roots."""
    global _ctx
    if _ctx is None:
        ctx = ssl.create_default_context()
        if sys.platform == "darwin" and os.path.isfile(MAC_ROOTS):
            try: ctx.load_verify_locations(cafile=MAC_ROOTS)
            except (OSError, ssl.SSLError): pass                    # unreadable: the defaults stay
        _ctx = ctx
    return _ctx
