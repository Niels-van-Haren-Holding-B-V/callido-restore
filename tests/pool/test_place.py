"""pool.place: the v3 placer on a synthetic pool. Chunks have distinct ulens so byte sums tell buckets apart; records are
built directly (poolmap2.Rec) and anchors given as the scanner would (one shared position list per tag)."""
import hashlib, json, os, struct, tempfile, unittest
from pathlib import Path

from callido_restore.pool import anchor, place
from callido_restore.pool.poolmap2 import Rec


def _entries(ulens, seed):
    return [(u // 2, 0, u, hashlib.md5(bytes([(seed + i) % 256, (seed + i) // 256])).digest()) for i, u in enumerate(ulens)]


def _index_file(entries):
    return b"\0" * 64 + b"".join(struct.pack(">III", *e[:3]) + e[3] + b"\0" * 4 for e in entries)


def _rec(idx, first_id, n, size=None, tag=None, dedup=()):
    r = Rec(idx * 1000); r.idx = idx; r.n = n; r.head_new = n; r.size = size; r.tag = tag; r.tail = 1
    r.ranges = [[first_id, n]] if n else []
    for a, b in dedup: r.add_dedup(a, b)
    return r


class Pool:
    """buckets: {b: (rev, ulens)}; writes the index files and builds the Index."""
    def __init__(self, buckets, ubytes=True):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name); self.counts = {}; self.revs = {}; self.paths = {}; tot = {}
        for b, (rev, ulens) in buckets.items():
            ents = _entries(ulens, seed=b * 100); d = root / f"Pool/0/{b // 2048}"; d.mkdir(parents=True, exist_ok=True)
            p = d / f"{b % 2048}.index.{rev}"; p.write_bytes(_index_file(ents))
            self.counts[b] = len(ents); self.revs[b] = rev; self.paths[b] = str(p); tot[b] = sum(ulens)
        self.index = place.Index(self.counts, self.revs, lambda b: self.paths[b], ubytes=tot if ubytes else None, lru=2)


class Placer(unittest.TestCase):
    def run_placer(self, pool, writers, anchors):
        return place.resolve_and_place_v3(writers, anchors, pool.index, log=lambda *a: None)

    def test_in_bucket_records_are_verified_and_copies_alias(self):
        # bucket 0 (rev 2): A = 3 chunks [10, 20, 30], B = 1 chunk [40]; a later copy of A (ids far above) stores nothing
        pool = Pool({0: (2, [10, 20, 30, 40, 50])})
        A = _rec(0, 100, 3, size=60, tag="a"); B = _rec(1, 103, 1, size=40, tag="b"); A2 = _rec(2, 900, 3, size=60, tag="a")
        bad = _rec(3, 104, 1, size=51, tag="c")                                            # size does not match entry 4 (50 bytes)
        pa = [(0, 0, 3)]; anchors = {(0, 0): pa, (0, 2): pa, (0, 1): [(0, 3, 1)], (0, 3): [(0, 4, 1)]}
        owner, pl, R, flags = self.run_placer(pool, {0: [A, B, A2, bad], 1: [], 2: [], 3: []}, anchors)
        self.assertEqual(owner, {0: 0})
        self.assertEqual(pl[0][0], [(0, 0, 3)]); self.assertEqual(pl[0][1], [(0, 3, 1)]); self.assertEqual(pl[0][2], [(0, 0, 3)])
        self.assertNotIn(3, pl[0]); self.assertEqual(R["size-mismatch"], 1)
        self.assertEqual(flags["aliased"], {(0, 2): (0, 0)}); self.assertEqual(flags["guessed"], set()); self.assertEqual(R["phantom-of-tagged"], 1)
        self.assertEqual(R["verified"], 2)
        pool.tmp.cleanup()

    def test_spill_claims_the_interior_by_count_and_bytes(self):
        # writer 0: head of C in bucket 0 (2 of 4 entries), interior = one of buckets 1, 2, 3 (same count, different bytes), tail in bucket 4 where D starts
        pool = Pool({0: (2, [10, 20, 30, 40]), 1: (2, [100, 100]), 2: (2, [100, 110]), 3: (2, [100, 120]), 4: (2, [5, 6, 7])})
        C = _rec(0, 100, 5, size=30 + 40 + 100 + 110 + 5, tag="c"); D = _rec(1, 105, 2, size=13, tag="d")
        anchors = {(0, 0): [(0, 2, 2)], (0, 1): [(4, 1, 2)]}
        owner, pl, R, flags = self.run_placer(pool, {0: [C, D], 1: [], 2: [], 3: []}, anchors)
        self.assertEqual(pl[0][0], [(0, 2, 2), (2, 0, 2), (4, 0, 1)]); self.assertEqual(owner[2], 0); self.assertNotIn(1, owner); self.assertNotIn(3, owner)
        self.assertEqual(R["placed-spill"], 1); self.assertEqual(flags["guessed"], set())
        # the same with two interiors that match exactly: ambiguous, so unplaced rather than guessed
        pool2 = Pool({0: (2, [10, 20, 30, 40]), 1: (2, [100, 110]), 2: (2, [100, 110]), 4: (2, [5, 6, 7])})
        owner, pl, R, flags = self.run_placer(pool2, {0: [C, D], 1: [], 2: [], 3: []}, anchors)
        self.assertNotIn(0, pl[0]); self.assertEqual(R["spill-ambiguous"], 1); self.assertEqual(owner, {0: 0, 4: 0})
        pool.tmp.cleanup(); pool2.tmp.cleanup()

    def test_gap_records_between_anchors_are_laid_out_and_verified(self):
        # bucket 0: A (1 chunk) then an untagged record U (2 chunks) then a record H whose head is a dedup reference to A's chunk
        # (so it has a tag but was never scanned), then B anchored; sizes: U unknown (guessed), H = own 70 + referenced 10
        pool = Pool({0: (2, [10, 20, 30, 70, 40])})
        A = _rec(0, 100, 1, size=10, tag="a"); U = _rec(1, 101, 2); H = _rec(2, 103, 1, size=80, tag="h", dedup=[(100, 101)]); H.head_new = 0
        B = _rec(3, 104, 1, size=40, tag="b")
        anchors = {(0, 0): [(0, 0, 1)], (0, 3): [(0, 4, 1)]}
        owner, pl, R, flags = self.run_placer(pool, {0: [A, U, H, B], 1: [], 2: [], 3: []}, anchors)
        self.assertEqual(pl[0][1], [(0, 1, 2)]); self.assertEqual(pl[0][2], [(0, 3, 1)]); self.assertEqual(R["placed-gap"], 2)
        self.assertEqual(flags["guessed"], {(0, 1)})                                      # U has no size to check against
        # with a wrong size for H the whole gap is rejected
        H.size = 81
        owner, pl, R, flags = self.run_placer(pool, {0: [A, U, H, B], 1: [], 2: [], 3: []}, anchors)
        self.assertNotIn(1, pl[0]); self.assertNotIn(2, pl[0]); self.assertEqual(R["gap-size-mismatch"], 1)
        pool.tmp.cleanup()

    def test_copy_of_an_untagged_original_is_placed_from_its_position(self):
        # bucket 0 is from revision 2 when ids reached ~100; record X (ids 9000, rev-2 anchors say 100) matched there: its content
        # is an untagged original's; X is laid out from the position and aliased. A real rev-2 record pins the clock.
        pool = Pool({0: (2, list(range(10, 30)) + [40, 50, 60, 70]), 1: (9, [8, 9])})
        small = [_rec(i, 100 + i, 1, size=10 + i, tag=f"s{i}") for i in range(20)]         # twenty rev-2 records: the clock says ids ~100-119
        X = _rec(20, 9000, 2, size=90, tag="x"); Y = _rec(21, 9002, 1, size=8, tag="y")
        anchors = {(0, i): [(0, i, 1)] for i in range(20)}; anchors[(0, 20)] = [(0, 20, 2)]; anchors[(0, 21)] = [(1, 0, 1)]
        owner, pl, R, flags = self.run_placer(pool, {0: small + [X, Y], 1: [], 2: [], 3: []}, anchors)
        self.assertEqual(R["phantom-of-untagged"], 1); self.assertEqual(pl[0][20], [(0, 20, 2)]); self.assertEqual(flags["aliased"].get((0, 20), 1), None)      # copy of an untagged original: no source record
        self.assertEqual(pl[0][21], [(1, 0, 1)]); self.assertEqual(R["verified"], 21)
        pool.tmp.cleanup()

    def test_multi_position_small_record_takes_a_matching_position(self):
        pool = Pool({0: (2, [10, 20, 30]), 1: (2, [30, 10])})
        A = _rec(0, 100, 1, size=10, tag="a"); S = _rec(1, 101, 1, size=30, tag="s")
        anchors = {(0, 0): [(0, 0, 1)], (0, 1): [(0, 1, 1), (0, 2, 1), (1, 0, 1)]}       # three candidate entries: 20, 30, 30 bytes
        owner, pl, R, flags = self.run_placer(pool, {0: [A, S], 1: [], 2: [], 3: []}, anchors)
        self.assertIn(pl[0][1], ([(0, 2, 1)], [(1, 0, 1)])); self.assertEqual(R["placed-multi-by-size"], 1); self.assertEqual(flags["guessed"], set())
        pool.tmp.cleanup()

    def test_index_reads_totals_on_demand(self):
        pool = Pool({0: (2, [10, 20, 30]), 1: (3, [5])}, ubytes=False)
        self.assertEqual(pool.index.total(0), 60); self.assertEqual(list(pool.index.prefix(1)), [0, 5]); self.assertEqual(pool.index.seg_bytes([(0, 1, 2)]), 50)
        pool.tmp.cleanup()


class Export(unittest.TestCase):
    def test_flags_and_stats_in_the_map(self):
        out = tempfile.mkdtemp(); A = _rec(0, 100, 1, size=10, tag="a"); B = _rec(1, 101, 1, size=10, tag="a")
        writers = {0: [A, B], 1: [], 2: [], 3: []}
        m = anchor.export_map(writers, {0: 0}, {0: {0: [(0, 0, 1)], 1: [(0, 0, 1)]}, 1: {}, 2: {}, 3: {}}, {0: 1}, out, mapped_upto=1,
                              flags={"guessed": {(0, 0)}, "aliased": {(0, 1): (0, 0)}}, report={"placed": 2})
        d = json.load(open(os.path.join(out, "poolmap2.json")))
        self.assertEqual(d["guessed"], {"4": [0], "3": [], "1": [], "2": []}); self.assertEqual(d["aliased"]["4"], [[1000, 4, 0]]); self.assertEqual(d["stats"], {"placed": 2})
        m = anchor.export_map(writers, {0: 0}, {0: {1: [(0, 0, 1)]}, 1: {}, 2: {}, 3: {}}, {0: 1}, out, mapped_upto=1, flags={"guessed": set(), "aliased": {(0, 1): None}})
        d = json.load(open(os.path.join(out, "poolmap2.json"))); self.assertEqual(d["aliased"]["4"], [[1000, None, None]]); self.assertFalse(d["map_complete"])
        anchor.export_map(writers, {0: 0}, {0: {}, 1: {}, 2: {}, 3: {}}, {0: 1}, out, mapped_upto=1, complete=True)
        self.assertTrue(json.load(open(os.path.join(out, "poolmap2.json")))["map_complete"])

    def test_scan_keeps_bucket_totals(self):
        from tests.pool.test_anchor import Scan
        s = Scan("test_serial"); s.setUp()
        try:
            anchor.scan_anchors(s.src, s.out, s.listing, s.writers, log=lambda *a: None, workers=1)
            self.assertEqual(anchor.load_bucket_totals(s.out), {0: 420, 1: 330})
        finally: s.tearDown()


if __name__ == "__main__":
    unittest.main()
