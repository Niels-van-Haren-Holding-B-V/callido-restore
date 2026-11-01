"""Ed25519 verification against RFC 8032 section 7.1 test vectors and against signatures made by OpenSSL."""
import os, shutil, subprocess, tempfile, unittest

from callido_restore import ed25519

H = bytes.fromhex
RFC = [   # (public key, message, signature) from RFC 8032 section 7.1, TEST 1-3
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


class RFC8032(unittest.TestCase):
    def test_vectors_verify(self):
        for pk, msg, sig in RFC: self.assertTrue(ed25519.verify(H(pk), H(msg), H(sig)), pk)

    def test_tampering_fails(self):
        pk, msg, sig = (H(x) for x in RFC[2])
        self.assertFalse(ed25519.verify(pk, msg + b"\0", sig))                     # another message
        self.assertFalse(ed25519.verify(pk, msg, sig[:-1] + bytes([sig[-1] ^ 1])))  # a flipped bit in s
        self.assertFalse(ed25519.verify(pk, msg, bytes([sig[0] ^ 1]) + sig[1:]))    # a flipped bit in R
        self.assertFalse(ed25519.verify(H(RFC[0][0]), msg, sig))                    # another key
        self.assertFalse(ed25519.verify(pk, msg, sig[:63]))                         # wrong lengths
        self.assertFalse(ed25519.verify(pk[:31], msg, sig))


OPENSSL = shutil.which("openssl")


def _openssl_ok():
    if not OPENSSL: return False
    d = tempfile.mkdtemp()
    r = subprocess.run([OPENSSL, "genpkey", "-algorithm", "ed25519", "-out", os.path.join(d, "k.pem")], capture_output=True)
    shutil.rmtree(d, True); return r.returncode == 0


@unittest.skipUnless(_openssl_ok(), "needs an OpenSSL with Ed25519 (OpenSSL 1.1.1+)")
class AgainstOpenSSL(unittest.TestCase):
    """The release workflow signs with OpenSSL; the app must accept exactly those signatures."""
    def test_openssl_signature_verifies(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        key, msg, sig = (os.path.join(d, n) for n in ("k.pem", "SHA256SUMS", "SHA256SUMS.sig"))
        open(msg, "w").write("a63edc9e48d20d02886354afd243bcc39440c865f97cc6631ffc7588b958bf55  CallidoRestore-arm64.dmg\n")
        subprocess.run([OPENSSL, "genpkey", "-algorithm", "ed25519", "-out", key], check=True, capture_output=True)
        subprocess.run([OPENSSL, "pkeyutl", "-sign", "-rawin", "-inkey", key, "-in", msg, "-out", sig], check=True, capture_output=True)
        der = subprocess.run([OPENSSL, "pkey", "-in", key, "-pubout", "-outform", "DER"], check=True, capture_output=True).stdout
        pub = der[-32:]
        self.assertTrue(ed25519.verify(pub, open(msg, "rb").read(), open(sig, "rb").read()))
        self.assertFalse(ed25519.verify(pub, open(msg, "rb").read().replace(b"a63e", b"a63f"), open(sig, "rb").read()))
