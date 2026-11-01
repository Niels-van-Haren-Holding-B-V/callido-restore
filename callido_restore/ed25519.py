"""Ed25519 signature verification (RFC 8032, section 5.1.7), pure Python, standard library only.

Used to check that an update's checksum list was signed with the release key (``about.UPDATE_PUBLIC_KEYS``). Verification
only: the app never signs. Follows the RFC's reference algorithm; checked against the RFC 8032 test vectors and against
signatures made by OpenSSL (tests/test_ed25519.py). Not constant-time, which does not matter for verifying public data.
"""
import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493           # the group order (q in the RFC)


def _inv(x): return pow(x, P - 2, P)


D = -121665 * _inv(121666) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _add(a, b):
    """Point addition in extended coordinates (X, Y, Z, T)."""
    A = (a[1] - a[0]) * (b[1] - b[0]) % P
    B = (a[1] + a[0]) * (b[1] + b[0]) % P
    C = 2 * a[3] * b[3] * D % P
    Dd = 2 * a[2] * b[2] % P
    E, F, G, H = B - A, Dd - C, Dd + C, B + A
    return (E * F % P, G * H % P, F * G % P, E * H % P)


def _mul(s, pt):
    q = (0, 1, 1, 0)                                               # the neutral element
    while s > 0:
        if s & 1: q = _add(q, pt)
        pt = _add(pt, pt); s >>= 1
    return q


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _recover_x(y, sign):
    if y >= P: return None
    x2 = (y * y - 1) * _inv(D * y * y + 1)
    if x2 == 0: return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0: x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0: return None
    if (x & 1) != sign: x = P - x
    return x


def _decompress(s):
    if len(s) != 32: return None
    y = int.from_bytes(s, "little"); sign = y >> 255; y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


G_Y = 4 * _inv(5) % P
G = (_recover_x(G_Y, 0), G_Y, 1, _recover_x(G_Y, 0) * G_Y % P)


def verify(public, message, signature):
    """True when ``signature`` (64 bytes) is a valid Ed25519 signature of ``message`` under ``public`` (32 bytes)."""
    if len(public) != 32 or len(signature) != 64: return False
    A = _decompress(public)
    if A is None: return False
    R = _decompress(signature[:32])
    if R is None: return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L: return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % L
    return _equal(_mul(s, G), _add(R, _mul(h, A)))
