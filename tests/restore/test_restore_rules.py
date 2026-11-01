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
from callido_restore.sources import rclone


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

    def test_existing_file_with_other_size_is_kept_and_reported(self):
        # a file of another size at the target may be the start of an interrupted copy: kept untouched, reported as an error
        old = os.path.join(self.dest, "share", "a.txt"); Path(old).write_bytes(b"OLD"); os.utime(old, (1000, 1000))
        res, job, b = self.run_restore([("a.txt", 5, 1700000000), ("b.txt", 4, 1700000001)])
        self.assertEqual(Path(old).read_bytes(), b"OLD"); self.assertEqual(int(os.stat(old).st_mtime), 1000)
        self.assertEqual(Path(self.dest, "share", "b.txt").read_bytes(), b"bbbb")
        self.assertEqual(int(os.stat(os.path.join(self.dest, "share", "b.txt")).st_mtime), 1700000001)
        self.assertEqual((res["files"], res["restored"], res["skipped"], res["errors"]), (2, 1, 0, 1))      # restored = files written
        self.assertIn("3 bytes is at the target", job["errors"][0]["error"]); self.assertEqual(b.pool.calls, ["b.txt"])

    def test_existing_file_with_the_same_size_and_another_mtime_is_skipped(self):
        old = os.path.join(self.dest, "share", "a.txt"); Path(old).write_bytes(b"OLD"); os.utime(old, (1000, 1000))
        res, job, b = self.run_restore([("a.txt", 3, 1700000000)])
        self.assertEqual((Path(old).read_bytes(), int(os.stat(old).st_mtime)), (b"OLD", 1000))
        self.assertEqual((res["restored"], res["skipped"], res["errors"]), (0, 1, 0)); self.assertEqual(b.pool.calls, [])
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
        self.assertEqual((res["restored"], res["skipped"], res["errors"]), (0, 0, 1))   # kept; another size than the backup's: reported
        self.assertEqual(sorted(os.listdir(self.dest)), ["share"])


class RestoreVerification(RestoreLoopLocal):
    """Every restored file is counted by how the map established its placement: verified, identical_copy or unverified (listed
    with a reason, capped like errors); skipped files are not counted; the counts go to the result and the persisted job."""
    def test_statuses_counted_and_listed(self):
        b = _fake_backup(self.dest, [("v.txt", 3, 1700000000), ("c.txt", 2, 1700000001), ("u.txt", 4, 1700000002), ("p.txt", 1, 1700000003), ("s.txt", 3, 1700000004)])
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
        self.assertEqual((res["restored"], res["skipped"] + res["errors"]), (3, 1)); self.assertEqual(sorted(b.pool.calls), ["w.txt", "x.txt", "y.txt"])
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
    """A restore to a remote target against a fake NAS (self.nas: path under nas:Restore -> size). rclone calls go through
    subprocess.run (lsjson, lsjson --stat, a server-side moveto --ignore-existing, deletefile); uploads through upload_watched."""
    ROOT = "nas:Restore/"
    PART = backup_mod.PART_SUFFIX
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        p = mock.patch.object(paths, "JOBS_DIR", Path(self.d) / "jobs"); p.start(); self.addCleanup(p.stop)
        self.calls = []; self.nas = {"share/a.txt": 3}; self.appear = {}; self.stall_once = set(); self.lost = set(); self.at_move = {}
        def run(cmd, **kw):
            self.calls.append(cmd); args = cmd[1:]
            ok = lambda out="": subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
            if args[0] == "lsjson" and "--stat" in args:
                k = args[-1].split(self.ROOT, 1)[1]
                if k not in self.nas: return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="ERROR : object not found")
                return ok(json.dumps({"Path": k, "Name": os.path.basename(k), "Size": self.nas[k], "IsDir": False}))
            if args[0] == "lsjson":
                return ok(json.dumps([{"Path": k, "Size": n} for k, n in self.nas.items()]))
            if args[0] == "moveto" and args[-2].startswith(self.ROOT):            # server-side rename, never over a file
                src, dst = (x.split(self.ROOT, 1)[1] for x in args[-2:])
                if dst in self.at_move: self.nas[dst] = self.at_move.pop(dst)        # appears between the stat and the move
                if dst in self.nas: return ok()
                if src not in self.nas: return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="ERROR : object not found")
                n = self.nas.pop(src)
                if dst in self.lost: self.lost.discard(dst)                       # lost once: the rename "worked", nothing is there
                else: self.nas[dst] = n
                return ok()
            if args[0] == "deletefile":
                self.nas.pop(args[-1].split(self.ROOT, 1)[1], None); return ok()
            raise AssertionError(cmd)
        p = mock.patch.object(subprocess, "run", side_effect=run); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(backup_mod, "upload_watched", side_effect=self.upload); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(backup_mod.time, "sleep", lambda s: None); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(backup_mod, "CHECK_WAIT_S", 0); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(rclone.DAEMON, "available", lambda: False); p.start(); self.addCleanup(p.stop)
    def put(self, cmd, size=None):
        """the upload of cmd lands at its (temporary) name on the fake NAS"""
        self.calls.append(cmd); staged, target = cmd[2], cmd[3].split(self.ROOT, 1)[1]
        self.nas[target] = os.path.getsize(staged) if size is None else size; os.unlink(staged)
        final = target.removesuffix(self.PART)
        if final in self.appear: self.nas[final] = self.appear.pop(final)    # a file with the final name shows up meanwhile
        return 0, ""
    def upload(self, cmd, cancelled=None, **kw):
        if cmd[3] in self.stall_once: self.stall_once.discard(cmd[3]); self.calls.append(cmd); return backup_mod.UPLOAD_STALLED, ""
        return self.put(cmd)
    def restore(self, files, job_id, **kw):
        b = _fake_backup("nas:Restore", files); job = {"id": job_id, "label": "restore", "started": 0.0, **kw.pop("job", {})}
        return b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job, **kw), job, b
    def uploads(self): return [c[3] for c in self.calls if c[1] == "moveto" and not c[-2].startswith(self.ROOT)]

    def test_uploads_go_to_a_temporary_name_and_are_renamed_never_over_a_file(self):
        # a.txt is there (same size: skipped); b.txt is uploaded and renamed; while c.txt uploads, a c.txt appears at the target:
        # it is kept, the app's temporary file is deleted, c.txt counts as skipped (same size)
        self.appear = {"share/c.txt": 3}
        res, job, b = self.restore([("a.txt", 3, 1), ("b.txt", 4, 2), ("c.txt", 3, 3)], "t2")
        self.assertEqual(self.uploads(), [f"nas:Restore/share/b.txt{self.PART}", f"nas:Restore/share/c.txt{self.PART}"])
        self.assertEqual(self.nas, {"share/a.txt": 3, "share/b.txt": 4, "share/c.txt": 3})        # no temporary name left
        self.assertEqual((res["files"], res["restored"], res["skipped"], res["errors"]), (3, 1, 2, 0))
        self.assertEqual(b.pool.calls, ["b.txt", "c.txt"])

    def test_a_file_appearing_between_the_check_and_the_rename_is_kept(self):
        self.at_move = {"share/b.txt": 9}
        res, job, b = self.restore([("b.txt", 4, 2)], "t2r")
        self.assertEqual(self.nas, {"share/a.txt": 3, "share/b.txt": 9})                   # theirs kept, our temporary file gone
        self.assertEqual((res["restored"], res["errors"]), (0, 1)); self.assertIn("9 bytes is at the target", job["errors"][0]["error"])

    def test_a_different_size_at_the_target_is_an_error_and_never_written_over(self):
        res, job, b = self.restore([("a.txt", 5, 1)], "t2d")                 # the NAS has a.txt with 3 bytes
        self.assertEqual(self.uploads(), []); self.assertEqual(self.nas, {"share/a.txt": 3})
        self.assertEqual((res["restored"], res["skipped"], res["errors"], res["check"]["differs"]), (0, 0, 1, 1))
        self.assertIn("3 bytes is at the target", job["errors"][0]["error"])

    def test_stop_mid_upload_leaves_only_the_temporary_name_and_resume_restores_the_file(self):
        # hbk-6's E2E case: an upload is killed half-way. Only <name>.callido-restore.part holds the cut-off data, never the real
        # name; Resume uploads the file again over that temporary name, renames it, and nothing is left behind.
        def cut(cmd, cancelled=None, **kw):
            self.put(cmd, size=2); res_job["cancel"] = True; return backup_mod.UPLOAD_CANCELLED, ""
        res_job = {}
        with mock.patch.object(backup_mod, "upload_watched", side_effect=cut):
            b = _fake_backup("nas:Restore", [("d.txt", 6, 4)]); res_job.update({"id": "t5", "label": "restore", "started": 0.0})
            res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", res_job)
        self.assertTrue(res["paused"]); self.assertNotIn("share/d.txt", self.nas); self.assertEqual(self.nas[f"share/d.txt{self.PART}"], 2)
        res2, job2, _ = self.restore([("d.txt", 6, 4)], "t5r", start_at=res["next_index"])
        self.assertEqual(self.nas, {"share/a.txt": 3, "share/d.txt": 6})
        self.assertEqual((res2["restored"], res2["errors"]), (1, 0))

    def test_a_stalled_upload_is_retried(self):
        self.stall_once = {f"nas:Restore/share/b.txt{self.PART}"}
        res, job, b = self.restore([("b.txt", 4, 2)], "t5s")
        self.assertEqual(len(self.uploads()), 2); self.assertEqual(self.nas["share/b.txt"], 4)
        self.assertEqual((res["restored"], res["errors"]), (1, 0))

    def test_final_check_restores_a_lost_file_again(self):
        self.lost = {"share/b.txt"}                                           # the rename "works" once but nothing arrives
        res, job, b = self.restore([("a.txt", 3, 1), ("b.txt", 4, 2)], "t5c")
        self.assertEqual(self.uploads(), [f"nas:Restore/share/b.txt{self.PART}"] * 2)
        self.assertEqual((res["errors"], res["restored"]), (0, 1))
        self.assertEqual(res["check"], {"ok": 2, "missing": 0, "differs": 0, "partial": 0, "restored_again": 1, "files": 2})

    def test_final_check_lists_a_file_that_stays_missing_removes_own_leftovers_and_counts_rclones(self):
        self.nas["share/b.txt.0123abcd.partial"] = 2                          # rclone's own leftover: counted, not touched
        self.nas[f"share/x.txt{self.PART}"] = 1                               # not a planned file: not touched
        def nothing(cmd, cancelled=None, **kw):
            self.calls.append(cmd); os.unlink(cmd[2]); return 0, ""            # "uploaded", never arrives
        with mock.patch.object(backup_mod, "upload_watched", side_effect=nothing):
            res, job, b = self.restore([("b.txt", 4, 2)], "t5d")
        self.assertEqual(len(self.uploads()), 1 + backup_mod.CHECK_ROUNDS)
        self.assertEqual((res["errors"], res["check"]["missing"], res["check"]["partial"]), (1, 1, 1))
        self.assertIn("could not be renamed", job["errors"][0]["error"])            # the last error of the file that stays missing
        self.assertIn(f"share/x.txt{self.PART}", self.nas)

    def test_the_check_deletes_the_apps_temporary_name_of_a_planned_file(self):
        self.nas[f"share/b.txt{self.PART}"] = 1                               # left by an earlier, killed run
        self.nas["share/b.txt"] = 4                                           # ...whose file is complete now
        res, job, b = self.restore([("b.txt", 4, 2)], "t5e")
        self.assertEqual(self.nas, {"share/a.txt": 3, "share/b.txt": 4})
        self.assertEqual((res["skipped"], res["errors"]), (1, 0))

    def test_stop_during_an_upload_pauses_at_that_file(self):
        job = {}
        def upload(cmd, cancelled=None, **kw):
            job["j"]["cancel"] = True; return backup_mod.UPLOAD_CANCELLED, ""
        b = _fake_backup("nas:Restore", [("b.txt", 4, 2), ("e.txt", 5, 5)]); job["j"] = {"id": "t6", "label": "restore", "started": 0.0}
        with mock.patch.object(backup_mod, "upload_watched", side_effect=upload):
            res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job["j"])
        self.assertTrue(res["paused"]); self.assertEqual(res["next_index"], 0)

    def test_parallel_uploads_report_progress_resume_at_the_oldest_unfinished_and_do_not_count_finished_ones_again(self):
        # four files; f.txt is still running when Stop comes, the others may finish: the restore pauses at f.txt (the oldest
        # unfinished); while f.txt ran its bar was in job["transfers"]; on resume the files that finished are not counted again
        b = _fake_backup("nas:Restore", [("e.txt", 1, 1), ("f.txt", 2, 2), ("g.txt", 3, 3), ("h.txt", 4, 4)])
        job = {"id": "t9p", "label": "restore", "started": 0.0}; seen = []
        def upload(cmd, cancelled=None, progress=None, **kw):
            if "/f.txt" in cmd[3]:
                progress(100, 200, 50); seen.append(list(job["transfers"]))
                job["cancel"] = True
                while not cancelled(): pass
                return backup_mod.UPLOAD_CANCELLED, ""
            return self.put(cmd)
        with mock.patch.object(backup_mod, "upload_watched", side_effect=upload):
            res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job)
        self.assertTrue(res["paused"]); self.assertEqual(res["next_index"], 1)
        self.assertIn({"kind": "upload", "name": "share/f.txt", "done": 100, "total": 200, "rate": 50}, seen[0])   # (other uploads may run too; each bar starts at 0)
        self.assertEqual(job["transfers"], [])                                          # cleared when the restore stops
        self.assertEqual(job["rates"], {"download": 0, "upload": 0})
        saved = json.loads((paths.JOBS_DIR / "t9p.json").read_text())
        seed = {k: saved[k] for k in ("done_files", "skipped", "errors", "errors_total", "verified", "identical_copy", "unverified", "unverified_total", "done_ahead")}
        res2, job2, _ = self.restore([("e.txt", 1, 1), ("f.txt", 2, 2), ("g.txt", 3, 3), ("h.txt", 4, 4)], "t9r", start_at=saved["next_index"], job=seed)
        self.assertEqual((res2["restored"], res2["skipped"], res2["errors"]), (4, 0, 0))   # each file counted once
        self.assertEqual(res2["check"]["ok"], 4)

    def test_a_stop_while_the_first_file_uploads_keeps_the_counts_on_resume(self):
        # Stop while e.txt (index 0) uploads and the others finish: next_index is 0; Resume must keep the seeded counts and
        # count each file once (it restarted the counts at position 0, and the files in done_ahead were never counted)
        b = _fake_backup("nas:Restore", [("e.txt", 1, 1), ("f.txt", 2, 2), ("g.txt", 3, 3)])
        job = {"id": "t0s", "label": "restore", "started": 0.0}
        import threading as th; others = th.Event(); n = [0]
        def upload(cmd, cancelled=None, **kw):
            if "/e.txt" in cmd[3]:
                others.wait(5); job["cancel"] = True
                while not cancelled(): pass
                return backup_mod.UPLOAD_CANCELLED, ""
            r = self.put(cmd); n[0] += 1
            if n[0] == 2: others.set()
            return r
        with mock.patch.object(backup_mod, "upload_watched", side_effect=upload):
            res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job)
        self.assertEqual((res["paused"], res["next_index"], res["restored"]), (True, 0, 2))
        saved = json.loads((paths.JOBS_DIR / "t0s.json").read_text())
        seed = {k: saved[k] for k in ("done_files", "skipped", "errors", "errors_total", "verified", "identical_copy", "unverified", "unverified_total", "done_ahead")}
        res2, job2, _ = self.restore([("e.txt", 1, 1), ("f.txt", 2, 2), ("g.txt", 3, 3)], "t0r", start_at=0, job=seed)
        self.assertEqual((res2["restored"], res2["skipped"], res2["errors"]), (3, 0, 0))

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
        lk = th.Lock()
        def upload(cmd, cancelled=None, **kw):
            th.Event().wait(0.3)
            with lk: return self.put(cmd)
        with mock.patch.object(backup_mod, "upload_watched", side_effect=upload), mock.patch.object(backup_mod, "STAGE_MAX", 5):
            res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:Restore", job)
        self.assertEqual(res["restored"], 4)
        self.assertEqual(len(seen), 4)
        for waiting, size in seen: self.assertTrue(waiting == 0 or waiting + size <= 5, seen)
        self.assertEqual(seen[1], (1, 2))                                           # f.txt joins e.txt: 3 bytes fit


class RestoreCache(unittest.TestCase):
    """The bucket cache is a passage: it stays near HBK_CACHE_GB during a restore, buckets left by an earlier run count too, a
    finished restore deletes them, and nothing but bucket files in the app cache is ever deleted."""
    def test_cache_is_bounded_counts_old_buckets_and_is_emptied_at_the_end(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        cache = d / "cache"; dest = d / "dest"; (dest / "share").mkdir(parents=True)
        p = mock.patch.object(paths, "JOBS_DIR", d / "jobs"); p.start(); self.addCleanup(p.stop)
        old = cache / "Pool/0/9/5.bucket.3"; old.parent.mkdir(parents=True); old.write_bytes(b"x" * 100)      # an earlier run's bucket
        keep = cache / "Config/file_chunk4.index/1.idx.3"; keep.parent.mkdir(parents=True); keep.write_bytes(b"i")
        sizes = []
        class Src:
            id = "cachetest"; cache_dir = cache
            def in_cache(self, q): return Path(q).resolve().is_relative_to(cache.resolve())
            def locate(self, rel): return (cache / rel) if (cache / rel).is_file() else None
            def listdir(self, rel): return {}
            def ensure(self, rel, progress=None, **kw):
                q = cache / rel; q.parent.mkdir(parents=True, exist_ok=True); q.write_bytes(b"b" * 100)
                def size(f):                                              # another fetch thread may delete it meanwhile
                    try: return f.stat().st_size
                    except FileNotFoundError: return 0
                sizes.append(sum(size(f) for f in (cache / "Pool").rglob("*.bucket.*"))); return q
        class Pool(_FakePool):
            def plan(self, off): return [(off, 0)], None                  # file n reads bucket n
            def bucket_name(self, b, kind): return f"Pool/0/{b // 2048}/{b % 2048}.bucket.1"
        b = _Backup.__new__(_Backup); b.src = Src(); b._fake_pool = Pool(str(dest))
        b.restore_plan = lambda items, skip_meta=True: [{"share": "share", "path": f"f{n}.txt", "off": n, "size": 4, "mtime": 0} for n in range(100)]
        job = {"id": "tc", "label": "restore", "started": 0.0}
        with mock.patch.dict(os.environ, {"HBK_CACHE_GB": str(1000 / (1 << 30))}):          # 1000 bytes = 10 buckets
            res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], str(dest), job)
        self.assertEqual(res["restored"], 100)
        self.assertFalse(old.exists())                                    # counted and deleted, it was not needed
        self.assertTrue(keep.exists())                                    # not a bucket: never touched
        self.assertEqual(sorted(q.name for q in (cache / "Pool").rglob("*.bucket.*")), [])  # emptied at the end
        self.assertLess(max(sizes), 100 * 60)                             # bounded: never all 100 buckets at once (look-ahead 40 + limit)

class DownloadBars(unittest.TestCase):
    def test_files_waiting_for_their_data_get_a_download_bar_by_file_name(self):
        # three files, each in its own bucket; while bucket 1 downloads (half done), the page sees a "download" bar named after
        # the file that needs it (not the bucket), with the bucket's bytes and speed; the bucket itself is kind "bucket"
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        cache = d / "cache"; dest = d / "dest"; (dest / "share").mkdir(parents=True)
        p = mock.patch.object(paths, "JOBS_DIR", d / "jobs"); p.start(); self.addCleanup(p.stop)
        seen = []; job = {"id": "tb", "label": "restore", "started": 0.0}
        class Src:
            id = "bars"; cache_dir = cache
            def in_cache(self, q): return True
            def locate(self, rel): return None
            def listdir(self, rel): return {n: {"size": 100} for n in ("0.bucket.1", "1.bucket.1", "2.bucket.1")}
            def ensure(self, rel, progress=None, **kw):
                if rel.endswith("/1.bucket.1"):
                    progress(50, 25); seen.append([dict(t) for t in job["transfers"]])
                q = cache / rel; q.parent.mkdir(parents=True, exist_ok=True); q.write_bytes(b"b" * 100); return q
        class Pool(_FakePool):
            def plan(self, off): return [(off, 0)], None
            def bucket_name(self, b, kind): return f"Pool/0/0/{b}.bucket.1"
        b = _Backup.__new__(_Backup); b.src = Src(); b._fake_pool = Pool(str(dest))
        b.restore_plan = lambda items, skip_meta=True: [{"share": "share", "path": f"f{n}.txt", "off": n, "size": 4, "mtime": 0} for n in range(3)]
        res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], str(dest), job)
        self.assertEqual(res["restored"], 3)
        bars = [t for t in seen[0] if t["kind"] == "download"]
        self.assertIn({"kind": "download", "name": "share/f1.txt", "done": 50, "total": 100, "rate": 25}, bars)
        self.assertTrue(all(t["kind"] in ("download", "bucket") for t in seen[0]))
        self.assertIn({"kind": "bucket", "name": "1.bucket.1", "done": 50, "total": 100, "rate": 25}, seen[0])
        self.assertEqual(job["transfers"], [])                                    # cleared at the end

class AlreadyThereNeedsNoDownload(unittest.TestCase):
    def test_files_at_the_target_are_skipped_without_fetching_their_data(self):
        # Niels: "the check on what's restored already ... can't that be quicker by a listing": a file already at the target
        # (name and size) is skipped before anything of the backup is downloaded for it
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        dest = d / "dest"; (dest / "share").mkdir(parents=True); (dest / "share" / "f0.txt").write_bytes(b"xxxx")
        p = mock.patch.object(paths, "JOBS_DIR", d / "jobs"); p.start(); self.addCleanup(p.stop)
        fetched = []
        class Src:
            id = "there"; cache_dir = d / "cache"
            def in_cache(self, q): return True
            def locate(self, rel): return None
            def listdir(self, rel): return {}
            def ensure(self, rel, progress=None, **kw):
                fetched.append(rel); q = d / "cache" / rel; q.parent.mkdir(parents=True, exist_ok=True); q.write_bytes(b"b"); return q
        class Pool(_FakePool):
            def plan(self, off): return [(off, 0)], None
            def bucket_name(self, b, kind): return f"Pool/0/0/{b}.bucket.1"
        b = _Backup.__new__(_Backup); b.src = Src(); b._fake_pool = Pool(str(dest))
        b.restore_plan = lambda items, skip_meta=True: [{"share": "share", "path": f"f{n}.txt", "off": n, "size": 4, "mtime": 0} for n in range(2)]
        res = b.restore([{"share": "share", "ver": 1, "path": "", "is_dir": True}], str(dest), {"id": "th", "label": "restore", "started": 0.0})
        self.assertEqual((res["restored"], res["skipped"]), (1, 1))
        self.assertEqual(fetched, ["Pool/0/0/1.bucket.1"])                      # nothing fetched for f0.txt


class CompareTarget(unittest.TestCase):
    """Niels restored folders himself: the run's selection against a target by name and size, nothing written."""
    def test_local_target_counts_same_missing_and_other_size_and_writes_nothing(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        (d / "share" / "a").mkdir(parents=True); (d / "share" / "a" / "x.txt").write_bytes(b"1234"); (d / "share" / "y.txt").write_bytes(b"12")
        b = _fake_backup(str(d), [("a/x.txt", 4, 0), ("y.txt", 3, 0), ("a/b/z.txt", 5, 0)])
        before = sorted(p.as_posix() for p in d.rglob("*"))
        job = {}; res = b.compare_target([{"share": "share", "ver": 1, "path": "", "is_dir": True}], str(d), job)
        self.assertEqual((res["files"], res["same"], res["missing"], res["differs"]), (3, 1, 1, 1))
        self.assertEqual(res["missing_list"], [{"path": "share/a/b/z.txt", "size": 5}])
        self.assertEqual(res["differs_list"], [{"path": "share/y.txt", "at_target": 2, "in_backup": 3}])
        self.assertEqual(res["missing_by_folder"], [{"folder": "share/a/b", "files": 1, "bytes": 5}])
        self.assertEqual(sorted(p.as_posix() for p in d.rglob("*")), before)
        self.assertEqual(b.pool.calls, [])                                       # nothing read from the backup's data

    def test_nas_target_uses_one_listing(self):
        b = _fake_backup("nas:R", [("a.txt", 3, 0), ("b.txt", 4, 0)])
        with mock.patch.object(backup_mod, "destination_sizes", return_value={"share/a.txt": 3}) as ls:
            res = b.compare_target([{"share": "share", "ver": 1, "path": "", "is_dir": True}], "nas:R", {})
        ls.assert_called_once_with("nas:R")
        self.assertEqual((res["same"], res["missing"], res["missing_bytes"]), (1, 1, 4))

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
