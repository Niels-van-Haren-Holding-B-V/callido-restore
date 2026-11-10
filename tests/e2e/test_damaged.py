"""Issue #250: a backup on a local target (no revision suffix on any name) that ransomware (C3RB3R, *.L0CK3D) partly
encrypted. The synthetic backup (tests/e2e/hbkgen.py) is changed the same way before it is opened:

* every name loses its revision suffix (``786.db``, ``0.idx``, ``12.bucket``), as Hyper Backup writes them to a USB disk;
* ``Config/version_info.db`` is encrypted and renamed (the version list is rebuilt from the snapshot databases);
* the chunk table's slices are renamed *.L0CK3D, a zone of each is overwritten with random bytes and a 588-byte trailer is
  appended (what the report measured). The restore takes each chunk the table still gives from it and the rest from the
  inferred map, and every file still comes back byte for byte."""
import os, random, re, shutil, tempfile, unittest
from pathlib import Path

from tests.e2e import flow

TRAILER = 588


def strip_revisions(hbk):
    """Rename every <name>.<rev> under the backup to <name> (the newest revision wins, the others are removed)."""
    for d, _, files in os.walk(hbk):
        newest = {}
        for f in files:
            m = re.match(r"^(.*)\.(\d+)$", f)
            if m and not re.fullmatch(r"\d+", m.group(1)) and (m.group(1) not in newest or int(m.group(2)) > newest[m.group(1)][0]):
                newest[m.group(1)] = (int(m.group(2)), f)
        for f in files:
            m = re.match(r"^(.*)\.(\d+)$", f)
            if not m or m.group(1) not in newest: continue
            if newest[m.group(1)][1] == f: os.replace(os.path.join(d, f), os.path.join(d, m.group(1)))
            else: os.remove(os.path.join(d, f))


def lock(path, zones, rng):
    """What the ransomware does to one file: random bytes over ``zones`` [(start, end)], a trailer, the name + .L0CK3D."""
    b = bytearray(Path(path).read_bytes())
    for a, z in zones:
        z = min(z, len(b))
        if a < z: b[a:z] = rng.randbytes(z - a)
    Path(path + ".L0CK3D").write_bytes(bytes(b) + rng.randbytes(TRAILER)); os.remove(path)


def damage(hbk, rng=None):
    rng = rng or random.Random(250)
    strip_revisions(hbk)
    vi = os.path.join(hbk, "Config", "version_info.db"); lock(vi, [(0, 1 << 20)], rng)
    ci = os.path.join(hbk, "Pool", "chunk_index")
    for f in sorted(os.listdir(ci)):
        n = os.path.getsize(os.path.join(ci, f)); lock(os.path.join(ci, f), [(n // 3, 2 * n // 3)], rng)   # a third of the table


class LocalTargetRansomware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="hbk-e2e-250-")
        cls.rep = flow.run_inprocess(cls.work, log=lambda m: None, chunk_index=True, mutate=damage)
        cls.m = cls.rep["manifest"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_opens_with_the_versions_from_the_snapshots(self):
        o = self.rep["open"]
        self.assertTrue(o["readable"], o)
        self.assertEqual(o["versions"], [101, 102, 103])
        self.assertEqual(o["shares"], ["Documents", "Photos", "homes"])

    def test_damaged_chunk_table_gets_the_inferred_map(self):
        self.assertTrue(self.rep["pool_status"]["map"], flow.summary(self.rep))
        self.assertTrue(self.rep["pool_status"]["complete"], flow.summary(self.rep))

    def test_last_clean_run_restores_byte_exact(self):
        r = self.rep["restores"][self.m["last_clean"]]
        self.assertEqual(r["errors"], [], flow.summary(self.rep))
        for k in ("missing", "wrong_bytes", "wrong_mtime", "extra"): self.assertEqual(r[k], [], f"{k}\n{flow.summary(self.rep)}")


class DamagedChunkTable(unittest.TestCase):
    def test_damaged_records_are_rejected_never_misread(self):
        """A record in a zone of random bytes is refused (KeyError), so the restore falls back to the map; none gives a wrong place."""
        from tests.e2e import hbkgen
        from callido_restore.core import pool as P
        from callido_restore.sources.source import Source
        work = tempfile.mkdtemp(prefix="hbk-e2e-250-"); self.addCleanup(shutil.rmtree, work, True); hbk = os.path.join(work, "e2e_1.hbk")
        hbkgen.build(hbk, 1.0, 256 * 1024, chunk_index=True)
        before = P.ChunkIndex(Source("a", "a", hbk)); ci = os.path.join(hbk, "Pool/chunk_index")
        n = (sum(os.path.getsize(os.path.join(ci, f)) for f in os.listdir(ci)) - 64) // 29
        good = {}
        for g in range(n):
            try: good[g] = before.loc(g)
            except KeyError: pass
        damage(hbk)
        after = P.ChunkIndex(Source("b", "b", hbk)); self.assertTrue(after.damaged())
        same = rejected = 0
        for g, loc in good.items():
            try: self.assertEqual(after.loc(g), loc, f"chunk {g}"); same += 1
            except KeyError: rejected += 1
        self.assertGreater(rejected, len(good) // 5); self.assertGreater(same, len(good) // 5)


if __name__ == "__main__":
    unittest.main()
