"""T8 rules: a restore never writes over or deletes what is at the destination.

* A destination inside the backup source is refused by ``Source.contains_dest`` (tests in ``test_dest_check.py``).
* ``list_destination`` raises when rclone cannot list the destination; a listing failure is never treated as "empty".
* ``Backup.restore`` has no overwrite parameter any more.
* ``place_file`` never replaces what is at the destination, even when it appeared after the existence check.
* The restore loop itself (local and remote target) leaves an existing file with a different size or mtime unchanged and
  counts it as skipped.
"""
import inspect, json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

from callido_restore import paths
from callido_restore.restore.backup import Backup
from callido_restore.restore import backup as backup_mod
from callido_restore.restore.destination import list_destination, place_file


class ListDestination(unittest.TestCase):
    def test_failure_raises_instead_of_empty(self):
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="directory not found")
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as cm:
                list_destination("nas:share/missing")
        self.assertIn("directory not found", str(cm.exception))

    def test_success_returns_paths(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps([{"Path": "homes/a/x.txt", "Size": 3}, {"Path": "b.jpg", "Size": 9}]), stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok):
            self.assertEqual(list_destination("nas:share/restore"), {"homes/a/x.txt", "b.jpg"})

    def test_empty_listing_is_empty_set(self):
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok):
            self.assertEqual(list_destination("nas:share/new"), set())


class PlaceFile(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        self.tmp = os.path.join(self.d, "stage", "f"); os.makedirs(os.path.dirname(self.tmp))
        Path(self.tmp).write_bytes(b"NEW"); os.utime(self.tmp, (1700000000, 1700000000))
        self.out = os.path.join(self.d, "share", "sub", "f")

    def test_places_new_file_with_mtime(self):
        self.assertTrue(place_file(self.tmp, self.out))
        self.assertEqual(Path(self.out).read_bytes(), b"NEW")
        self.assertEqual(int(os.stat(self.out).st_mtime), 1700000000)
        self.assertFalse(os.path.exists(self.tmp))

    def test_existing_file_is_kept(self):
        os.makedirs(os.path.dirname(self.out)); Path(self.out).write_bytes(b"OLD-LONGER"); os.utime(self.out, (1000, 1000))
        self.assertFalse(place_file(self.tmp, self.out))
        self.assertEqual(Path(self.out).read_bytes(), b"OLD-LONGER")
        self.assertEqual(int(os.stat(self.out).st_mtime), 1000)
        self.assertFalse(os.path.exists(self.tmp))

    def test_existing_symlink_even_dangling_is_kept(self):
        os.makedirs(os.path.dirname(self.out)); os.symlink("/nonexistent/target", self.out)
        self.assertFalse(place_file(self.tmp, self.out))
        self.assertTrue(os.path.islink(self.out)); self.assertEqual(os.readlink(self.out), "/nonexistent/target")

    def test_without_hard_links_places_and_keeps_existing(self):
        with mock.patch.object(os, "link", side_effect=PermissionError("no hard links")):
            self.assertTrue(place_file(self.tmp, self.out))
            self.assertEqual(Path(self.out).read_bytes(), b"NEW")
            Path(self.tmp).write_bytes(b"NEW2")
            self.assertFalse(place_file(self.tmp, self.out))
            self.assertEqual(Path(self.out).read_bytes(), b"NEW")
            self.assertFalse(os.path.exists(self.tmp))


class _FakePool:
    """Writes ``size`` bytes of the file's letter and sets the mtime, like ``PoolReader.extract``; ``appear`` lists the output
    names for which a foreign file shows up at the final destination while the extract runs (the lexists/extract race)."""
    def __init__(self, dest, appear=()):
        self.dest, self.appear, self.calls = dest, set(appear), []
    def extract(self, off, out, size=None, mtime=None, job=None, fh_cache=None):
        self.calls.append(os.path.basename(out))
        Path(out).write_bytes(os.path.basename(out)[0].encode() * size); os.utime(out, (mtime, mtime))
        if os.path.basename(out) in self.appear:
            final = os.path.join(self.dest, "share", os.path.basename(out))     # every test file lives in share/<name>
            os.makedirs(os.path.dirname(final), exist_ok=True); Path(final).write_bytes(b"INTRUDER")
        return size


class _Backup(Backup):
    """Real ``restore``; the pool and the file list come from the test (``pool`` is a read-only property on Backup)."""
    pool = property(lambda self: self._fake_pool)


def _fake_backup(dest, files, appear=()):
    b = _Backup.__new__(_Backup)
    b.src = mock.Mock(id="testsrc"); b._fake_pool = _FakePool(dest, appear)
    b.restore_plan = lambda items, skip_meta=True: [{"share": "share", "path": n, "off": -1, "size": sz, "mtime": mt} for n, sz, mt in files]
    return b


class RestoreLoopLocal(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        self.dest = os.path.join(self.d, "dest"); os.makedirs(os.path.join(self.dest, "share"))
        p = mock.patch.object(paths, "JOBS_DIR", Path(self.d) / "jobs"); p.start(); self.addCleanup(p.stop)

    def run_restore(self, files, appear=()):
        b = _fake_backup(self.dest, files, appear)
        job = {"id": "t1", "label": "restore", "started": 0.0}
        return b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], self.dest, job), job, b

    def test_existing_file_with_other_size_and_mtime_is_skipped(self):
        old = os.path.join(self.dest, "share", "a.txt"); Path(old).write_bytes(b"OLD"); os.utime(old, (1000, 1000))
        res, job, b = self.run_restore([("a.txt", 5, 1700000000), ("b.txt", 4, 1700000001)])
        self.assertEqual(Path(old).read_bytes(), b"OLD"); self.assertEqual(int(os.stat(old).st_mtime), 1000)
        self.assertEqual(Path(self.dest, "share", "b.txt").read_bytes(), b"bbbb")
        self.assertEqual(int(os.stat(os.path.join(self.dest, "share", "b.txt")).st_mtime), 1700000001)
        self.assertEqual((res["files"], res["restored"], res["skipped"], res["errors"]), (2, 1, 1, 0))      # restored = files written
        self.assertEqual(job["skipped"], 1); self.assertEqual(b.pool.calls, ["b.txt"])
        self.assertEqual(sorted(os.listdir(self.dest)), ["share"])            # staging folder removed, nothing else added

    def test_resume_removes_only_this_jobs_leftover_staging_folder(self):
        mine = os.path.join(self.dest, ".hbk-restore-t1-abc"); other = os.path.join(self.dest, ".hbk-restore-zz-abc")
        for d in (mine, other): os.makedirs(d); Path(d, "share", "x").parent.mkdir(); Path(d, "share", "x").write_bytes(b"part")
        self.run_restore([("b.txt", 4, 1700000001)])
        self.assertFalse(os.path.exists(mine)); self.assertTrue(os.path.exists(os.path.join(other, "share", "x")))
        self.assertEqual(sorted(os.listdir(self.dest)), [".hbk-restore-zz-abc", "share"])

    def test_file_appearing_during_extract_is_kept(self):
        res, job, b = self.run_restore([("c.txt", 3, 1700000000)], appear={"c.txt"})
        self.assertEqual(Path(self.dest, "share", "c.txt").read_bytes(), b"INTRUDER")
        self.assertEqual((res["restored"], res["skipped"], res["errors"]), (0, 1, 0))
        self.assertEqual(sorted(os.listdir(self.dest)), ["share"])


class RestoreVerification(RestoreLoopLocal):
    """Every restored file is counted by how the map established its placement: verified, identical_copy or unverified (listed
    with a reason, capped like errors); skipped files are not counted; the counts go to the result and the persisted job."""
    def test_statuses_counted_and_listed(self):
        b = _fake_backup(self.dest, [("v.txt", 3, 1700000000), ("c.txt", 2, 1700000001), ("u.txt", 4, 1700000002), ("p.txt", 1, 1700000003), ("s.txt", 1, 1700000004)])
        files = b.restore_plan(None); files[0]["off"] = 10; files[1]["off"] = 20; files[2]["off"] = 30; files[4]["off"] = 50     # p.txt stays in the file pool (off < 0)
        b.restore_plan = lambda items, skip_meta=True: [dict(f) for f in files]
        b._fake_pool.verification = lambda off: {10: ("verified", ""), 20: ("identical_copy", ""), 30: ("unverified", "placed without a size check"), 50: ("verified", "")}[off]
        Path(self.dest, "share", "s.txt").write_bytes(b"OLD")                          # skipped: no status
        job = {"id": "t9", "label": "restore", "started": 0.0}
        res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], self.dest, job)
        self.assertEqual((res["restored"], res["skipped"], res["verified"], res["identical_copy"], res["unverified"]), (4, 1, 2, 1, 1))
        self.assertEqual(job["unverified"], [{"path": "share/u.txt", "reason": "placed without a size check"}])
        saved = json.loads((Path(self.d) / "jobs" / "t9.json").read_text())
        self.assertEqual((saved["verified"], saved["identical_copy"], saved["unverified"]), (2, 1, job["unverified"]))

    def test_pool_without_verification_counts_unverified(self):
        b = _fake_backup(self.dest, [("a.txt", 5, 1700000000), ("f.txt", 1, 1700000001)])      # _FakePool has no verification(): nothing is claimed
        files = b.restore_plan(None); files[0]["off"] = 7; b.restore_plan = lambda items, skip_meta=True: [dict(f) for f in files]
        job = {"id": "t8", "label": "restore", "started": 0.0}
        res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], self.dest, job)
        self.assertEqual((res["verified"], res["identical_copy"], res["unverified"]), (1, 0, 1)); self.assertEqual(job["unverified"][0], {"path": "share/a.txt", "reason": "the pool map gives no status"})

    def test_status_check_failure_is_reported_not_hidden(self):
        b = _fake_backup(self.dest, [("a.txt", 5, 1700000000)]); files = b.restore_plan(None); files[0]["off"] = 7
        b.restore_plan = lambda items, skip_meta=True: [dict(f) for f in files]
        def boom(off): raise KeyError("record 7 not placed")
        b._fake_pool.verification = boom
        res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], self.dest, {"id": "t7", "label": "restore", "started": 0.0})
        self.assertEqual((res["restored"], res["unverified"], res["errors"]), (1, 1, 0))


class RestoreLoopTwoLocalRoots(unittest.TestCase):
    """H8 on the restore-loop path: two shares to two local roots. Each root gets its own staging folder (same filesystem for
    place_file), files land at <root>/<path in share> with no wrapper folder, existing files are kept per root, and only this
    job's leftover staging folders are removed, in every root."""
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        self.ra = os.path.join(self.d, "ShareA"); self.rb = os.path.join(self.d, "Docs"); os.makedirs(self.ra); os.makedirs(self.rb)
        p = mock.patch.object(paths, "JOBS_DIR", Path(self.d) / "jobs"); p.start(); self.addCleanup(p.stop)
        self.items = [{"share": "A", "ver": 1, "path": "", "is_dir": True, "dest": self.ra}, {"share": "B", "ver": 1, "path": "docs", "is_dir": True, "dest": self.rb}]
        self.plan = [{"share": "A", "path": "x.txt", "off": -1, "size": 3, "mtime": 1700000000, "item": 0},
                     {"share": "A", "path": "sub/y.txt", "off": -1, "size": 4, "mtime": 1700000001, "item": 0},
                     {"share": "B", "path": "docs/z.txt", "off": -1, "size": 5, "mtime": 1700000002, "item": 1},
                     {"share": "B", "path": "docs/deep/w.txt", "off": -1, "size": 6, "mtime": 1700000003, "item": 1}]

    def run_restore(self, job_id="t2"):
        b = _Backup.__new__(_Backup); b.src = mock.Mock(id="testsrc"); b._fake_pool = _FakePool(self.d)
        b.restore_plan = lambda items, skip_meta=True: [dict(f) for f in self.plan]
        job = {"id": job_id, "label": "restore", "started": 0.0}
        return b.restore(self.items, "", job), job, b

    def test_files_land_in_their_root_without_wrapper(self):
        res, job, b = self.run_restore()
        self.assertEqual((res["files"], res["restored"], res["skipped"], res["errors"]), (4, 4, 0, 0))
        self.assertEqual(Path(self.ra, "x.txt").read_bytes(), b"xxx"); self.assertEqual(Path(self.ra, "sub", "y.txt").read_bytes(), b"yyyy")
        self.assertEqual(Path(self.rb, "docs", "z.txt").read_bytes(), b"zzzzz"); self.assertEqual(Path(self.rb, "docs", "deep", "w.txt").read_bytes(), b"wwwwww")
        self.assertEqual(int(os.stat(os.path.join(self.rb, "docs", "deep", "w.txt")).st_mtime), 1700000003)
        self.assertFalse(os.path.exists(os.path.join(self.ra, "A"))); self.assertFalse(os.path.exists(os.path.join(self.rb, "B")))
        self.assertEqual(sorted(os.listdir(self.ra)), ["sub", "x.txt"]); self.assertEqual(sorted(os.listdir(self.rb)), ["docs"])   # staging gone in both roots
        staged = [c for c in b.pool.calls]; self.assertEqual(sorted(staged), ["w.txt", "x.txt", "y.txt", "z.txt"])

    def test_existing_file_kept_per_root_and_own_leftovers_removed_everywhere(self):
        old = os.path.join(self.rb, "docs", "z.txt"); os.makedirs(os.path.dirname(old)); Path(old).write_bytes(b"OLD"); os.utime(old, (1000, 1000))
        for root in (self.ra, self.rb):
            for name in (".hbk-restore-t2-left", ".hbk-restore-other-left"):
                os.makedirs(os.path.join(root, name)); Path(root, name, "part").write_bytes(b"p")
        res, job, b = self.run_restore()
        self.assertEqual(Path(old).read_bytes(), b"OLD"); self.assertEqual(int(os.stat(old).st_mtime), 1000)
        self.assertEqual((res["restored"], res["skipped"]), (3, 1)); self.assertEqual(sorted(b.pool.calls), ["w.txt", "x.txt", "y.txt"])
        for root in (self.ra, self.rb):
            self.assertFalse(os.path.exists(os.path.join(root, ".hbk-restore-t2-left")))
            self.assertTrue(os.path.exists(os.path.join(root, ".hbk-restore-other-left", "part")))
        self.assertEqual(sorted(os.listdir(self.ra)), [".hbk-restore-other-left", "sub", "x.txt"])

    def test_staging_folder_sits_inside_each_root(self):
        seen = {}
        class Pool(_FakePool):
            def extract(self2, off, out, **kw):
                seen[os.path.basename(out)] = out; return super().extract(off, out, **kw)
        b = _Backup.__new__(_Backup); b.src = mock.Mock(id="testsrc"); b._fake_pool = Pool(self.d)
        b.restore_plan = lambda items, skip_meta=True: [dict(f) for f in self.plan]
        b.restore(self.items, "", {"id": "t3", "label": "restore", "started": 0.0})
        self.assertTrue(seen["x.txt"].startswith(os.path.join(self.ra, ".hbk-restore-t3-")), seen["x.txt"])
        self.assertTrue(seen["z.txt"].startswith(os.path.join(self.rb, ".hbk-restore-t3-")), seen["z.txt"])


class RestoreLoopRemote(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        p = mock.patch.object(paths, "JOBS_DIR", Path(self.d) / "jobs"); p.start(); self.addCleanup(p.stop)
        self.calls = []
        def run(cmd, **kw):
            self.calls.append(cmd)
            if cmd[1] == "lsjson":
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([{"Path": "share/a.txt", "Size": 3}]), stderr="")
            raise AssertionError(cmd)
        def upload(cmd, cancelled=None, **kw):
            self.calls.append(cmd)
            staged, target = cmd[2], cmd[3]
            if target in self.stall_once: self.stall_once.discard(target); return backup_mod.UPLOAD_STALLED, ""
            if not target.endswith("/c.txt") and target not in self.partial: os.unlink(staged)   # moved; c.txt "appeared" after the listing
            return 0, ""
        self.stall_once = set(); self.partial = set()
        p = mock.patch.object(subprocess, "run", side_effect=run); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(backup_mod, "upload_watched", side_effect=upload); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(backup_mod.time, "sleep", lambda s: None); p.start(); self.addCleanup(p.stop)

    def test_listed_and_late_files_are_skipped_others_uploaded(self):
        b = _fake_backup("nas:Restore", [("a.txt", 5, 1), ("b.txt", 4, 2), ("c.txt", 3, 3)])
        job = {"id": "t2", "label": "restore", "started": 0.0}
        res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job)
        moves = [c for c in self.calls if c[1] == "moveto"]
        self.assertEqual([c[3] for c in moves], ["nas:Restore/share/b.txt", "nas:Restore/share/c.txt"])
        self.assertTrue(all("--ignore-existing" in c for c in moves))
        self.assertEqual((res["files"], res["restored"], res["skipped"], res["errors"]), (3, 1, 2, 0))
        self.assertEqual(b.pool.calls, ["b.txt", "c.txt"])


    def test_a_stalled_upload_is_retried_and_an_interrupted_one_reported(self):
        # b.txt stalls once and then uploads; d.txt stalls once and on the retry the target already holds a file (the start of
        # the interrupted upload): reported as an error, never overwritten, never counted as skipped.
        b = _fake_backup("nas:Restore", [("b.txt", 4, 2), ("d.txt", 6, 4)])
        self.stall_once = {"nas:Restore/share/b.txt", "nas:Restore/share/d.txt"}; self.partial = {"nas:Restore/share/d.txt"}
        job = {"id": "t5", "label": "restore", "started": 0.0}
        res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job)
        self.assertEqual((res["restored"], res["skipped"], res["errors"]), (1, 0, 1))
        self.assertIn("interrupted", job["errors"][0]["error"])

    def test_stop_during_an_upload_pauses_at_that_file(self):
        b = _fake_backup("nas:Restore", [("b.txt", 4, 2), ("e.txt", 5, 5)]); job = {"id": "t6", "label": "restore", "started": 0.0}
        def upload(cmd, cancelled=None, **kw):
            job["cancel"] = True; return backup_mod.UPLOAD_CANCELLED, ""
        with mock.patch.object(backup_mod, "upload_watched", side_effect=upload):
            res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job)
        self.assertTrue(res["paused"]); self.assertEqual(res["next_index"], 0)


    def test_parallel_uploads_report_progress_and_resume_at_the_oldest_unfinished_file(self):
        # four files; the uploads of a.txt and c.txt finish, b.txt is still running when Stop comes, d.txt never starts:
        # the restore pauses at b.txt (the oldest unfinished), and while b.txt ran its bar was in job["transfers"]
        import threading as th
        b = _fake_backup("nas:Restore", [("e.txt", 1, 1), ("f.txt", 2, 2), ("g.txt", 3, 3), ("h.txt", 4, 4)])
        job = {"id": "t9p", "label": "restore", "started": 0.0}; seen = []; release = th.Event()
        def upload(cmd, cancelled=None, progress=None, **kw):
            target = cmd[3]
            if target.endswith("/f.txt"):
                progress(100, 200, 50); seen.append(list(job["transfers"]))
                job["cancel"] = True
                while not cancelled(): pass
                return backup_mod.UPLOAD_CANCELLED, ""
            os.unlink(cmd[2]); return 0, ""
        with mock.patch.object(backup_mod, "upload_watched", side_effect=upload):
            res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job)
        self.assertTrue(res["paused"]); self.assertEqual(res["next_index"], 1)
        self.assertEqual(seen[0], [{"kind": "upload", "name": "share/f.txt", "done": 100, "total": 200, "rate": 50}])
        self.assertEqual(job["transfers"], [])                                          # cleared when the restore stops

    def test_staged_bytes_are_bounded_and_one_large_file_still_goes(self):
        # STAGE_MAX = 5 bytes; each upload holds its file 0.3 s. When a file is extracted, what already waits plus the new file
        # stays within 5 bytes, unless nothing waits (h.txt, 9 bytes, still goes alone)
        import threading as th                                                      # (time.sleep is patched out in setUp)
        b = _fake_backup("nas:Restore", [("e.txt", 1, 1), ("f.txt", 2, 2), ("g.txt", 3, 3), ("h.txt", 9, 4)])
        job = {"id": "t9s", "label": "restore", "started": 0.0}; seen = []; real = b.pool.extract
        def extract(off, out, size=None, **kw):
            stage = out
            while not os.path.basename(stage).startswith("hbk-stage-"): stage = os.path.dirname(stage)
            seen.append((sum(os.path.getsize(os.path.join(r, n)) for r, _, ns in os.walk(stage) for n in ns), size))
            return real(off, out, size=size, **kw)
        b.pool.extract = extract
        def upload(cmd, cancelled=None, **kw):
            th.Event().wait(0.3); os.unlink(cmd[2]); return 0, ""
        with mock.patch.object(backup_mod, "upload_watched", side_effect=upload), mock.patch.object(backup_mod, "STAGE_MAX", 5):
            res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job)
        self.assertEqual(res["restored"], 4)
        self.assertEqual(len(seen), 4)
        for waiting, size in seen: self.assertTrue(waiting == 0 or waiting + size <= 5, seen)
        self.assertEqual(seen[1], (1, 2))                                           # f.txt joins e.txt: 3 bytes fit

class UploadWatchdog(unittest.TestCase):
    def test_kill_uploads_ends_running_upload_processes(self):
        import sys, threading as th, time as _t
        out = {}
        t = th.Thread(target=lambda: out.setdefault("rc", backup_mod.upload_watched([sys.executable, "-c", "import time; time.sleep(30)"], stall_s=60, poll_s=1)))
        t.start(); _t.sleep(1.5)
        self.assertEqual(backup_mod.kill_uploads(), 1); t.join(10)
        self.assertFalse(t.is_alive()); self.assertNotEqual(out["rc"][0], 0)

    def test_rclone_units(self):
        self.assertEqual(backup_mod._bytes("512 B"), 512); self.assertEqual(backup_mod._bytes("1.5 MiB"), int(1.5 * (1 << 20)))
        self.assertEqual(backup_mod._bytes("2GiB"), 2 << 30); self.assertEqual(backup_mod._bytes(""), 0)

    def test_progress_callback_gets_bytes(self):
        import sys
        got = []
        rc, _ = backup_mod.upload_watched([sys.executable, "-c", "import sys; print('1 MiB / 4 MiB, 25%, 512 KiB/s, ETA 6s', file=sys.stderr, flush=True)"],
                                          poll_s=1, stall_s=5, progress=lambda d, t, r: got.append((d, t, r)))
        self.assertEqual(rc, 0); self.assertEqual(got[0], (1 << 20, 4 << 20, 512 << 10))

    """upload_watched with a stand-in for rclone that prints its one-line stats to stderr."""
    def run_fake(self, script, **kw):
        import sys
        return backup_mod.upload_watched([sys.executable, "-c", script], poll_s=1, **kw)

    def test_progress_then_success(self):
        rc, _ = self.run_fake("import sys,time\nfor i in range(3): print(f'{i} MiB / 3 MiB, 33%', file=sys.stderr, flush=True); time.sleep(0.3)", stall_s=5)
        self.assertEqual(rc, 0)

    def test_no_progress_is_a_stall(self):
        rc, _ = self.run_fake("import sys,time\nprint('1 MiB / 3 MiB', file=sys.stderr, flush=True)\ntime.sleep(30)", stall_s=2)
        self.assertEqual(rc, backup_mod.UPLOAD_STALLED)

    def test_stop_ends_it_at_once(self):
        import time as _t
        t = _t.time(); rc, _ = self.run_fake("import time\ntime.sleep(30)", stall_s=60, cancelled=lambda: _t.time() - t > 1)
        self.assertEqual(rc, backup_mod.UPLOAD_CANCELLED); self.assertLess(_t.time() - t, 10)


class NoOverwriteParameter(unittest.TestCase):
    def test_restore_has_no_overwrite_option(self):
        self.assertNotIn("overwrite", inspect.signature(Backup.restore).parameters)


if __name__ == "__main__":
    unittest.main()
