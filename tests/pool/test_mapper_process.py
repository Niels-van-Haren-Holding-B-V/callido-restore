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
        """v3 placer by default: scan -> place.resolve_and_place_v3 (with an Index over the listing) -> export with flags and stats."""
        from callido_restore.pool import anchor, poolmap2, place
        out = tempfile.mkdtemp(); json.dump([{"b": 0, "t": "index", "name": "0.index.2", "rev": 2, "size": 64 + 32 * 5}], open(os.path.join(out, "listing.json"), "w"))
        json.dump({"positions": {}, "records": {}, "ubytes": {"0": 1234}}, open(os.path.join(out, "anchors.json"), "w"))
        calls = []; flags = {"guessed": set(), "aliased": set()}
        with mock.patch.object(poolmap2, "build_writers", return_value="W"), \
             mock.patch.object(anchor, "scan_anchors", return_value=("A", {0: 5, 1: 5, 2: 5})) as scan, \
             mock.patch.object(place, "resolve_and_place_v3", return_value=({0: 1, 1: 2, 2: 0}, "P", {"placed": 3}, flags)) as placer, \
             mock.patch.object(anchor, "export_map", side_effect=lambda *a, **k: calls.append(k)) as export:
            res, upto = poolmap.build_poolmap_anchor(Src(), out, log=lambda m: None)
        self.assertEqual((res, upto), ({0: 1, 1: 2, 2: 0}, 3)); self.assertEqual(calls[0]["mapped_upto"], 3)
        self.assertIs(calls[0]["flags"], flags); self.assertEqual(calls[0]["report"], {"placed": 3})
        self.assertEqual(scan.call_args[0][:2], (scan.call_args[0][0], out)); placer.assert_called_once()
        index = placer.call_args[0][2]
        self.assertEqual((index.counts, index.revs, index.ubytes), ({0: 5, 1: 5, 2: 5}, {0: 2}, {0: 1234}))   # totals from anchors.json

    def test_v2_placer_on_request(self):
        from callido_restore.pool import anchor, poolmap2
        out = tempfile.mkdtemp(); json.dump([], open(os.path.join(out, "listing.json"), "w"))
        with mock.patch.object(poolmap2, "build_writers", return_value="W"), mock.patch.object(anchor, "scan_anchors", return_value=("A", {0: 5, 1: 5, 2: 5})), \
             mock.patch.object(anchor, "resolve_and_place", return_value=({0: 1}, "P", {})) as v2, mock.patch.object(anchor, "export_map"), \
             mock.patch.dict(os.environ, {"HBK_PLACER": "v2"}):
            self.assertEqual(poolmap.build_poolmap_anchor(Src(), out, log=lambda m: None)[1], 1)       # partial owner set: partial map
        v2.assert_called_once()

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


@unittest.skipIf(os.name == "nt", "POSIX process groups; the Windows stop (taskkill /T) is in tests/test_windows_compat.py")
class StopKillsTheGroup(unittest.TestCase):
    def test_child_runs_in_its_own_session_and_cancel_kills_the_group(self):
        seen = {}
        real_popen = poolmap.subprocess.Popen
        def popen(cmd, **kw):
            seen.update(kw); return real_popen(cmd, **kw)
        t0 = time.time()
        with mock.patch.object(poolmap, "_mapper_command", lambda sid, outdir, mb: ([sys.executable, "-c", SLOW], None)), \
             mock.patch.object(poolmap.subprocess, "Popen", popen), mock.patch.object(poolmap.os, "killpg", wraps=poolmap.os.killpg) as killpg:
            res = poolmap.build_poolmap_detached(Src(), tempfile.mkdtemp(), log=lambda m: None, cancelled=lambda: time.time() - t0 > 0.5)
        self.assertEqual(res, ({}, 0)); self.assertTrue(seen.get("start_new_session")); killpg.assert_called(); self.assertEqual(poolmap.RUNNING, set())

    def test_orphan_helpers_of_a_dead_app_are_ended(self):
        from callido_restore.about import APP_NAME
        exe = f"/Applications/{APP_NAME}.app/Contents/MacOS/{APP_NAME}"
        rows = [(500, 1, f"{exe} --scan-worker /t/job0.pkl /t/out0.pkl"), (501, 1, f"{exe} --mapper demo /t/pool -"),
                (502, 300, f"{exe} --scan-worker /t/job1.pkl /t/out1.pkl"),        # parent alive: a running app's worker
                (503, 1, exe), (504, 1, "/usr/bin/python3 --mapper x")]           # the app itself; not our binary
        with mock.patch.object(poolmap, "_ps_commands", return_value=rows), mock.patch.object(poolmap.os, "kill") as kill:
            self.assertEqual(poolmap.kill_orphan_helpers(), 2)
        self.assertEqual(sorted(c[0][0] for c in kill.call_args_list), [500, 501])
