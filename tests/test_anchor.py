"""hbk_anchor scanner: hits are stored per tag (shared lists), not as a per-record cross product (that cross product
is what exhausted the RAM on the full scan); resolve/place work on the shared lists."""
import hashlib, json, os, struct, tempfile, unittest
from pathlib import Path

import hbk_anchor
from hbk_poolmap2 import Rec


def _entry(ulen, seed):
    return (ulen // 2, 0, ulen, hashlib.md5(bytes([seed % 256, seed // 256])).digest())


def _index_file(entries):
    return b"\0" * 64 + b"".join(struct.pack(">III", *e[:3]) + e[3] + b"\0" * 4 for e in entries)


def _rec(idx, n, tag, tail):
    r = Rec(idx * 1000); r.n = n; r.head_new = n; r.idx = idx; r.tag = tag; r.tail = tail; r.ranges = [[idx * 1000, n]]; return r


class _Src:
    def __init__(self, local): self.local = Path(local)
    def ensure(self, rel): return self.local / rel


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        # one 2-chunk file stored once (unique tag) and one 1-chunk file stored at 3 positions (shared tag, 5 identical records)
        self.uniq = [_entry(100, 1), _entry(200, 2)]; self.dup = [_entry(50, 9)]
        bucket0 = self.uniq + self.dup + [_entry(70, 3)]
        bucket1 = [_entry(80, 4)] + self.dup + [_entry(90, 5), _entry(60, 6)] + self.dup
        (root / "Pool/0/0").mkdir(parents=True)
        (root / "Pool/0/0/0.index.2").write_bytes(_index_file(bucket0)); (root / "Pool/0/0/1.index.2").write_bytes(_index_file(bucket1))
        self.listing = [{"t": "index", "b": 0, "rev": 2, "name": "0.index.2", "size": 64 + 32 * len(bucket0)},
                        {"t": "index", "b": 1, "rev": 2, "name": "1.index.2", "size": 64 + 32 * len(bucket1)}]
        tag_u = hashlib.md5(b"".join(e[3] for e in self.uniq)).hexdigest(); tag_d = hashlib.md5(self.dup[0][3]).hexdigest()
        recs = [_rec(0, 2, tag_u, 300)] + [_rec(i, 1, tag_d, 50) for i in range(1, 6)] + [_rec(6, 1, "ff" * 16, 50)]
        self.writers = {0: recs, 1: [], 2: [], 3: []}
        self.src = _Src(root); self.out = root / "out"; self.out.mkdir()

    def tearDown(self): self.tmp.cleanup()

    def _check(self, anchors, counts):
        self.assertEqual(counts, {0: 4, 1: 5})
        self.assertEqual(anchors[(0, 0)], [(0, 0, 2)])
        dup = [(0, 2, 1), (1, 1, 1), (1, 4, 1)]
        for i in range(1, 6): self.assertEqual(anchors[(0, i)], dup)
        self.assertTrue(all(anchors[(0, i)] is anchors[(0, 1)] for i in range(2, 6)), "records of one tag share one list")
        self.assertNotIn((0, 6), anchors)
        d = json.load(open(self.out / "anchors.json"))
        self.assertEqual(sum(len(v) for v in d["positions"].values()), 4)      # linear, not 1 + 5*3
        self.assertEqual(len(d["records"]), 6)
        loaded = hbk_anchor.load_anchors(self.out)
        self.assertEqual({k: list(v) for k, v in loaded.items()}, {k: list(v) for k, v in anchors.items()})

    def test_serial(self):
        self._check(*hbk_anchor.scan_anchors(self.src, self.out, self.listing, self.writers, log=lambda *a: None, workers=1))

    def test_parallel_capped(self):
        self.assertLessEqual(hbk_anchor.MAX_WORKERS, 4)
        self._check(*hbk_anchor.scan_anchors(self.src, self.out, self.listing, self.writers, log=lambda *a: None, workers=64))

    def test_resolve_uses_neighbours(self):
        anchors, counts = hbk_anchor.scan_anchors(self.src, self.out, self.listing, self.writers, log=lambda *a: None, workers=1)
        owner, placements, report = hbk_anchor.resolve_and_place(self.writers, anchors, counts, log=lambda *a: None)
        self.assertEqual(report["unique"], 1); self.assertEqual(report["multi"], 5)
        self.assertIn(0, placements[0])
        self.assertEqual(hbk_anchor._hits_between([(0, 2, 1), (1, 1, 1), (1, 4, 1), (3, 0, 1)], 1, 1), [(1, 1, 1), (1, 4, 1)])


if __name__ == "__main__": unittest.main()


class Placement(unittest.TestCase):
    """A record must not run past the next anchor of its writer (reviewer repro on issue #15: rec0 took rec1's chunks)."""
    def setUp(self):
        self.writers = {0: [_rec(0, 4, "a" * 32, 1), _rec(1, 3, "b" * 32, 1)], 1: [], 2: [], 3: []}
        self.anchors = {(0, 0): [(101, 0, 1)], (0, 1): [(101, 2, 1)]}; self.counts = {101: 10}

    def test_place_from_anchors_stops_at_next_anchor(self):
        owner, placements, report = hbk_anchor.place_from_anchors(self.writers, self.anchors, self.counts, log=lambda *a: None)
        self.assertEqual(report["unplaced"], 1); self.assertNotIn(0, placements[0])
        self.assertEqual(placements[0][1], [(101, 2, 3)])

    def test_resolve_and_place_stops_at_next_anchor(self):
        owner, placements, report = hbk_anchor.resolve_and_place(self.writers, self.anchors, self.counts, log=lambda *a: None)
        self.assertEqual(report["unplaced"], 1); self.assertNotIn(0, placements[0])
        self.assertEqual(placements[0][1], [(101, 2, 3)])

    def test_dump_json_is_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json"); hbk_anchor.dump_json(p, {"a": 1})
            self.assertEqual(json.load(open(p)), {"a": 1}); self.assertEqual(os.listdir(d), ["x.json"])
