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
        with mock.patch.object(poolmap, "DETACHED", False), mock.patch.object(poolmap, "build_map", return_value=({1: (0, 0)}, 1)) as bp:
            self.assertEqual(poolmap.build_poolmap_detached(Src(), self.out, log=lambda m: None, max_bucket=9), ({1: (0, 0)}, 1))
        bp.assert_called_once()


class AnchorFirst(unittest.TestCase):
    """The app's mapper is the anchor-first pipeline; v1 only on request."""
    def test_pipeline_and_export(self):
        from callido_restore.pool import anchor, poolmap2
        out = tempfile.mkdtemp(); json.dump([{"b": 0, "t": "index"}], open(os.path.join(out, "listing.json"), "w"))
        calls = []
        with mock.patch.object(poolmap2, "build_writers", return_value="W"), \
             mock.patch.object(anchor, "scan_anchors", return_value=("A", {0: 5, 1: 5, 2: 5})) as scan, \
             mock.patch.object(anchor, "resolve_and_place", return_value=({0: 1, 1: 2, 2: 0}, "P", {"placed": 3})) as place, \
             mock.patch.object(anchor, "export_map", side_effect=lambda *a, **k: calls.append(k)) as export:
            res, upto = poolmap.build_poolmap_anchor(Src(), out, log=lambda m: None)
        self.assertEqual((res, upto), ({0: 1, 1: 2, 2: 0}, 3)); self.assertEqual(calls[0]["mapped_upto"], 3)
        self.assertEqual(scan.call_args[0][:2], (scan.call_args[0][0], out)); place.assert_called_once()

    def test_partial_owner_set_is_a_partial_map(self):
        from callido_restore.pool import anchor, poolmap2
        out = tempfile.mkdtemp(); json.dump([], open(os.path.join(out, "listing.json"), "w"))
        with mock.patch.object(poolmap2, "build_writers", return_value="W"), mock.patch.object(anchor, "scan_anchors", return_value=("A", {0: 5, 1: 5, 2: 5})), \
             mock.patch.object(anchor, "resolve_and_place", return_value=({0: 1}, "P", {})), mock.patch.object(anchor, "export_map"):
            self.assertEqual(poolmap.build_poolmap_anchor(Src(), out, log=lambda m: None)[1], 1)

    def test_build_map_picks_v1_only_on_request(self):
        with mock.patch.object(poolmap, "build_poolmap_anchor", return_value=({}, 0)) as a, mock.patch.object(poolmap, "build_poolmap", return_value=({}, 0)) as v1:
            poolmap.build_map(Src(), "/tmp/x"); a.assert_called_once(); v1.assert_not_called()
            with mock.patch.dict(os.environ, {"HBK_MAPPER": "v1"}): poolmap.build_map(Src(), "/tmp/x")
            v1.assert_called_once()

    def test_read_map_prefers_v2(self):
        out = tempfile.mkdtemp()
        json.dump({"buckets": {"1": [0, 0]}, "mapped_upto": 1}, open(os.path.join(out, "poolmap.json"), "w"))
        self.assertEqual(poolmap.read_map(out), ({1: (0, 0)}, 1))
        json.dump({"buckets": {"1": 2, "2": 3}, "mapped_upto": 2}, open(os.path.join(out, "poolmap2.json"), "w"))
        self.assertEqual(poolmap.read_map(out), ({1: 2, 2: 3}, 2))


class ScanWorkers(unittest.TestCase):
    """The anchor scan's workers: the module with -m from a checkout, the app binary with --scan-worker when frozen."""
    def test_worker_command(self):
        from callido_restore.pool import anchor
        argv, kw = anchor._worker_command("/t/job.pkl", "/t/out.pkl")
        self.assertEqual(argv[1:], ["-m", "callido_restore.pool.anchor", "--worker", "/t/job.pkl", "/t/out.pkl"]); self.assertIn("PYTHONPATH", kw["env"])
        with mock.patch.object(sys, "frozen", True, create=True):
            argv, kw = anchor._worker_command("/t/job.pkl", "/t/out.pkl")
        self.assertEqual(argv, [sys.executable, "--scan-worker", "/t/job.pkl", "/t/out.pkl"]); self.assertEqual(kw, {})

    def test_frozen_app_scans_with_several_workers(self):
        """scan_anchors used to force one worker when frozen; the worker count now only depends on MAX_WORKERS and the CPUs."""
        import inspect
        from callido_restore.pool import anchor
        src = inspect.getsource(anchor.scan_anchors)
        self.assertNotIn('1 if getattr(sys, "frozen"', src); self.assertIn("MAX_WORKERS", src)
