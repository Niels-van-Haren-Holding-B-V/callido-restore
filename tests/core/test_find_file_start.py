"""Tests for the tag-based file-start locator (hbk_pool.find_file_start) on synthetic bucket index entries."""
import hashlib, struct, unittest

from callido_restore.core import pool as hbk_pool


def _entry(ulen, seed):
    md5 = hashlib.md5(bytes([seed])).digest()
    return (ulen // 2, 0, ulen, md5)   # (clen, off, ulen, md5) as bucket_index_entries returns


def _tag(entries):
    """version_list.tag for a file whose first chunks are `entries`: md5 of the md5 fields + u32 ulen sum."""
    return hashlib.md5(b"".join(e[3] for e in entries)).digest() + struct.pack(">I", sum(e[2] for e in entries))


class TagParts(unittest.TestCase):
    def test_hex_and_bytes(self):
        tag = bytes(range(16)) + struct.pack(">I", 0x1278A)
        self.assertEqual(hbk_pool.tag_parts(tag), (bytes(range(16)).hex(), 0x1278A))
        self.assertEqual(hbk_pool.tag_parts(tag.hex().upper()), (bytes(range(16)).hex(), 0x1278A))
        with self.assertRaises(ValueError): hbk_pool.tag_parts(b"short")


class FindFileStart(unittest.TestCase):
    def setUp(self):
        self.a = [_entry(100, 1), _entry(200, 2), _entry(300, 3), _entry(400, 4)]
        self.b = [_entry(50, 5), _entry(60, 6), _entry(70, 7)]
        self.ents = {7: self.a, 11: self.b}

    def test_single_chunk_file(self):
        t16, tail = hbk_pool.tag_parts(_tag(self.a[2:3]))
        self.assertEqual(hbk_pool.find_file_start(self.ents, t16, tail), (7, 2, 1))

    def test_multi_chunk_file_mid_bucket(self):
        t16, tail = hbk_pool.tag_parts(_tag(self.a[1:4]))
        self.assertEqual(hbk_pool.find_file_start(self.ents, t16, tail), (7, 1, 3))

    def test_sum_match_without_hash_match_is_rejected(self):
        t16, tail = hbk_pool.tag_parts(_tag(self.a[1:4]))
        self.assertIsNone(hbk_pool.find_file_start(self.ents, "0" * 32, tail))

    def test_run_crossing_into_next_listed_bucket(self):
        run = self.a[3:] + self.b[:2]            # 400 + 50 + 60
        t16, tail = hbk_pool.tag_parts(_tag(run))
        self.assertEqual(hbk_pool.find_file_start(self.ents, t16, tail), (7, 3, 3))
        # with the buckets in the other order the run does not exist
        self.assertIsNone(hbk_pool.find_file_start({11: self.b, 7: self.a}, t16, tail))

    def test_no_partial_sum_match(self):
        # tail that no run of consecutive entries sums to exactly
        t16, _ = hbk_pool.tag_parts(_tag(self.a[0:2]))
        self.assertIsNone(hbk_pool.find_file_start(self.ents, t16, 250))
        self.assertIsNone(hbk_pool.find_file_start(self.ents, t16, 0))

    def test_iter_yields_every_hit(self):
        dup = {1: self.a, 2: self.a}
        t16, tail = hbk_pool.tag_parts(_tag(self.a[0:1]))
        self.assertEqual(list(hbk_pool.iter_file_starts(dup, t16, tail)), [(1, 0, 1), (2, 0, 1)])


if __name__ == "__main__":
    unittest.main()
