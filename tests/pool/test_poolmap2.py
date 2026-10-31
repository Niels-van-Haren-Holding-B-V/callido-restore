"""Pool map v2: new_range, the tag-anchored mapper (try_assign / probe undo / guessed records) and PoolReader's v2 map with v1 fallback."""
import hashlib, json, os, struct, tempfile, unittest
from pathlib import Path

from callido_restore.pool import poolmap2
from callido_restore.core import pool
from callido_restore.pool.poolmap import new_range
from callido_restore.core.pool import BLOCK


def _entry(ulen, seed):
    return (ulen // 2, 0, ulen, hashlib.md5(bytes([seed % 256, seed // 256])).digest())


def _rec(idx, n, entries=None, tagged=True):
    r = poolmap2.Rec(idx * 1000); r.n = n; r.head_new = n; r.idx = idx; r.ranges = [[idx * 100000, n]]
    if entries is not None:
        r.size = sum(e[2] for e in entries)
        if tagged: r.tag = hashlib.md5(b"".join(e[3] for e in entries[:poolmap2.TAGK])).hexdigest(); r.tail = sum(e[2] for e in entries[:poolmap2.TAGK])
    return r


class _Src:
    def __init__(self, local): self.local = Path(local)
    def ensure(self, rel, job=None, progress=None, cancelled=None): return self.local / rel


class NewRange(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(new_range(10, 5, 9), (10, 15))         # continues from frontier+1
        self.assertEqual(new_range(5, 10, 9), (10, 15))         # run starts below the frontier: only the tail is new
        self.assertIsNone(new_range(5, 4, 9))                   # entirely dedup
        self.assertEqual(new_range(BLOCK * 3, 7, 9), (BLOCK * 3, BLOCK * 3 + 7))   # new block beyond the frontier
        self.assertIsNone(new_range(BLOCK * 3 + 1, 7, 9))       # a gap that is not block aligned is not new


class RecDedup(unittest.TestCase):
    def test_runs_merge_and_count(self):
        r = poolmap2.Rec(0)
        r.add_dedup(10, 13); r.add_dedup(13, 15); r.add_dedup(20, 21); r.add_dedup(30, 30)
        self.assertEqual(r.dedup, [[10, 5], [20, 1]]); self.assertEqual(r.nd, 6)
        self.assertEqual(list(r.dedup_ids()), [10, 11, 12, 13, 14, 20])

    def test_build_writers_keeps_dedup_as_runs(self):
        # set 4 = writer 0: record A stores ids 0-2; record B references 0-1 again (dedup) and stores 3-4; the other sets are empty.
        with tempfile.TemporaryDirectory() as d:
            for s in (1, 2, 3, 4):
                recs, starts, lens = ([100, 200, 200], [0, 0, 3], [3, 2, 2]) if s == 4 else ([], [], [])
                with open(os.path.join(d, f"runs_set{s}.bin"), "wb") as f:
                    f.write(struct.pack("<Q", len(recs)))
                    from array import array
                    array("q", recs).tofile(f); array("q", starts).tofile(f); array("l", lens).tofile(f)
                pool.dump_json(os.path.join(d, f"recmeta_set{s}.json"), {"200": [999, "ab" * 16, 7]} if s == 4 else {})
            W = poolmap2.build_writers(d, log=lambda *a: None)
        a, b = W[0]
        self.assertEqual((a.off, a.ranges, a.n, a.dedup, a.nd, a.head_new), (100, [[0, 3]], 3, [], 0, 3))
        self.assertEqual((b.off, b.ranges, b.n, b.dedup, b.nd, b.head_new), (200, [[3, 2]], 2, [[0, 2]], 2, 0))
        self.assertEqual((b.size, b.tag, b.tail), (999, "ab" * 16, 7))
        self.assertEqual([len(W[w]) for w in (1, 2, 3)], [0, 0, 0])


class Mapper(unittest.TestCase):
    def setUp(self):
        self.a = [_entry(100, 1), _entry(200, 2)]; self.b = [_entry(50, 3)]; self.c = [_entry(70, 4), _entry(80, 5)]
        self.ents = {5: self.a + self.b, 6: self.c}
        recs = [_rec(0, 2, self.a), _rec(1, 1, self.b, tagged=False), _rec(2, 2, self.c)]
        listing = [{"t": "index", "b": b, "rev": 2, "name": f"{b}.index.2", "size": 64 + 32 * len(e)} for b, e in self.ents.items()]
        self.mp = poolmap2.Mapper2(_Src("/nonexistent"), "/nonexistent", listing, {0: recs, 1: [], 2: [], 3: []}, log=lambda *a: None)
        self.mp.entries = lambda b: self.ents[b]

    def test_try_assign_identifies_by_tag_and_reports_guesses(self):
        state, ev, placements = self.mp.try_assign(0, 5, (0, (), None))
        self.assertEqual(state[0], 2); self.assertGreater(ev, 0)
        self.assertEqual(placements, [(0, 5, 0, 2), (1, 5, 2, 1)])
        self.assertEqual(self.mp.stats["tag-match"], 1); self.assertEqual(self.mp.stats["untagged-assumed"], 1)
        self.assertEqual(self.mp.how[0], {0: "tag", 1: "assumed"})
        out = self.mp.export({5: 0})
        self.assertEqual(out["guessed"]["4"], [1000]); self.assertEqual(out["stats"]["untagged-assumed"], 1)

    def test_size_mismatch_rejects(self):
        self.mp.W[0][0].size += 1
        r = self.mp.try_assign(0, 5, (0, (), None))
        self.assertIsNone(r[0]); self.assertIn("size mismatch", r[1])

    def test_size_check_counts_dedup_runs(self):
        # record 2 (bucket 6) also references record 0's two ids (100 + 200 bytes); its size must include them
        r2 = self.mp.W[0][2]; r2.add_dedup(0, 2); r2.size += 300
        st, ev, pl = self.mp.try_assign(0, 5, (0, (), None))          # places records 0 and 1 (marked)
        st, ev, pl = self.mp.try_assign(0, 6, st)
        self.assertEqual(pl, [(2, 6, 0, 2)]); self.assertEqual(self.mp.stats["verified"], 3)      # records 0, 1 and 2
        r2.size += 1
        self.mp.place[0].pop(2)
        r = self.mp.try_assign(0, 6, (2, (), None))
        self.assertIsNone(r[0]); self.assertIn("got 450 want 451", r[1])

    def test_probe_leaves_no_placements(self):
        r = self.mp.probe(0, 5, (0, (), None))
        self.assertIsNotNone(r[0]); self.assertEqual(self.mp.place[0], {}); self.assertEqual(self.mp.placelog, [])
        self.assertEqual(self.mp._cands(5, {w: (0, (), None) for w in range(4)})[0][0][0], 0)
        self.assertEqual(self.mp.place[0], {})

    def test_tag_matches(self):
        self.assertTrue(self.mp.tag_matches(self.ents[5], 0, self.mp.W[0][0]))
        self.assertFalse(self.mp.tag_matches(self.ents[5], 1, self.mp.W[0][0]))


class ReaderMaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.pool = self.tmp.name
        # v2: set 4 (writer 0) record at off 100 owns ids 1000..1002, stored as bucket 7 entries 0-1 then bucket 8 entry 0
        self.v2 = {"counts": {"7": 2, "8": 5}, "placements": {"4": {"100": [[7, 0, 2], [8, 0, 1]]}}, "ranges": {"4": [[1000, 3, 100]]},
                   "guessed": {"4": [100]}, "mapped_upto": 8}
        # v1: block 2 (ids 4520..6779) belongs to writer 1, one piece, stored in bucket 9
        self.v1 = {"blocks": {"1": [2]}, "pieces": {"1": {"raw_start": [0], "length": [BLOCK], "stored_start": [0]}}, "counts": {"9": 1000},
                   "buckets": {"9": [1, 0]}, "mapped_upto": 9}

    def tearDown(self): self.tmp.cleanup()

    def _reader(self):
        return pool.PoolReader(_Src(self.pool), self.pool)

    def test_v2_only(self):
        pool.dump_json(os.path.join(self.pool, "poolmap2.json"), self.v2); r = self._reader()
        self.assertTrue(r.v2); self.assertFalse(r.v1)
        self.assertEqual(r.locate2(4, 100, [1000, 1001, 1002]), [(7, 0), (7, 1), (8, 0)])
        self.assertEqual(r.locate(1001), (7, 1)); self.assertEqual(r.guessed, {4: {100}})
        with self.assertRaises(KeyError): r.locate(5000)

    def test_partial_v2_falls_back_to_v1(self):
        pool.dump_json(os.path.join(self.pool, "poolmap2.json"), self.v2); pool.dump_json(os.path.join(self.pool, "poolmap.json"), self.v1)
        r = self._reader(); self.assertTrue(r.v1 and r.v2)
        self.assertEqual(r.locate(1001), (7, 1))                 # served by v2
        self.assertEqual(r.locate(5000), (9, 5000 - 2 * BLOCK))  # not in v2, served by v1
        self.assertEqual(r.counts, {7: 2, 8: 5, 9: 1000})

    def test_dump_json_atomic(self):
        p = os.path.join(self.pool, "x.json"); pool.dump_json(p, [1]); self.assertEqual(pool.load_json(p), [1]); self.assertEqual(os.listdir(self.pool), ["x.json"])


if __name__ == "__main__": unittest.main()
