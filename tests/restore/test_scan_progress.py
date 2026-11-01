"""Opening a backup reports its steps (the first open of a remote backup lists folders and fetches small files over rclone,
which takes seconds per step): ``Backup(src, log)`` calls ``log`` per step, and ``/api/open?job=1`` runs the scan as a job whose
message follows those steps, after which a plain ``/api/open`` returns the info."""
import json, os, tempfile, threading, time, unittest
from callido_restore.server.api import LocalServer
from pathlib import Path
from urllib.request import urlopen

from callido_restore import paths
from callido_restore.restore.backup import Backup
from callido_restore.server.api import BACKUPS, H
from callido_restore.sources.source import SOURCES, Source


class ScanProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); t = self.tmp.name
        self.saved = (paths.INDEX_DIR, paths.CACHE_DIR)
        paths.INDEX_DIR = Path(t) / "index"; paths.CACHE_DIR = Path(t) / "cache"
        root = Path(t) / "backup_1.hbk"                                  # a backup folder without a version database: scans, but "not readable"
        for sh in ("Documents", "Photos"): (root / "Config" / "@Share" / sh).mkdir(parents=True)
        (root / "Pool").mkdir(); (root / "_Syno_TaskConfig").write_text("[task]\nname=\"Backup 1\"\n")
        self.src = Source("scan", "scan", str(root))
    def tearDown(self):
        paths.INDEX_DIR, paths.CACHE_DIR = self.saved; self.tmp.cleanup()

    def test_scan_reports_each_step(self):
        msgs = []
        b = Backup(self.src, msgs.append)
        self.assertEqual(msgs[0], "Listing the backup folder…")
        self.assertIn("Listing shares 2/2…", msgs); self.assertIn("Reading the task info…", msgs); self.assertIn("Reading the version list…", msgs)
        self.assertIn("Reading the snapshot list of share 2/2…", msgs)
        self.assertFalse(b.info["readable"]); self.assertTrue(any("not a Hyper Backup root" in p for p in b.info["problems"]))
        self.assertEqual([s["name"] for s in b.info["shares"]], ["Documents", "Photos"])
        Backup(self.src)                                                 # no log: still fine

    def test_open_as_job_then_info(self):
        SOURCES["scan"] = self.src; BACKUPS.pop("scan", None)
        srv = LocalServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            with urlopen(base + "/api/open?src=scan&job=1") as r: j = json.loads(r.read())
            for _ in range(100):
                with urlopen(base + f"/api/job?id={j['id']}") as r: job = json.loads(r.read())
                if job["state"] != "running": break
                time.sleep(0.02)
            self.assertEqual(job["state"], "done", job); self.assertEqual(job["label"], "open backup")
            with urlopen(base + "/api/open?src=scan") as r: info = json.loads(r.read())
            self.assertEqual([s["name"] for s in info["shares"]], ["Documents", "Photos"]); self.assertIn("scan", BACKUPS)
            try: urlopen(base + "/api/open?src=nope&job=1"); self.fail("unknown source must be refused")
            except Exception as e: self.assertEqual(e.code, 404)
        finally:
            srv.shutdown(); srv.server_close(); SOURCES.pop("scan", None); BACKUPS.pop("scan", None)


if __name__ == "__main__":
    unittest.main()
