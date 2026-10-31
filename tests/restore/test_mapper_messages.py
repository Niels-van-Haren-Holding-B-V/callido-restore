"""The pool mapper's log lines become plain messages and progress for the job; stats stay in the log."""
import unittest

from callido_restore.restore.backup import mapper_phase


class MapperPhase(unittest.TestCase):
    def test_scan_lines(self):
        self.assertEqual(mapper_phase("  scanned 54/54 buckets, tags hit=17, 3s"), ("reading the index (54 of 54)", 0.95))
        self.assertEqual(mapper_phase("  scanned 12,400/36,132 buckets, tags hit=120,000, 120s")[0], "reading the index (12,400 of 36,132)")
        self.assertEqual(mapper_phase("anchor scan (x4): 277,493 tagged records, 248,475 tags, 88,213 keys, 36,132 buckets, rss 1766 MB"), ("reading the index", 0.7))

    def test_placement_lines(self):
        self.assertEqual(mapper_phase("  placing writer 2: 8,000/30,000 anchors, placed=1,000")[0], "building the map (part 3 of 4, 8,000 of 30,000)")
        self.assertEqual(mapper_phase("  resolving copies: pass 1/3, 120,000/400,000 records, 3,000 resolved"), ("resolving duplicate files (pass 1 of 3, 120,000 of 400,000)", 0.955))
        self.assertEqual(mapper_phase("phase2: {'unique': 265456, 'multi': 11742}; owners 13,656/36,132"), ("building the map", 0.955))
        self.assertEqual(mapper_phase("mapped 13656/36132 buckets (anchor-first); {'unique': 265456}"), ("writing the map", 0.995))

    def test_loading_and_stats_lines(self):
        self.assertEqual(mapper_phase("writer0: records=212,139 stored chunks=51,866,003 tagged=76,730 sized=76,730"), ("loading the record tables", 0.7))
        self.assertEqual(mapper_phase("loading the record tables of the 4 writers (a few GB, one to two minutes)…"), ("loading the record tables", 0.7))
        self.assertEqual(mapper_phase("record meta: 347,094 records (301,092 with tag) from 347,094 rows")[0], "collecting file tags")
        self.assertEqual(mapper_phase("owners from anchors: 13,656 of 36,132 buckets, conflicts=2440")[0], "building the map")   # a stats line
        self.assertEqual(mapper_phase("some plain sentence"), ("some plain sentence", None))

    def test_v1_search_line(self):
        self.assertEqual(mapper_phase("  D=1: mapped 3000/36132 buckets, backtracks=100, 3s")[0], "building the map (3000 of 36132)")
