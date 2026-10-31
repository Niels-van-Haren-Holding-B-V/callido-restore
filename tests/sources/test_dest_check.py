from pathlib import Path
"""Restore destination safety (T7/T8): the source is read-only, so the restore must never write into or around the .hbk.

``Source.contains_dest(dest, shares)`` checks the roots that actually get written, ``dest/<share>`` for every selected
share, against every local root and every source remote. It returns the offending written root or "". A restore back into
the original shares (dest = the NAS root) passes as long as the backup does not live inside one of the restored shares.
``/api/dest/check`` reports that verdict together with whether the folder exists and is empty (rclone mocked here).
"""
import json, os, subprocess, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from callido_restore import paths
from callido_restore.server import api as hbk_api
from callido_restore.server.api import H
from callido_restore.sources.source import SOURCES, Source


class SourceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = paths.INDEX_DIR; self.old_cache = paths.CACHE_DIR
        paths.INDEX_DIR = os.path.join(self.tmp.name, "index"); paths.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        paths.INDEX_DIR = Path(paths.INDEX_DIR); paths.CACHE_DIR = Path(paths.CACHE_DIR)
        self.local = os.path.join(self.tmp.name, "mirror", "backup_1.hbk"); os.makedirs(os.path.join(self.local, "Config"))
        self.extra = os.path.join(self.tmp.name, "vol", "backup_1.hbk"); os.makedirs(self.extra)
        self.src = Source("t7", "t7", self.local, ["nas:Backups/backup_1.hbk", "gdrive:Synology/backup_1.hbk"], [self.extra])
    def tearDown(self):
        paths.INDEX_DIR = self.old; paths.CACHE_DIR = self.old_cache; self.tmp.cleanup()


class ContainsDestTests(SourceFixture):
    def test_local_dest_alone(self):
        c = self.src.contains_dest
        self.assertTrue(c(self.local))                                             # the backup itself
        self.assertTrue(c(os.path.join(self.local, "Config")))                     # inside it
        self.assertTrue(c(os.path.join(self.extra, "Pool")))                       # inside an extra root
        self.assertTrue(c(self.local + "/"))                                       # trailing slash
        self.assertFalse(c(os.path.join(self.tmp.name, "restore-1234")))
        self.assertFalse(c(self.local + "-restore"))                               # sibling with a shared prefix
        self.assertFalse(c(self.local + "-copy"))
        self.assertFalse(c(os.path.dirname(self.local)))                           # a parent: nothing is written into the .hbk

    def test_local_written_roots(self):
        c = self.src.contains_dest
        parent = os.path.dirname(self.local)                                       # .../mirror, which holds backup_1.hbk
        self.assertEqual(c(parent, ["backup_1.hbk"]), os.path.join(parent, "backup_1.hbk"))      # would write into the backup
        self.assertEqual(c(os.path.dirname(parent), ["mirror"]), os.path.join(self.tmp.name, "mirror"))   # the backup lies inside the written root
        self.assertEqual(c(parent, ["homes", "Photos"]), "")                  # siblings of the backup are fine
        self.assertEqual(c(os.path.join(self.tmp.name, "restore"), ["homes"]), "")

    def test_remote_given_as_local_folder(self):
        folder = os.path.join(self.tmp.name, "usb", "backup_1.hbk"); os.makedirs(folder)
        src = Source("t7b", "t7b", os.path.join(self.tmp.name, "nonexistent-cache"), [folder])
        self.assertTrue(src.contains_dest(os.path.join(folder, "sub")))
        self.assertFalse(src.contains_dest(os.path.join(self.tmp.name, "usb", "restore")))

    def test_remote_dest_alone(self):
        c = self.src.contains_dest
        self.assertTrue(c("nas:Backups/backup_1.hbk"))
        self.assertTrue(c("nas:Backups/backup_1.hbk/Pool"))
        self.assertTrue(c("gdrive:Synology/backup_1.hbk/"))
        self.assertFalse(c("nas:Backups"))                                         # parent of the backup: allowed on its own
        self.assertFalse(c("nas:"))                                                # the NAS root (original-shares mode)
        self.assertFalse(c("nas:Restore/restore-1234"))
        self.assertFalse(c("nas:Backups/backup_1.hbk-restore"))
        self.assertFalse(c("other:Backups/backup_1.hbk"))                          # another remote, same path
        self.assertFalse(c("nas:share/folder"))

    def test_remote_written_roots(self):
        c = self.src.contains_dest
        # back into the original shares on the NAS that also holds the backup: only the share holding the backup is refused
        self.assertEqual(c("nas:", ["homes", "Photos", "Documents"]), "")
        self.assertEqual(c("nas:", ["homes", "Backups"]), "nas:Backups")
        self.assertEqual(c("nas:Backups", ["backup_1.hbk"]), "nas:Backups/backup_1.hbk")
        self.assertEqual(c("nas:Backups/", ["homes"]), "")                         # next to the .hbk, not into it
        self.assertEqual(c("nas:Restore/restore-1234", ["Backups"]), "")
        self.assertEqual(c("gdrive:Synology", ["backup_1.hbk"]), "gdrive:Synology/backup_1.hbk")
        self.assertEqual(c("gdrive:", ["Synology"]), "gdrive:Synology")


def fake_rclone(stdout="", stderr="", rc=0):
    def run(args, **kw):
        return subprocess.CompletedProcess(args=args, returncode=rc, stdout=stdout, stderr=stderr)
    return run


class DestCheckApiTests(SourceFixture):
    def setUp(self):
        super().setUp()
        SOURCES["t7"] = self.src
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); SOURCES.pop("t7", None); super().tearDown()
    def check(self, **q):
        with urlopen(f"http://127.0.0.1:{self.srv.server_address[1]}/api/dest/check?" + urlencode(q)) as r:
            return json.loads(r.read())

    def test_remote_new_folder(self):
        with mock.patch.object(subprocess, "run", fake_rclone(stderr="2026/10/30 23:00:00 ERROR : : error listing: directory not found\nFailed to lsjson: directory not found", rc=3)):
            out = self.check(src="t7", dest="nas:Restore/restore-1234", shares=json.dumps(["homes"]))
        self.assertEqual((out["inside_source"], out["exists"], out.get("error")), (False, False, None))

    def test_remote_unknown_remote_is_an_error_not_a_new_folder(self):
        with mock.patch.object(subprocess, "run", fake_rclone(stderr="Failed to create file system for \"nsa:Restore\": didn't find section in config file (\"nsa\")", rc=1)):
            out = self.check(src="t7", dest="nsa:Restore", shares="[]")
        self.assertFalse(out["exists"]); self.assertIn("didn't find section", out["error"])

    def test_remote_existing_with_entries(self):
        listing = json.dumps([{"Name": "homes", "IsDir": True}, {"Name": "x.txt", "IsDir": False}])
        with mock.patch.object(subprocess, "run", fake_rclone(stdout=listing)):
            out = self.check(src="t7", dest="nas:Restore", shares=json.dumps(["homes"]))
        self.assertEqual((out["exists"], out["empty"], out["files"], out["inside_source"]), (True, False, 2, False))

    def test_remote_inside_source_by_share(self):
        with mock.patch.object(subprocess, "run", fake_rclone(stdout="[]")):
            ok = self.check(src="t7", dest="nas:", shares=json.dumps(["homes"]))
            bad = self.check(src="t7", dest="nas:", shares=json.dumps(["homes", "Backups"]))
        self.assertFalse(ok["inside_source"]); self.assertTrue(bad["inside_source"]); self.assertEqual(bad["conflict"], "nas:Backups")

    def test_local_folders(self):
        new = os.path.join(self.tmp.name, "new"); empty = os.path.join(self.tmp.name, "empty"); os.makedirs(empty)
        full = os.path.join(self.tmp.name, "full"); os.makedirs(full); open(os.path.join(full, "a"), "w").close()
        self.assertEqual(self.check(src="t7", dest=new)["exists"], False)
        self.assertEqual(self.check(src="t7", dest=empty)["empty"], True)
        out = self.check(src="t7", dest=full); self.assertEqual((out["empty"], out["files"]), (False, 1))
        out = self.check(src="t7", dest=self.local, shares=json.dumps(["homes"])); self.assertTrue(out["inside_source"])
        self.assertEqual(self.check(src="t7", dest="")["error"], "no destination")
        out = self.check(src="t7", dest=os.path.join(self.tmp.name, "nope", "new"))     # parent missing: /api/restore would refuse it too
        self.assertFalse(out["exists"]); self.assertIn("does not exist", out["error"])

    def test_ls_remote_is_one_rclone_call(self):                           # a Drive listing takes about a second; one call per click
        calls = []
        def run(args, **kw):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([{"Name": "backup_1.hbk", "IsDir": True}, {"Name": "Config", "IsDir": True}, {"Name": "notes.txt", "IsDir": False}, {"Name": "Pool", "IsDir": True}]), stderr="")
        with mock.patch.object(hbk_api.subprocess, "run", run):
            with urlopen(f"http://127.0.0.1:{self.srv.server_address[1]}/api/rclone/ls?" + urlencode({"remote": "gdrive:", "path": "Synology"})) as r: out = json.loads(r.read())
        self.assertEqual(len(calls), 1); self.assertIn("--max-depth", calls[0]); self.assertNotIn("--dirs-only", calls[0])
        self.assertEqual(out["dirs"], ["Config", "Pool", "backup_1.hbk"]); self.assertEqual(out["files"], ["notes.txt"])
        self.assertTrue(out["looks_like_hbk"]); self.assertEqual(out["hbk_dirs"], ["backup_1.hbk"])

    def test_connect_password_goes_over_stdin_and_post_only(self):
        calls = []
        def run(args, **kw):
            calls.append((args, kw.get("input")))
            if args[1] == "obscure": return subprocess.CompletedProcess(args, 0, stdout="OBSCURED\n", stderr="")
            if args[1] == "config": return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([{"Name": "Restore"}]), stderr="")
        body = json.dumps({"kind": "smb", "name": "nas", "host": "192.0.2.10", "user": "restore", "password": "s3cret pw"}).encode()
        base = f"http://127.0.0.1:{self.srv.server_address[1]}/api/rclone/connect"
        with mock.patch.object(subprocess, "run", run):
            with urlopen(Request(base, data=body, headers={"Content-Type": "application/json"})) as r: job = json.loads(r.read())
            for _ in range(50):
                with urlopen(f"http://127.0.0.1:{self.srv.server_address[1]}/api/job?id={job['id']}") as r: j = json.loads(r.read())
                if j["state"] != "running": break
                time.sleep(0.05)
        self.assertEqual(j["state"], "done", j); self.assertEqual(j["result"]["shares"], ["Restore"])
        obscure = [c for c in calls if c[0][1] == "obscure"][0]
        self.assertEqual(obscure[0][2], "-"); self.assertEqual(obscure[1], "s3cret pw\n")             # stdin, not argv
        create = [c for c in calls if c[0][1] == "config"][0][0]
        self.assertIn("pass=OBSCURED", create); self.assertNotIn("s3cret pw", " ".join(create))
        try:
            urlopen(base + "?kind=smb&password=x"); self.fail("GET must be refused")
        except HTTPError as e:
            self.assertEqual(e.code, 405)

    def test_connect_drive_with_own_key_recreates_the_remote_and_clears_the_daemon_caches(self):
        """A Google key typed for an existing connection re-creates that remote under the same name with client id and secret
        (rclone config create replaces an existing section and runs the sign-in); the daemon's listing cache and backend cache
        are dropped so the next call uses the new key."""
        calls = []
        def run(args, **kw):
            calls.append(args)
            if args[1] == "config": return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([{"Name": "Synology"}]), stderr="")
        body = json.dumps({"kind": "drive", "name": "gdrive", "client_id": "123.apps.googleusercontent.com", "client_secret": "GOCSPX-x"}).encode()
        base = f"http://127.0.0.1:{self.srv.server_address[1]}/api/rclone/connect"
        from callido_restore.sources import rclone
        with mock.patch.object(subprocess, "run", run), mock.patch.object(rclone, "forget") as forget, mock.patch.object(rclone, "fscache_clear") as clear:
            with urlopen(Request(base, data=body, headers={"Content-Type": "application/json"})) as r: job = json.loads(r.read())
            for _ in range(50):
                with urlopen(f"http://127.0.0.1:{self.srv.server_address[1]}/api/job?id={job['id']}") as r: j = json.loads(r.read())
                if j["state"] != "running": break
                time.sleep(0.05)
        self.assertEqual(j["state"], "done", j); self.assertEqual(j["result"], {"remote": "gdrive:", "shares": ["Synology"]})
        create = [c for c in calls if c[1] == "config"][0]
        self.assertEqual(create[1:5], ["config", "create", "gdrive", "drive"])
        self.assertIn("client_id=123.apps.googleusercontent.com", create); self.assertIn("client_secret=GOCSPX-x", create)
        forget.assert_called_once(); clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
