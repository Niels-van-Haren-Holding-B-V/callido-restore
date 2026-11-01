"""End to end through the real app process (HBK_E2E=1), for the paths real users take that test_app.py does not:

* RemoteSourceAndDestination: the backup is read through an rclone remote (how Google Drive users run the app: rclone daemon,
  listings, on-demand downloads into the app cache, parallel bucket fetches) and restored to an rclone remote (how a NAS is
  reached), with a file already at the target that must be kept. Needs the rclone binary; the remotes are `alias` remotes on
  temporary folders, in the test's own rclone.conf (HOME is a temporary folder).
* DamagedBucket: one bucket of the backup is corrupted (bit rot on the storage): exactly the files that read it fail, while the
  restore runs (#160: errors visible live and logged), nothing half-written reaches the target, everything else restores;
  an update install is refused while the restore runs.
* LocalApiGuard: the running server refuses requests that other web pages can make (#182/#184: DNS rebinding Host, cross-site
  Origin / Sec-Fetch-Site, opaque Origin null) and still serves the app's own requests and command-line tools.
"""
import json, os, shutil, tempfile, time, unittest
from pathlib import Path

from tests.e2e import flow, hbkgen
from tests.e2e.apprun import App

E2E = os.environ.get("HBK_E2E") == "1"
SHARES = ("Documents", "Photos", "homes")


def tree(root):
    """{relative path: (size, mtime_ns)} of every file under root"""
    root = Path(root)
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def have_rclone():
    from callido_restore.paths import RCLONE
    return bool(shutil.which(RCLONE) or (os.path.isfile(RCLONE) and os.access(RCLONE, os.X_OK)))


@unittest.skipUnless(E2E, "set HBK_E2E=1 to run the app-level end-to-end tests")
@unittest.skipUnless(E2E and have_rclone(), "needs the rclone binary")
class RemoteSourceAndDestination(unittest.TestCase):
    S = {}

    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="hbk-e2e-remote-")); home = cls.work / "home"; home.mkdir()
        cls.drive = cls.work / "drive"; cls.nas = cls.work / "nas"; cls.nas.mkdir()
        cls.m = hbkgen.build(str(cls.drive / "e2e_1.hbk"), scale=6, bucket_cap=1 << 20, chunk_index=True, attr_photos=True)
        keep = cls.nas / "restore" / "Documents" / "notes.txt"; keep.parent.mkdir(parents=True); keep.write_bytes(b"the NAS already has this file\n")
        cls.app = App(home, cls.work / "app.log")
        cls.app.rclone("config", "create", "e2edrive", "alias", f"remote={cls.drive}")     # "Google Drive"
        cls.app.rclone("config", "create", "e2enas", "alias", f"remote={cls.nas}")         # "the NAS"
        cls.before = tree(cls.drive)
        cls.app.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        if os.environ.get("HBK_E2E_KEEP") == "1": print("\nkept:", cls.work)
        else: shutil.rmtree(cls.work, ignore_errors=True)

    def test_1_open_through_the_remote(self):
        a = self.app; S = self.S
        self.assertIn("e2edrive", json.dumps(a.get("/api/remotes")))
        S["src"] = src = a.post("/api/sources", name="E2E on Drive", local="~/CallidoRestore/e2e_1", remote="e2edrive:e2e_1.hbk")["id"]
        j = a.wait_job(a.get("/api/open", src=src, job="1")["id"]); self.assertEqual(j["state"], "done", j)
        info = a.get("/api/open", src=src); self.assertTrue(info["readable"], info.get("problems"))
        self.assertEqual([s["name"] for s in info["shares"]], list(SHARES))
        j = a.wait_job(a.get("/api/index", src=src, share="*", ver=self.m["last_clean"])["id"])
        self.assertTrue(all(x["ok"] for x in j["result"]), j["result"])
        st = a.get("/api/pool", src=src); self.assertTrue(st.get("chunk_index") and st["complete"], st)

    def test_2_restore_to_the_nas_keeps_what_is_there(self):
        a = self.app; S = self.S; self.assertIn("src", S); ver = self.m["last_clean"]
        chk = a.get("/api/dest/check", src=S["src"], dest="e2enas:restore", shares=json.dumps(list(SHARES)))
        self.assertTrue(chk["exists"] and not chk["inside_source"], chk)
        chk = a.get("/api/dest/check", src=S["src"], dest="e2edrive:e2e_1.hbk/restore", shares=json.dumps(list(SHARES)))
        self.assertTrue(chk["inside_source"], f"a target inside the backup on the remote must be refused: {chk}")
        items = [{"share": sh, "ver": ver, "path": "", "is_dir": True} for sh in SHARES]
        j = a.wait_job(a.post("/api/restore", src=S["src"], items=items, dest="e2enas:restore")["id"], timeout=600)
        # notes.txt at the target has another size than the backup's: kept untouched and reported as the one error (#208)
        self.assertEqual(j["state"], "done", j.get("error")); self.assertEqual([e["path"] for e in j["errors"]], ["Documents/notes.txt"], j["errors"])
        total = sum(len(v) for v in self.m["versions"][str(ver)].values())
        self.assertEqual((j["result"]["skipped"], j["result"]["restored"], j["result"]["errors"]), (0, total - 1, 1), j["result"])
        self.assertEqual(j["result"]["check"]["ok"], total - 1, j["result"])
        self.assertEqual((self.nas / "restore/Documents/notes.txt").read_bytes(), b"the NAS already has this file\n")
        cmp_ = flow.compare(self.m, ver, {sh: self.nas / "restore" / sh for sh in SHARES}, only=lambda sh, p: (sh, p) != ("Documents", "notes.txt"))
        for k in ("missing", "wrong_bytes", "wrong_mtime"): self.assertEqual(cmp_[k], [], k)
        self.assertEqual(cmp_["extra"], ["Documents/notes.txt"])
        S["items"] = items

    def test_3_second_restore_writes_nothing(self):
        a = self.app; S = self.S; self.assertIn("items", S)
        before = tree(self.nas)
        j = a.wait_job(a.post("/api/restore", src=S["src"], items=S["items"], dest="e2enas:restore")["id"], timeout=600)
        self.assertEqual(j["state"], "done", j.get("error"))
        self.assertEqual(j["result"]["restored"], 0, j["result"])
        self.assertEqual((j["result"]["skipped"], j["result"]["errors"]), (j["result"]["files"] - 1, 1), j["result"])   # notes.txt: kept, reported
        self.assertEqual(tree(self.nas), before)

    def test_4_the_backup_is_only_read(self):
        self.assertIn("src", self.S)
        self.assertEqual(tree(self.drive), self.before, "nothing in the backup on the remote may change")
        cache = Path(self.app.home, ".hbk-explorer", "cache", self.S["src"])
        left = [p for p in cache.rglob("*.bucket.*")] if cache.is_dir() else []
        self.assertEqual(left, [], f"a finished restore empties the bucket cache {cache} (#208)")


@unittest.skipUnless(E2E, "set HBK_E2E=1 to run the app-level end-to-end tests")
@unittest.skipUnless(E2E and have_rclone(), "needs the rclone binary")
class UploadsToANasStopAndResume(unittest.TestCase):
    """#202/#201: a restore to a NAS uploads several files at once with per-file progress (job["transfers"]); Stop in the middle
    of uploads, then Resume: every file lands byte-exact, nothing is skipped and no half-uploaded file is kept. The "NAS" is
    `rclone serve webdav` on a local port and the app's uploads are held to 1 MB/s each (RCLONE_BWLIMIT), so they really stream
    and can be cut off mid-file (an
    alias remote on a local folder would make every upload an instant rename)."""
    @classmethod
    def setUpClass(cls):
        import subprocess
        from tests.e2e.apprun import free_port
        cls.work = Path(tempfile.mkdtemp(prefix="hbk-e2e-upload-")); home = cls.work / "home"; home.mkdir()
        cls.hbk = cls.work / "backups" / "e2e_1.hbk"; cls.nas = cls.work / "nas"; cls.nas.mkdir()
        cls.m = hbkgen.build(str(cls.hbk), scale=10, bucket_cap=2 << 20, chunk_index=True)
        cls.app = App(home, cls.work / "app.log", env={"RCLONE_BWLIMIT": "1M"})   # every upload process of the app at 1 MB/s
        from callido_restore.paths import RCLONE
        port = free_port()
        cls.dav = subprocess.Popen([RCLONE, "serve", "webdav", str(cls.nas), "--addr", f"127.0.0.1:{port}"],
                                   env={**cls.app.env, "RCLONE_BWLIMIT": "off"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.app.rclone("config", "create", "e2enas", "webdav", f"url=http://127.0.0.1:{port}", "vendor=other")
        for _ in range(100):                                       # the WebDAV server is up when the remote lists
            try: cls.app.rclone("lsd", "e2enas:"); break
            except RuntimeError: time.sleep(0.1)
        cls.app.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop(); cls.dav.terminate()
        try: cls.dav.wait(10)
        except Exception: cls.dav.kill()
        if os.environ.get("HBK_E2E_KEEP") == "1": print("\nkept:", cls.work)
        else: shutil.rmtree(cls.work, ignore_errors=True)

    @classmethod
    def run_flow(cls):
        """Restore to the NAS, Stop while uploads are part-way, look at the NAS, Resume. Runs once; the tests read cls.R."""
        if hasattr(cls, "R"): return cls.R
        a = cls.app; m = cls.m; ver = m["last_clean"]; R = cls.R = {}
        src = a.post("/api/sources", name="upload", local=str(cls.hbk))["id"]
        a.wait_job(a.get("/api/open", src=src, job="1")["id"]); a.wait_job(a.get("/api/index", src=src, share="*", ver=ver)["id"])
        items = [{"share": sh, "ver": ver, "path": "", "is_dir": True} for sh in SHARES]
        jid = a.post("/api/restore", src=src, items=items, dest="e2enas:restore")["id"]
        R["most"] = 0; R["partial_bar"] = None; t0 = time.time()
        while time.time() - t0 < 300:                              # wait for several uploads in flight, one of them part-way
            j = a.get("/api/job", id=jid)
            ups = [x for x in j.get("transfers") or [] if x.get("kind") == "upload"]
            R["most"] = max(R["most"], len(ups))
            if R["partial_bar"] is None: R["partial_bar"] = next((x for x in ups if 0 < x["done"] < x["total"]), None)
            if j["state"] != "running" or (R["most"] >= 2 and R["partial_bar"] and (j.get("done_files") or 0) >= 2): break
            time.sleep(0.2)
        R["running_at_stop"] = j["state"] == "running"
        a.get("/api/job/cancel", id=jid); j = a.wait_job(jid, timeout=120); R["stop"] = j
        R["expected"] = expected = {f"{sh}/{p}": e for sh in SHARES for p, e in m["versions"][str(ver)][sh].items()}
        root = cls.nas / "restore"
        on_nas = {f.relative_to(root).as_posix(): f.stat().st_size for f in root.rglob("*") if f.is_file()} if root.is_dir() else {}
        R["truncated"] = sorted(p for p, sz in on_nas.items() if p in expected and sz != expected[p]["size"])
        R["strays"] = sorted(p for p in on_nas if p not in expected)
        R["final"] = a.wait_job(a.post("/api/restore/resume", id=jid)["id"], timeout=600)
        after = {f.relative_to(root).as_posix() for f in root.rglob("*") if f.is_file()} if root.is_dir() else set()
        R["strays_after_resume"] = sorted(p for p in after if p not in expected)      # e.g. temporary upload names left behind
        R["cmp"] = flow.compare(m, ver, {sh: root / sh for sh in SHARES})
        R["report"] = (f"uploads at once: {R['most']}; a bar part-way: {R['partial_bar']}; stopped: {j.get('result')}; truncated on the NAS "
                       f"after Stop: {R['truncated']}; other names after Stop {R['strays']} and after Resume {R['strays_after_resume']}; final {R['final'].get('result')}")
        return R

    def test_1_uploads_run_side_by_side_with_progress_and_stop(self):
        R = self.run_flow()
        self.assertTrue(R["running_at_stop"], "the restore finished before Stop: lower RCLONE_BWLIMIT or raise the scale")
        self.assertGreaterEqual(R["most"], 2, R["report"])
        b = R["partial_bar"]; self.assertIsNotNone(b, R["report"]); self.assertTrue(b["name"] and b["rate"] > 0, b)
        self.assertTrue(R["stop"]["result"].get("paused"), R["report"])
        self.assertEqual(R["strays_after_resume"], [], "no temporary or stray files may stay on the NAS: " + R["report"])
        self.assertEqual(R["final"]["state"], "done", R["final"].get("error")); self.assertEqual(R["final"]["errors"], [])

    def test_2_resume_leaves_no_truncated_file(self):          # E9, fixed in #208
        """hbk-6 finding E9: Stop kills the running `rclone moveto`s; the target keeps each partial upload under its final name
        (WebDAV here; SMB unverified), and Resume skips it as "already there" (--ignore-existing): the restore ends with
        0 errors and the files that were in flight truncated on the NAS."""
        R = self.run_flow()
        self.assertEqual(R["truncated"], [], "after Stop no file under its final name may be part of an upload: " + R["report"])
        for k in ("missing", "wrong_bytes", "extra"): self.assertEqual(R["cmp"][k], [], f"{k}: " + R["report"])

    def test_3_resume_counts_every_file_once(self):            # E10, fixed in #208
        """hbk-6 finding E10: Resume restarts at the oldest unfinished file; the files that finished before Stop are seeded
        as restored and counted again as skipped (e.g. 21 restored + 21 skipped for 25 files)."""
        R = self.run_flow(); r = R["final"]["result"]
        self.assertEqual(r["restored"] + r["skipped"], len(R["expected"]), R["report"])

@unittest.skipUnless(E2E, "set HBK_E2E=1 to run the app-level end-to-end tests")
class DamagedBucket(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="hbk-e2e-damaged-")); home = cls.work / "home"; home.mkdir()
        cls.hbk = cls.work / "backups" / "e2e_1.hbk"
        cls.m = m = hbkgen.build(str(cls.hbk), scale=6, bucket_cap=1 << 20, chunk_index=True)
        ver = str(m["last_clean"]); users = {}
        for sh in SHARES:
            for p, e in m["versions"][ver][sh].items():
                for b in e["buckets"]: users.setdefault(b, set()).add(f"{sh}/{p}")
        # a bucket that one Documents file reads alone: its error comes early in the restore (shares in order, paths sorted)
        cls.bucket, (cls.victim,) = next((b, u) for b, u in sorted(users.items()) if len(u) == 1 and next(iter(u)).startswith("Documents/"))
        bf = next((cls.hbk / "Pool" / "0" / str(cls.bucket // 2048)).glob(f"{cls.bucket % 2048}.bucket.*"))
        data = bytearray(bf.read_bytes()); mid = len(data) // 2
        for i in range(mid, mid + 64): data[i] ^= 0xFF
        bf.write_bytes(bytes(data))
        cls.app = App(home, cls.work / "app.log")
        upd = home / ".hbk-explorer"; upd.mkdir(parents=True)          # an update on offer (no network: HBK_NO_UPDATE_CHECK=1)
        (upd / "update.json").write_text(json.dumps({"checked": time.time(), "release": {"tag_name": "v99.0.0", "name": "v99.0.0", "body": "test",
                                                     "html_url": "https://github.com/x/y/releases/tag/v99.0.0", "assets": []}}))
        cls.app.start()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        if os.environ.get("HBK_E2E_KEEP") == "1": print("\nkept:", cls.work)
        else: shutil.rmtree(cls.work, ignore_errors=True)

    def test_damaged_bucket(self):
        a = self.app; ver = self.m["last_clean"]
        src = a.post("/api/sources", name="damaged", local=str(self.hbk))["id"]
        a.wait_job(a.get("/api/open", src=src, job="1")["id"])
        a.wait_job(a.get("/api/index", src=src, share="*", ver=ver)["id"])
        dest = self.work / "restore"
        items = [{"share": sh, "ver": ver, "path": "", "is_dir": True} for sh in SHARES]
        jid = a.post("/api/restore", src=src, items=items, dest=str(dest))["id"]
        live = None; refused = None
        while True:
            j = a.get("/api/job", id=jid)
            if j["state"] != "running": break
            if refused is None:                                      # an update must wait for a running restore
                st, body = a.request("POST", "/api/update/install", body={}); refused = (st, body)
            if j.get("errors") and live is None: live = (j["done_files"], j["total_files"], [e["path"] for e in j["errors"]])
            time.sleep(0.1)
        self.assertEqual(j["state"], "done", j.get("error"))
        self.assertEqual([e["path"] for e in j["errors"]], [self.victim], j["errors"])
        self.assertIsNotNone(live, "the error must be visible while the restore runs (#160)")
        self.assertLess(live[0], live[1], f"seen only at the end: {live}")
        self.assertEqual(refused[0], 409, refused); self.assertIn("restore", refused[1].get("error", ""))
        sh, p = self.victim.split("/", 1)
        self.assertFalse((dest / sh / p).exists(), "a file that failed must not reach the target")
        self.assertEqual([x for x in tree(dest) if x.endswith(".part") or "/.hbk-restore-" in "/" + x], [])
        cmp_ = flow.compare(self.m, ver, {s: dest / s for s in SHARES}, only=lambda s, q: f"{s}/{q}" != self.victim)
        for k in ("missing", "wrong_bytes", "wrong_mtime", "extra"): self.assertEqual(cmp_[k], [], k)
        self.assertIn(f"restore error 1: {self.victim}", a.output(), "the error must be in the log (app.log in the packaged app)")


@unittest.skipUnless(E2E, "set HBK_E2E=1 to run the app-level end-to-end tests")
class ResumeWhileARestoreRuns(unittest.TestCase):
    """#190: a stopped restore cannot be resumed while another restore runs (409); once that one is done it resumes and the
    whole run lands byte-exact next to the other one."""
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="hbk-e2e-resume-")); home = cls.work / "home"; home.mkdir()
        cls.hbk = cls.work / "backups" / "e2e_1.hbk"
        cls.m = hbkgen.build(str(cls.hbk), scale=40, bucket_cap=2 << 20, chunk_index=True)
        cls.app = App(home, cls.work / "app.log").start()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop(); shutil.rmtree(cls.work, ignore_errors=True)

    def test_resume_is_refused_while_another_restore_runs(self):
        a = self.app; ver = self.m["last_clean"]
        src = a.post("/api/sources", name="resume", local=str(self.hbk))["id"]
        a.wait_job(a.get("/api/open", src=src, job="1")["id"]); a.wait_job(a.get("/api/index", src=src, share="*", ver=ver)["id"])
        items = [{"share": sh, "ver": ver, "path": "", "is_dir": True} for sh in SHARES]
        first, second = self.work / "first", self.work / "second"
        jid = a.post("/api/restore", src=src, items=items, dest=str(first))["id"]
        a.wait_job(jid, until=lambda j: (j.get("done_files") or 0) >= 3)
        a.get("/api/job/cancel", id=jid); j = a.wait_job(jid)
        self.assertTrue(j["result"].get("paused"), j)
        other = a.post("/api/restore", src=src, items=items, dest=str(second))["id"]
        st, body = a.request("POST", "/api/restore/resume", body={"id": jid})
        running = a.get("/api/job", id=other)["state"] == "running"
        self.assertTrue(running, "the second restore finished before the check: raise the scale")
        self.assertEqual(st, 409, body); self.assertIn("already running", body.get("error", ""))
        self.assertEqual([x["state"] for x in a.get("/api/jobs/saved") if x["id"] == jid], ["paused"], "the refused resume must keep the saved restore")
        a.wait_job(other, timeout=600)
        j = a.wait_job(a.post("/api/restore/resume", id=jid)["id"], timeout=600)
        self.assertEqual(j["state"], "done", j.get("error")); self.assertEqual(j["errors"], [])
        for dest in (first, second):
            cmp_ = flow.compare(self.m, ver, {sh: dest / sh for sh in SHARES})
            for k in ("missing", "wrong_bytes", "wrong_mtime", "extra"): self.assertEqual(cmp_[k], [], f"{dest.name} {k}")


@unittest.skipUnless(E2E, "set HBK_E2E=1 to run the app-level end-to-end tests")
class LocalApiGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="hbk-e2e-guard-")); home = cls.work / "home"; home.mkdir()
        cls.app = App(home, cls.work / "app.log").start()

    @classmethod
    def tearDownClass(cls):
        cls.app.stop(); shutil.rmtree(cls.work, ignore_errors=True)

    def test_other_pages_are_refused_the_app_is_not(self):
        a = self.app; own = f"http://127.0.0.1:{a.port}"
        refused = {
            "DNS rebinding (Host of another site)": ("GET", "/api/sources", {"Host": f"evil.example:{a.port}"}),
            "Host with another port": ("GET", "/api/sources", {"Host": f"127.0.0.1:{a.port + 1}"}),
            "cross-site Origin": ("GET", "/api/sources", {"Origin": "http://evil.example"}),
            "cross-site fetch": ("GET", "/api/sources", {"Sec-Fetch-Site": "cross-site"}),
            "same-site page on another port": ("GET", "/api/sources", {"Origin": "http://127.0.0.1:1", "Sec-Fetch-Site": "same-site"}),
            "opaque origin (sandboxed iframe, file: page)": ("GET", "/api/sources", {"Origin": "null"}),
            "cross-site POST to add a source": ("POST", "/api/sources", {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}),
            "cross-site quit": ("GET", "/api/quit", {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}),
        }
        for why, (method, path, hdrs) in refused.items():
            body = {"name": "evil", "local": "/"} if method == "POST" else None
            st, out = a.request(method, path, hdrs, body)
            self.assertEqual(st, 403, f"{why}: {st} {out}")
        time.sleep(0.6)
        self.assertTrue(a.alive(), "a refused /api/quit must not stop the app")
        self.assertEqual(a.get("/api/sources"), [], "a refused POST must not add a source")
        allowed = {
            "command-line tool (no Origin)": ("GET", "/api/about", {}),
            "Host localhost": ("GET", "/api/about", {"Host": f"localhost:{a.port}"}),
            "the app's own page": ("GET", "/api/about", {"Origin": own, "Sec-Fetch-Site": "same-origin"}),
            "the app's own POST": ("POST", "/api/update/settings", {"Origin": own}),
            "typed into the address bar": ("GET", "/api/about", {"Sec-Fetch-Site": "none"}),
        }
        for why, (method, path, hdrs) in allowed.items():
            st, out = a.request(method, path, hdrs, {"check": True} if method == "POST" else None)
            self.assertEqual(st, 200, f"{why}: {st} {out}")


if __name__ == "__main__":
    unittest.main()
