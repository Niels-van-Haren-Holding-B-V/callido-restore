"""End to end through the real app process and its HTTP API, the way the UI drives it: add a local backup, open it as a job,
load the file lists of a run (which starts the preparation by itself, in the detached mapper child), wait for it, check the
destination, count, restore with a pause and a resume, download one file, quit.

Slow-ish (a ~60 MB backup, a few seconds) and it starts a server: only with HBK_E2E=1. The app runs with HOME pointed at a
temporary folder, so its settings folder, the mapper child and the rclone daemon never see the user's ~/.hbk-explorer.
Port: HBK_SMOKE_PORT if set, else a free one (never 8765, the running app's default)."""
import json, os, shutil, socket, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.e2e import flow, hbkgen

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


@unittest.skipUnless(os.environ.get("HBK_E2E") == "1", "set HBK_E2E=1 to run the app-level end-to-end test")
class AppFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="hbk-e2e-app-")); cls.home = cls.work / "home"; cls.home.mkdir()
        cls.hbk = cls.work / "backups" / "e2e_1.hbk"
        cls.ci = os.environ.get("HBK_E2E_CHUNK_INDEX") == "1"      # the backup carries its own chunk table (Pool/chunk_index)
        cls.m = hbkgen.build(str(cls.hbk), scale=float(os.environ.get("HBK_E2E_SCALE", "6")), bucket_cap=1 << 20, chunk_index=cls.ci)
        port = int(os.environ.get("HBK_SMOKE_PORT") or free_port())
        if port == 8765: raise unittest.SkipTest("refusing port 8765: that is the user's running app")
        cls.base = f"http://127.0.0.1:{port}"
        env = dict(os.environ, HOME=str(cls.home), USERPROFILE=str(cls.home), PYTHONPATH=str(ROOT)); env.pop("HBK_NO_AUTOPREPARE", None)
        cls.log = open(cls.work / "app.log", "w")
        cls.p = subprocess.Popen([sys.executable, "-m", "callido_restore", "--no-window", "--port", str(port)], cwd=str(ROOT), env=env,
                                 stdout=cls.log, stderr=subprocess.STDOUT)
        for _ in range(150):
            try: cls.get("/api/about"); break
            except OSError: time.sleep(0.1)
        else: raise RuntimeError("the app did not start: " + (cls.work / "app.log").read_text()[-2000:])

    @classmethod
    def tearDownClass(cls):
        try: cls.get("/api/quit")
        except Exception: pass
        try: cls.p.wait(10)
        except subprocess.TimeoutExpired: cls.p.kill()
        cls.log.close()
        if os.environ.get("HBK_E2E_KEEP") == "1": print("\nkept:", cls.work)
        else: shutil.rmtree(cls.work, ignore_errors=True)

    @classmethod
    def get(cls, path, **q):
        with urlopen(f"{cls.base}{path}?{urlencode(q)}", timeout=60) as r: return json.loads(r.read() or b"null")

    @classmethod
    def raw(cls, path, **q):
        with urlopen(f"{cls.base}{path}?{urlencode(q)}", timeout=60) as r: return r.read()

    @classmethod
    def post(cls, path, **body):
        req = Request(f"{cls.base}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=60) as r: return json.loads(r.read())

    def wait_job(self, jid, timeout=300, until=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = self.get("/api/job", id=jid)
            if j.get("state") != "running" or (until and until(j)): return j
            time.sleep(0.1)
        self.fail(f"job {jid} still running after {timeout}s: {j.get('message')}")

    S = {}                                   # state handed from one step to the next (unittest runs them in name order)

    def test_1_open_and_prepare(self):
        m = self.m; ver = m["last_clean"]; S = self.S
        # the wizard's "local folder" source, opened as a job
        src = S["src"] = self.post("/api/sources", name="E2E backup", local=str(self.hbk))["id"]
        j = self.wait_job(self.get("/api/open", src=src, job="1")["id"]); self.assertEqual(j["state"], "done", j)
        info = self.get("/api/open", src=src)
        self.assertTrue(info["readable"]); self.assertEqual([s["name"] for s in info["shares"]], ["Documents", "Photos", "homes"])
        self.assertEqual(max(v["id"] for v in info["versions"]), m["incident"])
        # a local backup has every snapshot .db on disk already, so the preparation may start right at open;
        # loading the file lists of the last clean run starts it otherwise
        # the Restore step loads just the selected shares whose list is missing (a JSON list), then "*" loads the rest
        j = self.wait_job(self.get("/api/index", src=src, shares=json.dumps(["Photos", "homes"]), ver=ver)["id"])
        self.assertEqual([x["share"] for x in j["result"]], ["Photos", "homes"]); self.assertTrue(all(x["ok"] for x in j["result"]), j["result"])
        j = self.wait_job(self.get("/api/index", src=src, share="*", ver=ver)["id"])
        self.assertTrue(all(x["ok"] for x in j["result"]), j["result"])
        # unknown or malformed share names are refused before any path is built
        for bad in ({"share": "../etc"}, {"shares": json.dumps(["Documents", "nope"])}, {"shares": json.dumps("Documents")}, {"shares": "[]"}):
            with self.assertRaises(HTTPError) as cm: self.get("/api/index", src=src, ver=ver, **bad)
            self.assertEqual(cm.exception.code, 400, bad); self.assertIn("error", json.loads(cm.exception.read()))
        st = self.get("/api/pool", src=src); self.assertTrue(st["running"] or st["complete"], f"preparation did not start: {st}")
        if st["running"]: pj = self.wait_job(st["running"], timeout=600); self.assertEqual(pj["state"], "done", pj.get("error") or pj)
        st = self.get("/api/pool", src=src)
        if self.ci: self.assertTrue(st["complete"] and st.get("chunk_index"), st)             # no map needed: the table locates every chunk
        else: self.assertTrue(st["complete"] and st["map"], st)
        kids = self.get("/api/children", src=src, share="Documents", ver=ver, dir="")
        self.assertIn("Finance", [k["name"] for k in kids])
        S["ready"] = True

    def test_2_destination_checks_and_count(self):
        S = self.S; self.assertTrue(S.get("ready")); src = S["src"]; m = self.m; ver = m["last_clean"]
        S["dest"] = dest = self.work / "restore"
        chk = self.get("/api/dest/check", src=src, dest=str(self.hbk / "x"), shares=json.dumps(["Documents"])); self.assertTrue(chk["inside_source"], chk)
        chk = self.get("/api/dest/check", src=src, dest=str(dest), shares=json.dumps(["Documents"])); self.assertFalse(chk["inside_source"]); self.assertFalse(chk["exists"])
        S["items"] = items = [{"share": sh, "ver": ver, "path": "", "is_dir": True} for sh in ("Documents", "Photos", "homes")]
        want_files = sum(len(v) for v in m["versions"][str(ver)].values()); want_bytes = sum(e["size"] for v in m["versions"][str(ver)].values() for e in v.values())
        cnt = self.get("/api/restore_count", src=src, items=json.dumps(items)); self.assertEqual((cnt["files"], cnt["bytes"]), (want_files, want_bytes))
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError, msg="a restore into the backup itself must be refused") as cm:
            self.post("/api/restore", src=src, items=items, dest=str(self.hbk))
        self.assertEqual(cm.exception.code, 400); cm.exception.close()

    def test_3_restore_pause_resume(self):
        S = self.S; self.assertIn("items", S); src = S["src"]; m = self.m; ver = m["last_clean"]; dest = S["dest"]
        jid = self.post("/api/restore", src=src, items=S["items"], dest=str(dest))["id"]
        j = self.wait_job(jid, until=lambda j: (j.get("done_files") or 0) + len(j.get("errors") or []) >= 3)
        S["parts"] = []
        if j["state"] == "running":
            self.get("/api/job/cancel", id=jid); j = self.wait_job(jid)
            self.assertTrue(j["result"].get("paused"), j); S["parts"].append(j)
            saved = [s for s in self.get("/api/jobs/saved") if s["id"] == jid]; self.assertEqual(saved[0]["state"], "paused", saved)
            j = self.wait_job(self.post("/api/restore/resume", id=jid)["id"])
        self.assertEqual(j["state"], "done", j.get("error")); S["parts"].append(j); S["final"] = j
        S["cmp"] = flow.compare(m, ver, {sh: dest / sh for sh in ("Documents", "Photos", "homes")})
        for k in ("wrong_bytes", "wrong_mtime", "extra"): self.assertEqual(S["cmp"][k], [], k)   # whatever was written is right

    def test_4_final_report_names_every_missing_file(self):
        """E3 (fixed in #117): a resumed restore used to start with empty errors and zero counts, so files that failed
        before the pause vanished from the final report. Every missing file is an error, once, and the counts add up."""
        S = self.S; self.assertIn("final", S)
        if len(S["parts"]) < 2: self.skipTest("the restore finished before the pause landed (raise HBK_E2E_SCALE)")
        reported = [e["path"] for e in S["final"]["errors"]]
        self.assertEqual(len(reported), len(set(reported)), "an error is recorded once per file")
        reported = set(reported)
        missing = {x.rsplit(" (", 1)[0] for x in S["cmp"]["missing"]}
        self.assertTrue(missing <= reported, f"missing but not reported: {sorted(missing - reported)}")
        self.assertEqual(S["final"]["done_files"] + len(S["final"]["errors"]), S["final"]["total_files"])

    def test_5_everything_restored(self):
        """E1 + E4 (fixed in #118): at this file/bucket ratio records used to stay unplaced."""
        self.assertEqual(self.S["cmp"]["missing"], [])

    @unittest.skipUnless(os.environ.get("HBK_E2E_STRESS") == "1", "HBK_E2E_STRESS=1: large files over many buckets (E5)")
    @unittest.expectedFailure
    def test_7_large_files_over_many_buckets(self):
        """hbk-6 finding E5: once files span ~40+ buckets while the four writers interleave, interior claims become
        genuinely ambiguous (several exact count+byte solutions) and the placer declines. Scale 20 over 1 MiB buckets:
        13/24 records, 29.8 % of bytes. Never wrong bytes, only missing files."""
        work = tempfile.mkdtemp(prefix="hbk-e2e-stress-")
        try:
            rep = flow.run_inprocess(work, scale=20, bucket_kb=1024)
            for ver, r in rep["restores"].items():
                self.assertEqual(r["wrong_bytes"], [], ver)
                self.assertEqual(r["missing"], [], ver)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_6_single_file_download(self):
        S = self.S; self.assertIn("src", S); src = S["src"]; m = self.m; ver = m["last_clean"]
        rows = self.get("/api/query", src=src, share="Photos", ver=ver, q="IMG_4102")
        rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
        f = next(r for r in rows if r["name"] == "IMG_4102.jpg")
        data = self.raw("/api/download", src=src, share="Photos", ver=ver, id=f["id"])
        import hashlib
        self.assertEqual(hashlib.sha256(data).hexdigest(), m["versions"][str(ver)]["Photos"]["2025/Summer/IMG_4102.jpg"]["sha256"])

if __name__ == "__main__":
    unittest.main()
