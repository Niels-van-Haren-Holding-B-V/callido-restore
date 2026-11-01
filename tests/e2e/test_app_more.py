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
        self.assertEqual(j["state"], "done", j.get("error")); self.assertEqual(j["errors"], [])
        total = sum(len(v) for v in self.m["versions"][str(ver)].values())
        self.assertEqual((j["result"]["skipped"], j["result"]["restored"]), (1, total - 1), j["result"])
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
        self.assertEqual(j["result"]["restored"], 0, j["result"]); self.assertEqual(j["result"]["skipped"], j["result"]["files"])
        self.assertEqual(tree(self.nas), before)

    def test_4_the_backup_is_only_read(self):
        self.assertIn("src", self.S)
        self.assertEqual(tree(self.drive), self.before, "nothing in the backup on the remote may change")
        cache = Path(self.app.home, ".hbk-explorer", "cache", self.S["src"])
        fetched = [p for p in cache.rglob("*.bucket.*")] if cache.is_dir() else []
        self.assertTrue(fetched, f"bucket data should have been fetched into the app cache {cache}")


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
