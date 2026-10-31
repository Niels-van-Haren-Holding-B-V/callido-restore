"""Parser tests for core.pool against synthetic files.

Assumptions where docs/FORMAT.md and the code disagree, or where the doc is
silent (asserted against the current code):

* ``bucket_index_entries`` skips the 64-byte header and returns
  ``(clen, off, ulen, md5)``. The crc32 of the first 28 bytes is stored
  big-endian in the fixture (as the format describes) but is not checked;
  changing it does not change the parse result. A trailing partial entry is
  ignored.
* ``file_pool_chunks`` ignores the first 0x140 bytes (a decoy chunk magic in
  the header is not a chunk) and does not check the u16 padding field.
  ``file_pool_chunks`` does not verify md5; the md5 check lives in
  ``PoolReader.read_chunk`` and is of the decompressed chunk.
* The pure-python LZ4 fallback (``lz4_decompress_py``) does not check
  ``usize``. The lz4 package does. Both decode the hand-built blocks below.
* A virtual-file entry is 14 big-endian u32s at the absolute stream offset
  (the 64-byte stream header is not skipped by ``VirtualFileIndex``; the
  caller passes the entry offset). ``set`` is word 12's high 16 bits (pointer
  B), not word 0 (pointer A). ``mtime`` is word 9 only, not the u64 in words
  9..10. Pointer B is not required to equal pointer A minus 12. Field 4
  (documented constant 0x64) is not checked.
"""
import hashlib, struct, tempfile, unittest, zlib
from unittest import mock
from pathlib import Path

from callido_restore.core import pool as cpool

# Single-token literal block (token high nibble = length, no match). Valid for
# both the lz4 package and lz4_decompress_py.
def lz4_literals(data):
    if not 0 < len(data) <= 14:
        raise ValueError("literal helper supports 1..14 bytes")
    return bytes([len(data) << 4]) + data

# Match of 4 bytes at offset 10, then 12 trailing literals. The C decoder
# rejects a shorter tail (it wants the last match to end well before the
# block end); 12 trailing literals is accepted by lz4.block and by the
# pure-python fallback.
_TRAIL = b"HELLO-WORLD!"
MATCH_BLOCK = bytes([0xA0]) + b"abcdefghij" + struct.pack("<H", 10) + bytes([0xC0]) + _TRAIL
MATCH_PLAIN = b"abcdefghijabcd" + _TRAIL


def _file_pool_bytes(chunks):
    """chunks: list of (plain, lz4 block). 0x140-byte header with a decoy magic."""
    buf = bytearray(0x140)
    buf[0:2] = b"\xe5\xbb"
    for plain, block in chunks:
        buf += struct.pack(">HIIH", 0xE5BB, len(block), len(plain), 0)
        buf += block
    return bytes(buf)


def _index_entry(clen, off, ulen, md5):
    body = struct.pack(">III", clen, off, ulen) + md5
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack(">I", crc), crc


class _LocalSource:
    def __init__(self, root):
        self.local = Path(root)
        self.extra = []
        self.remotes = []
    def has_sized_file(self, rel, size):
        p = self.local / rel
        return p.is_file() and size is not None and p.stat().st_size == int(size)
    def ensure(self, rel, job=None, progress=None, cancelled=None):
        p = self.local / rel
        if p.is_file():
            return p
        raise FileNotFoundError(rel)
    def listdir(self, rel):
        p = self.local / rel if rel else self.local
        if not p.is_dir():
            return {}
        out = {}
        for e in p.iterdir():
            out[e.name] = {"size": e.stat().st_size if e.is_file() else 0, "dir": e.is_dir(), "local": True, "remote": False}
        return out
    def find(self, rel, pattern):
        return {}


class Lz4Tests(unittest.TestCase):
    def test_handcrafted_blocks_roundtrip(self):
        literal = b"Hello, "
        self.assertEqual(cpool.lz4_decompress(lz4_literals(literal), len(literal)), literal)
        self.assertEqual(cpool.lz4_decompress_py(lz4_literals(literal), len(literal)), literal)
        self.assertEqual(cpool.lz4_decompress(MATCH_BLOCK, len(MATCH_PLAIN)), MATCH_PLAIN)
        self.assertEqual(cpool.lz4_decompress_py(MATCH_BLOCK, len(MATCH_PLAIN)), MATCH_PLAIN)

    def test_library_block_decodes_with_fallback(self):
        try:
            import lz4.block as lz4
        except ImportError:
            self.skipTest("lz4 not installed")
        raw = b"abcdefghij" * 3
        block = lz4.compress(raw, store_size=False)
        self.assertEqual(cpool.lz4_decompress(block, len(raw)), raw)
        self.assertEqual(cpool.lz4_decompress_py(block, len(raw)), raw)


class FilePoolTests(unittest.TestCase):
    def test_two_chunks_skip_header(self):
        plain1, block1 = b"Hello, ", lz4_literals(b"Hello, ")
        data = _file_pool_bytes([(plain1, block1), (MATCH_PLAIN, MATCH_BLOCK)])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "7.file.3"
            path.write_bytes(data)
            chunks = cpool.file_pool_chunks(path)
        self.assertEqual(chunks, [plain1, MATCH_PLAIN])

    def test_bad_magic_raises(self):
        data = bytearray(0x140) + struct.pack(">HIIH", 0x0001, 1, 1, 0) + b"\x10"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "1.file.1"
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                cpool.file_pool_chunks(path)


class BucketIndexTests(unittest.TestCase):
    def _write(self, directory):
        b1, b2 = MATCH_BLOCK, lz4_literals(b"Hello, ")
        bucket = b1 + b2
        md1, md2 = hashlib.md5(MATCH_PLAIN).digest(), hashlib.md5(b"Hello, ").digest()
        e1, crc1 = _index_entry(len(b1), 0, len(MATCH_PLAIN), md1)
        e2, crc2 = _index_entry(len(b2), len(b1), len(b"Hello, "), md2)
        index = b"\xab" * 64 + e1 + e2 + b"\xff" * 10  # short tail is not an entry
        root = Path(directory)
        (root / "0.bucket.1").write_bytes(bucket)
        (root / "0.index.1").write_bytes(index)
        return md1, md2, crc1, crc2, index

    def test_header_and_entries(self):
        with tempfile.TemporaryDirectory() as td:
            md1, md2, crc1, crc2, index = self._write(td)
            entries = cpool.bucket_index_entries(Path(td) / "0.index.1")
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0][0], len(MATCH_BLOCK))
            self.assertEqual(entries[0][1], 0)
            self.assertEqual(entries[0][2], len(MATCH_PLAIN))
            self.assertEqual(entries[0][3], md1)
            self.assertEqual(entries[1][3], md2)
            # crc sits in the file and is not part of the returned tuple
            self.assertEqual(index[64 + 28:64 + 32], struct.pack(">I", crc1))
            self.assertEqual(len(entries[0]), 4)
            flipped = bytearray(index)
            flipped[64 + 28] ^= 0xFF
            flipped_path = Path(td) / "flipped.index"
            flipped_path.write_bytes(flipped)
            self.assertEqual(cpool.bucket_index_entries(flipped_path), entries)
            self.assertNotEqual(crc1, crc2)

    def test_read_chunk_checks_md5_of_decompressed_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hbk"
            pool = Path(td) / "pool"
            (root / "Pool" / "0" / "0").mkdir(parents=True)
            pool.mkdir()
            self._write(root / "Pool" / "0" / "0")
            idx = root / "Pool" / "0" / "0" / "0.index.1"
            bucket = root / "Pool" / "0" / "0" / "0.bucket.1"
            listing = [{"b": 0, "t": "index", "rev": 1, "name": "0.index.1", "size": idx.stat().st_size},
                       {"b": 0, "t": "bucket", "rev": 1, "name": "0.bucket.1", "size": bucket.stat().st_size}]
            (pool / "listing.json").write_text(__import__("json").dumps(listing))
            reader = cpool.PoolReader(_LocalSource(root), str(pool))
            fh = {}
            try:
                self.assertEqual(reader.read_chunk(0, 0, fh), MATCH_PLAIN)
                self.assertEqual(reader.read_chunk(0, 1, fh), b"Hello, ")
                # md5 of the compressed bytes must not be accepted
                entries = cpool.bucket_index_entries(idx)
                bad = bytearray(idx.read_bytes())
                bad[64 + 12:64 + 28] = hashlib.md5(MATCH_BLOCK).digest()
                idx.write_bytes(bad)
                reader.idx_cache.clear()
                with self.assertRaises(ValueError) as cm:
                    reader.read_chunk(0, 0, fh)
                self.assertIn("md5", str(cm.exception))
                self.assertEqual(entries[0][3], hashlib.md5(MATCH_PLAIN).digest())
            finally:
                for f in fh.values():
                    f.close()


class VirtualFileIndexTests(unittest.TestCase):
    def _pack(self, set_a, list_off, set_b, rec_off, mtime_hi=0x11111111, mtime_lo=0x22222222):
        def ptr(set_no, off):
            return ((set_no & 0xFFFF) << 16) | ((off >> 32) & 0xFFFF), off & 0xFFFFFFFF
        words = [0] * 14
        words[0], words[1] = ptr(set_a, list_off)
        words[2] = 1234
        words[3] = 0x402
        words[4] = 0x64
        words[6], words[7] = 0, 1700000000
        words[9], words[10] = mtime_hi, mtime_lo
        words[11] = 0xAABBCCDD
        words[12], words[13] = ptr(set_b, rec_off)
        return struct.pack(">14I", *words)

    def test_entry_fields_follow_the_code(self):
        # Pointer B is not A-12, and the two set fields differ, on purpose.
        list_off, rec_off = 0x10000000030, 0x55
        blob = self._pack(set_a=1, list_off=list_off, set_b=4, rec_off=rec_off)
        self.assertEqual(len(blob), 56)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "Config" / "virtual_file.index"
            d.mkdir(parents=True)
            # lower revision must lose; header bytes must not be parsed as the entry
            (d / "0.idx.1").write_bytes(b"\x00" * 120)
            data = bytearray(b"\xee" * 64 + blob)
            (d / "0.idx.9").write_bytes(data)
            entry = cpool.VirtualFileIndex(_LocalSource(root)).entry(64)
        self.assertEqual(entry["set"], 4)          # word 12, not word 0
        self.assertEqual(entry["list_off"], list_off)
        self.assertEqual(entry["rec_off"], rec_off)
        self.assertNotEqual(rec_off, list_off - 12)
        self.assertEqual(entry["version"], 1234)
        self.assertEqual(entry["flags"], 0x402)
        self.assertEqual(entry["mtime"], 0x11111111)  # word 9 only
        self.assertNotEqual(entry["mtime"], 0x22222222)
        self.assertEqual(entry["crc"], 0xAABBCCDD)
        self.assertEqual(entry["raw"][4], 0x64)
        self.assertEqual(entry["raw"][10], 0x22222222)

    def test_entry_may_straddle_slices(self):
        list_off = 0x200
        rec_off = list_off - 12  # the relationship the format describes
        blob = self._pack(set_a=4, list_off=list_off, set_b=4, rec_off=rec_off, mtime_hi=1700001111, mtime_lo=0)
        off = cpool.SLICE - 20
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "Config" / "virtual_file.index"
            d.mkdir(parents=True)
            with open(d / "0.idx.1", "wb") as f:
                f.seek(off)
                f.write(blob[:20])
            (d / "1.idx.1").write_bytes(blob[20:])
            entry = cpool.VirtualFileIndex(_LocalSource(root)).entry(off)
        self.assertEqual(entry["set"], 4)
        self.assertEqual(entry["list_off"], list_off)
        self.assertEqual(entry["rec_off"], rec_off)
        self.assertEqual(entry["mtime"], 1700001111)
        self.assertEqual(bytes(struct.pack(">14I", *entry["raw"])), blob)


class PoolStatusTests(unittest.TestCase):
    def test_index_file_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hbk"
            pool = Path(td) / "pool"
            (root / "Pool" / "0" / "0").mkdir(parents=True)
            pool.mkdir()
            (root / "Pool" / "0" / "0" / "0.index.1").write_bytes(b"x" * 10)
            (root / "Pool" / "0" / "0" / "2.index.1").write_bytes(b"bad")  # wrong size: not counted
            listing = [
                {"b": 0, "t": "index", "rev": 1, "name": "0.index.1", "size": 10},
                {"b": 1, "t": "index", "rev": 1, "name": "1.index.1", "size": 10},
                {"b": 2, "t": "index", "rev": 1, "name": "2.index.1", "size": 10},
                {"b": 0, "t": "bucket", "rev": 1, "name": "0.bucket.1", "size": 1},
            ]
            (pool / "listing.json").write_text(__import__("json").dumps(listing))
            st = cpool.PoolReader(_LocalSource(root), str(pool)).status()
        self.assertEqual(st["index_files"], 3)
        self.assertEqual(st["indexes_local"], 1)
        self.assertEqual(st["total_buckets"], 3)
        self.assertEqual((st["index_bytes"], st["index_bytes_local"]), (30, 10))   # all index files / the one present at its size
        self.assertFalse(st["map"])



class CancelDuringParse(unittest.TestCase):
    """The Stop button of the pool job is honoured inside the record parsing, not only between phases."""
    def test_parse_record_stream_raises_cancelled(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "slice"
            rec = struct.pack(">II", cpool.REC_MAGIC, 16) + struct.pack(">QQ", 64 + 29 * 5, (0x74000000 << 32) | 1)
            p.write_bytes(b"\0" * 64 + rec * 12000)
            seen = []
            with self.assertRaises(cpool.Cancelled):
                cpool.parse_record_stream([str(p)], lambda off, ids: seen.append(off), cancelled=lambda: True)
            self.assertLess(len(seen), 12000)
            fr = []
            cpool.parse_record_stream([str(p)], lambda off, ids: None, cancelled=lambda: False, progress=fr.append)   # completes
            self.assertTrue(fr and fr == sorted(fr) and 0 < fr[0] and fr[-1] <= 1.0)           # progress rises through the parse

    def test_build_runs_reports_slice_download_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hbk"
            for s in (1, 2, 3, 4):
                d = root / "Config" / f"file_chunk{s}.index"; d.mkdir(parents=True)
                (d / "0.idx.1").write_bytes(b"\0" * 100); (d / "1.idx.1").write_bytes(b"\0" * 50)
            reports = []
            cpool.build_runs(_LocalSource(root), str(Path(td) / "pool"), log=lambda *a: None, report=lambda m, d, t: reports.append((m, d, t)))
            per_set = [r for r in reports if r[0].startswith("downloading index set 1/4")]
            done = [d for _, d, _ in per_set]
            # Slices finish in any order with several workers (transfer.profile("index_slices")): the finished-bytes
            # reports start at 0, end at the set's size, and only ever grow.
            self.assertEqual((done[0], done[-1]), (0, 150)); self.assertEqual(done, sorted(done)); self.assertTrue(all(t == 150 for _, _, t in per_set))
            self.assertIn("parsing index set 1/4", [r[0] for r in reports])

    def test_build_runs_stops_before_the_first_set(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "pool"
            self.assertIsNone(cpool.build_runs(_LocalSource(Path(td)), str(out), log=lambda *a: None, cancelled=lambda: True))
            self.assertFalse((out / "blocks.json").exists())


if __name__ == "__main__":
    unittest.main()

