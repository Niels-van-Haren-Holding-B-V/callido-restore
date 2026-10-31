"""The pool mapper as a child process: log lines become progress, the result is read back, Stop ends the child."""
import json, os, sys, tempfile, textwrap, time, unittest
from unittest import mock

from callido_restore.pool import poolmap


class Src:
    id = "demo"


CHILD = textwrap.dedent("""
    import json, os, sys, time
    out = sys.argv[1]
    for i in range(3):
        print(f"  D=1: mapped {i * 1000}/3000 buckets, backtracks=0, {i}s", flush=True); time.sleep(0.05)
    json.dump({"buckets": {"7": [1, 4096], "8": [2, 0]}, "mapped_upto": 3000}, open(os.path.join(out, "poolmap.json"), "w"))
""")
SLOW = "import time\nwhile True: time.sleep(0.1)\n"


class Detached(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()

    def _cmd(self, script):
        return lambda sid, outdir, mb: ([sys.executable, "-c", script, outdir], None)

    def test_lines_flow_and_the_result_is_read_back(self):
        lines = []
        with mock.patch.object(poolmap, "_mapper_command", self._cmd(CHILD)):
            res, upto = poolmap.build_poolmap_detached(Src(), self.out, log=lines.append)
        self.assertEqual(res, {7: (1, 4096), 8: (2, 0)}); self.assertEqual(upto, 3000)
        self.assertTrue(any("mapped 2000/3000" in l for l in lines))

    def test_cancel_terminates_the_child(self):
        t0 = time.time()
        with mock.patch.object(poolmap, "_mapper_command", self._cmd(SLOW)):
            res, upto = poolmap.build_poolmap_detached(Src(), self.out, log=lambda m: None, cancelled=lambda: time.time() - t0 > 0.5)
        self.assertEqual((res, upto), ({}, 0)); self.assertLess(time.time() - t0, 10)

    def test_failure_carries_the_childs_last_lines(self):
        with mock.patch.object(poolmap, "_mapper_command", self._cmd("import sys; print('loading'); print('boom: no runs'); sys.exit(3)")):
            with self.assertRaises(RuntimeError) as cm: poolmap.build_poolmap_detached(Src(), self.out, log=lambda m: None)
        self.assertIn("boom: no runs", str(cm.exception))

    def test_command_lines(self):
        cmd, env = poolmap._mapper_command("demo", "/tmp/out", None)
        self.assertEqual(cmd[-4:], ["--mapper", "demo", "/tmp/out", "-"]); self.assertIn("callido_restore", cmd); self.assertIn("PYTHONPATH", env)
        with mock.patch.object(sys, "frozen", True, create=True):
            cmd, env = poolmap._mapper_command("demo", "/tmp/out", 500)
        self.assertEqual(cmd, [sys.executable, "--mapper", "demo", "/tmp/out", "500"]); self.assertIsNone(env)

    def test_in_process_when_not_detached(self):
        with mock.patch.object(poolmap, "DETACHED", False), mock.patch.object(poolmap, "build_poolmap", return_value=({1: (0, 0)}, 1)) as bp:
            self.assertEqual(poolmap.build_poolmap_detached(Src(), self.out, log=lambda m: None, max_bucket=9), ({1: (0, 0)}, 1))
        bp.assert_called_once()
