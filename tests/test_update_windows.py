"""Windows auto-update step (update_windows): the MSI is fetched into the app cache only when its sha256 matches the release's
.sha256, only from GitHub's hosts, and the helper waits for the app, runs msiexec /passive and starts the app again."""
import hashlib, http.server, os, tempfile, threading, unittest
from pathlib import Path
from unittest import mock

from callido_restore import paths, update_windows as uw

MSI = b"MSI-BYTES" * 1000


class _Handler(http.server.BaseHTTPRequestHandler):
    files = {}
    def do_GET(self):
        body = self.files.get(self.path)
        if body is None: self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass


class Download(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.old = paths.CACHE_DIR; paths.CACHE_DIR = Path(self.tmp.name) / "cache"
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        # the test server stands in for github.com: allow its address for the duration of the test
        self.hosts = mock.patch.object(uw, "ALLOWED_HOSTS", (self.base + "/",))
        self.hosts.start()
        _Handler.files = {"/msi": MSI, "/sum": f"{hashlib.sha256(MSI).hexdigest()}  {uw.MSI_NAME}\n".encode()}
    def tearDown(self):
        self.hosts.stop(); self.srv.shutdown(); self.srv.server_close(); paths.CACHE_DIR = self.old; self.tmp.cleanup()
    def release(self, tag="v1.2.3", msi="/msi", sum_="/sum"):
        a = [{"name": uw.MSI_NAME, "browser_download_url": self.base + msi, "size": len(MSI)}]
        if sum_: a.append({"name": uw.SUM_NAME, "browser_download_url": self.base + sum_})
        return {"tag_name": tag, "assets": a}

    def test_downloads_into_the_cache_and_checks_the_sum(self):
        seen = []
        p = uw.download(self.release(), progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(p, paths.CACHE_DIR / "updates" / "1.2.3" / uw.MSI_NAME); self.assertEqual(p.read_bytes(), MSI)
        self.assertEqual(seen[-1], (len(MSI), len(MSI)))
        self.assertFalse(any(x.name.endswith(".part") for x in p.parent.iterdir()))

    def test_a_good_file_is_reused(self):
        p = uw.download(self.release()); _Handler.files.pop("/msi")
        self.assertEqual(uw.download(self.release()), p)

    def test_mismatch_is_refused_and_nothing_is_kept(self):
        _Handler.files["/sum"] = ("0" * 64 + f"  {uw.MSI_NAME}\n").encode()
        with self.assertRaises(RuntimeError) as c: uw.download(self.release())
        self.assertIn("does not match", str(c.exception))
        self.assertEqual(list((paths.CACHE_DIR / "updates" / "1.2.3").iterdir()), [])

    def test_missing_assets_and_bad_versions(self):
        with self.assertRaises(RuntimeError): uw.download(self.release(sum_=None))
        with self.assertRaises(RuntimeError): uw.download(self.release(tag="nightly"))

    def test_only_github_hosts(self):
        self.hosts.stop()
        try:
            with self.assertRaises(RuntimeError) as c: uw.download(self.release())
            self.assertIn("not served from github.com", str(c.exception))
        finally: self.hosts.start()

    def test_cancel(self):
        with self.assertRaises(RuntimeError): uw.download(self.release(), cancelled=lambda: True)
        self.assertFalse((paths.CACHE_DIR / "updates" / "1.2.3" / uw.MSI_NAME).exists())


class Helper(unittest.TestCase):
    def test_command(self):
        cmd = uw.helper_command(r"C:\Users\x\.hbk-explorer\cache\updates\1.2.3\CallidoRestore-x64.msi",
                                r"C:\Program Files\Callido\Callido Restore\Callido Restore.exe", 4321, r"C:\log's.txt")
        self.assertEqual(cmd[0], "powershell.exe"); script = cmd[-1]
        self.assertTrue(script.startswith("Wait-Process -Id 4321"))
        self.assertIn("'/i', 'C:\\Users\\x\\.hbk-explorer\\cache\\updates\\1.2.3\\CallidoRestore-x64.msi', '/passive', '/norestart'", script)
        self.assertIn("'C:\\log''s.txt'", script)                                  # single quotes doubled for PowerShell
        self.assertTrue(script.endswith("Start-Process -FilePath 'C:\\Program Files\\Callido\\Callido Restore\\Callido Restore.exe'"))

    def test_install_only_on_windows_packaged(self):
        with mock.patch.object(uw.oscompat, "WINDOWS", False), self.assertRaises(RuntimeError): uw.install_and_relaunch("x.msi", exit_app=lambda c: None)
        with mock.patch.object(uw.oscompat, "WINDOWS", True), mock.patch.object(uw.sys, "frozen", False, create=True), \
             self.assertRaises(RuntimeError): uw.install_and_relaunch("x.msi", exit_app=lambda c: None)

    def test_install_starts_the_helper_then_exits(self):
        with tempfile.TemporaryDirectory() as d:
            msi = Path(d) / "1.2.3" / uw.MSI_NAME; msi.parent.mkdir(); msi.write_bytes(b"x"); exits = []
            with mock.patch.object(uw.oscompat, "WINDOWS", True), mock.patch.object(uw.sys, "frozen", True, create=True), \
                 mock.patch.object(uw.subprocess, "Popen") as popen, mock.patch.object(uw.time, "sleep"):
                uw.install_and_relaunch(msi, exit_app=exits.append)
            self.assertEqual(exits, [0]); args, kw = popen.call_args
            self.assertEqual(args[0][0], "powershell.exe"); self.assertTrue(kw["creationflags"] & uw.DETACHED_PROCESS)


if __name__ == "__main__":
    unittest.main()
