"""Issue #250: names without a revision (a local target), slices renamed *.L0CK3D with a trailer, and record streams parsed
slice by slice (a whole stream in memory reached 12 GB) that skip zones of garbage."""
import os, random, shutil, struct, tempfile, unittest
from callido_restore.core import pool as P
from callido_restore.core.hbkmeta import rev_of, split_name, RANSOM_TRAILER


def record(ids, attr=False):
    refs = b"".join(struct.pack(">Q", 64 + 29 * g) for g in ids) + struct.pack(">II", 0x74000000, 0)
    if attr:                                    # the attribute TLV's last body word doubles as the list header's flags (FORMAT.md)
        return struct.pack(">II", P.ATTR_MAGIC, 28) + bytes(28) + struct.pack(">II", P.REC_MAGIC, len(refs)) + refs
    return struct.pack(">III", 0, P.REC_MAGIC, len(refs)) + refs


def stream(rng, n=400):
    """A record stream (64-byte header, records with markers between) and the (key, ids) a parse must report."""
    out = bytearray(64); want = []; g = 0
    for i in range(n):
        if rng.random() < 0.3: out += struct.pack(">II", 0xa93b0156, 4) + bytes(4)                 # a marker TLV
        ids = list(range(g, g + rng.randint(1, 40))); g += len(ids) + rng.randint(0, 5)
        attr = rng.random() < 0.1; r = record(ids, attr)
        key = len(out) + (8 if attr else 0)                       # pointer B: the attribute body (p - 28) or the header (p - 4)
        want.append((key, ids)); out += r
    return bytes(out), want


def parse(paths, sizes=None):
    got = []; P.parse_record_stream(paths, lambda k, ids: got.append((k, ids)), sizes=sizes); return got


class Names(unittest.TestCase):
    def test_revision_is_optional(self):
        self.assertEqual(rev_of("version_info.db", "version_info.db"), (0, False))
        self.assertEqual(rev_of("version_info.db.12", "version_info.db"), (12, False))
        self.assertEqual(rev_of("version_info.db.L0CK3D", "version_info.db"), (0, True))
        self.assertIsNone(rev_of("version_info.dbx", "version_info.db"))
        self.assertEqual(split_name("12.bucket", r"(\d+)\.(bucket|index)"), (("12", "bucket"), 0, False))
        self.assertEqual(split_name("12.index.4.L0CK3D", r"(\d+)\.(bucket|index)"), (("12", "index"), 4, True))
        self.assertEqual(split_name("786.db", r"(\d+)\.db"), (("786",), 0, False))
        self.assertIsNone(split_name("complete_list.db", r"(\d+)\.db"))


class _Src:
    def __init__(self, d): self.d = d
    def listdir(self, rel): return {n: {"dir": False} for n in os.listdir(os.path.join(self.d, rel))}
    def ensure(self, rel, job=None, progress=None, cancelled=None): return os.path.join(self.d, rel)


class LockedSlices(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True); os.makedirs(os.path.join(self.d, "S"))
    def put(self, name, data):
        with open(os.path.join(self.d, "S", name), "wb") as f: f.write(data)
    def test_trailer_is_not_stream_data(self):
        a = bytes(range(256)) * (P.SLICE // 256); b = b"tail of the stream"
        self.put("0.idx.L0CK3D", a + b"T" * RANSOM_TRAILER); self.put("1.idx.L0CK3D", b + b"T" * RANSOM_TRAILER)
        st = P.SlicedStream(_Src(self.d), "S")
        self.assertEqual(st.locked(), {0, 1})
        self.assertEqual(st.read(P.SLICE - 4, 4 + len(b)), a[-4:] + b)            # across the boundary, no trailer in between
        self.assertEqual(st.slice_bytes(1), b)
        with self.assertRaises(EOFError): st.read(P.SLICE + len(b), 1)
    def test_plain_names_and_readable_copy_preferred(self):
        self.put("0.idx", b"plain"); self.put("1.idx.3", b"three"); self.put("1.idx.3.L0CK3D", b"xxxxx" + bytes(RANSOM_TRAILER)); self.put("1.idx.2", b"two")
        st = P.SlicedStream(_Src(self.d), "S")
        self.assertEqual(st.names(), {0: "0.idx", 1: "1.idx.3"}); self.assertEqual(st.locked(), set())


class StreamParse(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        self.window = P.PARSE_WINDOW; P.PARSE_WINDOW = 512; self.addCleanup(setattr, P, "PARSE_WINDOW", self.window)
    def cut(self, data, sizes):
        paths = []; p = 0
        for i, n in enumerate(sizes + [len(data)]):
            q = os.path.join(self.d, f"{i}.idx"); open(q, "wb").write(data[p:p + n]); paths.append(q); p += n
            if p >= len(data): break
        return paths
    def test_slice_by_slice_equals_whole(self):
        rng = random.Random(1); data, want = stream(rng)
        self.assertEqual(parse(self.cut(data, [len(data)])), want)
        self.assertEqual(parse(self.cut(data, [rng.randint(1, 3000) for _ in range(200)])), want)   # records across every kind of cut
    def test_garbage_zone_is_skipped(self):
        rng = random.Random(2); data, want = stream(rng, 600); b = bytearray(data)
        a, z = len(b) // 3, len(b) // 2
        for _ in range(30): b[a:z] = rng.randbytes(z - a)
        got = parse(self.cut(bytes(b), [4096] * 1000))
        outside = [(k, ids) for k, ids in want if k + 4 + 12 + 8 * (len(ids) + 1) <= a or k >= z]
        self.assertEqual([x for x in got if x in outside], outside)               # every record outside the zone is found
        self.assertTrue(all(x in want for x in got))                                 # and nothing made up from the garbage
    def test_magic_on_the_window_edge_during_a_resync(self):
        """Garbage up to a record whose magic straddles the end of what the parse has read: the record is still found."""
        rng = random.Random(4); r = record([5, 6, 7])
        for cut in range(len(r) + 8):
            garbage = bytes(64) + bytes([0x11]) * 3000                               # no 0xa9 in it: one resync over all of it
            data = garbage + r; first = len(garbage) + 4 + cut - 8
            got = parse(self.cut(data, [max(1, first)]))
            self.assertEqual(got, [(len(garbage), [5, 6, 7])], f"cut {cut}")

    def test_sizes_cut_a_trailer(self):
        rng = random.Random(3); data, want = stream(rng)
        q = os.path.join(self.d, "0.idx.L0CK3D"); open(q, "wb").write(data + rng.randbytes(RANSOM_TRAILER))
        self.assertEqual(parse([q], sizes=[len(data)]), want)


class DamagedTableRecord(unittest.TestCase):
    def test_nonzero_padding_is_rejected(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True); os.makedirs(os.path.join(d, "Pool/chunk_index"))
        good = b"\x00" + struct.pack(">II", 7, 64 + 32 * 3) + bytes(15) + b"\x01" + bytes(4)
        bad = b"\x00" + struct.pack(">II", 7, 64 + 32 * 3) + bytes(5) + b"\x99" + bytes(9) + b"\x01" + bytes(4)
        # bytes 16 and 23 are set in valid records of a real backup (0x03 in byte 16 on 4.3 % of them): never refused for that
        b16 = b"\x00" + struct.pack(">II", 8, 64 + 32 * 5) + bytes(7) + b"\x03" + bytes(6) + b"\x02" + b"\x01" + bytes(4)
        open(os.path.join(d, "Pool/chunk_index/0.idx"), "wb").write(bytes(64) + good + bad + b16)
        ci = P.ChunkIndex(_Src(d))
        self.assertEqual(ci.loc(0), (7, 3))
        with self.assertRaisesRegex(KeyError, "damaged"): ci.loc(1)
        self.assertEqual(ci.loc(2), (8, 5))


class Md5Retry(unittest.TestCase):
    """A table record on the edge of an encrypted zone can pass the record check with a garbage location: the chunk then fails
    its md5, and with a damaged table and a map loaded the map's location is tried before the file fails."""
    def reader(self, damaged, have_map):
        r = P.PoolReader.__new__(P.PoolReader)
        r.map = {} if have_map else None
        r.ci = type("CI", (), {"damaged": lambda self: damaged})()
        r.plan = lambda off: ([(1, 0), (1, 1)], {"ids": [10, 11]})
        r.locate = lambda g: (2, g)
        def read_chunk(b, i, fh, job=None, opener=None):
            if b == 1 and i == 1: raise ValueError("md5 mismatch bucket 1 entry 1")
            return f"{b}:{i};".encode()
        r.read_chunk = read_chunk
        return r
    def test_map_location_is_tried(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True); out = os.path.join(d, "f")
        self.reader(True, True).extract(0, out)
        self.assertEqual(open(out, "rb").read(), b"1:0;2:11;")
    def test_missing_bucket_or_entry_also_tries_the_map(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        for err in (KeyError((99999, "bucket")), IndexError("list index out of range"), FileNotFoundError("Pool/0/48/1.bucket")):
            r = self.reader(True, True); ok = r.read_chunk
            def read_chunk(b, i, fh, job=None, opener=None, err=err):
                if (b, i) == (1, 1): raise err
                return ok(b, i, fh, job, opener)
            r.read_chunk = read_chunk; out = os.path.join(d, "f"); r.extract(0, out)
            self.assertEqual(open(out, "rb").read(), b"1:0;2:11;", repr(err))
    def test_stop_is_not_retried(self):
        r = self.reader(True, True)
        def stop(b, i, fh, job=None, opener=None): raise P.Stopped("stopped")
        r.read_chunk = stop
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        with self.assertRaises(P.Stopped): r.extract(0, os.path.join(d, "f"))
    def test_healthy_table_still_fails(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        for damaged, have_map in ((False, True), (True, False)):
            with self.assertRaisesRegex(ValueError, "md5"): self.reader(damaged, have_map).extract(0, os.path.join(d, "f"))


if __name__ == "__main__":
    unittest.main()
