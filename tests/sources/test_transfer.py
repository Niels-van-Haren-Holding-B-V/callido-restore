"""Transfer profiles: one place for every rclone parallelism and resilience number, and the places that must use it."""
import unittest

from callido_restore.sources import transfer, rclone
from callido_restore.core import pool
from callido_restore.pool import indexfetch


class Profiles(unittest.TestCase):
    def test_measured_values(self):
        self.assertEqual(transfer.profile("index_files").workers, 64)       # small files: 16 → 33 MB, 64 → 84 MB, 128 → 86 MB per 25 s
        self.assertEqual(transfer.profile("restore").workers, 8)            # 52 MB files: 4 → 29 MB/s, 8 → 43 MB/s
        self.assertEqual(transfer.profile("index_slices").workers, 8)

    def test_common_resilience_everywhere(self):
        for flag in ("--retries", "--retries-sleep", "--low-level-retries", "--drive-pacer-min-sleep", "--drive-pacer-burst"):
            self.assertIn(flag, transfer.COMMON); self.assertIn(flag, transfer.DAEMON)
        self.assertIn("--tpslimit", transfer.DAEMON); self.assertNotIn("--tpslimit", transfer.COMMON)   # the cap is for the shared daemon only

    def test_index_copy_uses_the_profile(self):
        cmd = indexfetch.index_copy_cmd("rclone", "r:", 3, "/tmp/x")
        self.assertEqual(cmd[cmd.index("--transfers") + 1], "64"); self.assertEqual(cmd[cmd.index("--checkers") + 1], "32")
        for flag in transfer.COMMON[::2]: self.assertIn(flag, cmd)

    def test_slices_and_daemon_use_the_profiles(self):
        self.assertEqual(pool.SLICE_WORKERS, transfer.profile("index_slices").workers)
        self.assertEqual(rclone.THROTTLE, list(transfer.DAEMON))
