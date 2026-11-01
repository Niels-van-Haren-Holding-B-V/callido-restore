"""SlicedStream is read from several threads at once during a restore to a NAS: the upload threads check each uploaded file
(PoolReader.verification), the main thread reads the next file's record (plan/extract) and the prefetch threads plan ahead.
A read is seek + read on one cached file handle per slice, so without a lock one thread's seek lands between another's seek and
read and the record comes back with the wrong bytes (Niels, Windows, NAS target: "The check itself failed: record trailer
missing" for a photo that verifies fine on its own)."""
import os, shutil, tempfile, threading, unittest
from callido_restore.core import pool as P


class _Src:
    def __init__(self, d): self.d = d
    def listdir(self, reldir): return {n: {} for n in os.listdir(os.path.join(self.d, reldir))}
    def ensure(self, rel, job=None, progress=None, cancelled=None): return os.path.join(self.d, rel)


class Threads(unittest.TestCase):
    def test_concurrent_reads_return_their_own_bytes(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "Config/x.index"))
        size = 1 << 20                                             # the read offset written into every 8 bytes: easy to check
        with open(os.path.join(d, "Config/x.index/0.idx.1"), "wb") as f: f.write(b"".join(i.to_bytes(8, "big") for i in range(0, size, 8)))
        st = P.SlicedStream(_Src(d), "Config/x.index"); bad = []
        def reader(seed):
            o = (seed * 7919 * 8) % (size - 4096)
            for _ in range(3000):
                o = (o + 8 * 1237) % (size - 4096); o -= o % 8
                b = st.read(o, 64)
                if int.from_bytes(b[:8], "big") != o: bad.append(o); return
        ts = [threading.Thread(target=reader, args=(i,)) for i in range(8)]
        for t in ts: t.start()
        for t in ts: t.join()
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
