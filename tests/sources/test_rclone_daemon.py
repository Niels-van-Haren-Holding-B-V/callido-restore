"""The shared rclone client: subprocess fallback when the daemon is not running, remote-control calls when it is, and (when an
rclone binary is available) a real daemon round trip on a local folder."""
import json, os, re, shutil, subprocess, tempfile, unittest
from unittest import mock

from callido_restore.sources import rclone as rc


class Fallback(unittest.TestCase):
    """Without a daemon the helpers run the same rclone command lines as before."""
    def setUp(self):
        self.assertFalse(rc.DAEMON.running)

    def test_ls_builds_the_old_command_line(self):
        ok = subprocess.CompletedProcess([], 0, stdout=json.dumps([{"Path": "a", "Name": "a", "IsDir": True, "Size": -1}]), stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok) as run:
            out = rc.ls("nas:share", dirs_only=True, max_depth=1, no_modtime=True)
        self.assertEqual(out[0]["Name"], "a")
        self.assertEqual(run.call_args[0][0][1:], ["lsjson", "--no-mimetype", "--no-modtime", "--dirs-only", "--max-depth", "1", "nas:share"])

    def test_ls_failure_raises_with_rclone_text(self):
        bad = subprocess.CompletedProcess([], 1, stdout="", stderr="directory not found")
        with mock.patch.object(subprocess, "run", return_value=bad):
            with self.assertRaises(rc.RcloneError) as cm: rc.ls("nas:missing")
        self.assertIn("directory not found", str(cm.exception))

    def test_remote_info_falls_back_to_config_dump(self):
        dump = {"nas": {"type": "smb", "host": "nas"}, "gdrive": {"type": "drive", "client_id": "1-a.apps.googleusercontent.com", "token": "{}"}}
        ok = subprocess.CompletedProcess([], 0, stdout=json.dumps(dump), stderr="")
        with mock.patch.object(rc.DAEMON, "available", return_value=False), mock.patch.object(subprocess, "run", return_value=ok) as run:
            self.assertEqual(rc.remote_info(), {"nas:": {"type": "smb", "own_key": False}, "gdrive:": {"type": "drive", "own_key": True}})
        self.assertEqual(run.call_args[0][0][1:3], ["config", "dump"])

    def test_remotes_parses_listremotes_long(self):
        ok = subprocess.CompletedProcess([], 0, stdout="nas: smb\ngdrive: drive\n", stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok):
            self.assertEqual(rc.remotes(), [("nas:", "smb"), ("gdrive:", "drive")])


class Cache(unittest.TestCase):
    def setUp(self): rc.forget()
    def tearDown(self): rc.forget()

    def test_repeat_listing_is_served_from_cache_until_forgotten(self):
        ok = subprocess.CompletedProcess([], 0, stdout=json.dumps([{"Path": "a", "Name": "a", "IsDir": True, "Size": -1}]), stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok) as run:
            rc.ls("nas:", dirs_only=True, cache=True); rc.ls("nas:", dirs_only=True, cache=True)
            self.assertEqual(run.call_count, 1)
            rc.ls("nas:", dirs_only=True)                      # uncached call always asks rclone
            self.assertEqual(run.call_count, 2)
            rc.forget("nas:"); rc.ls("nas:", dirs_only=True, cache=True)
            self.assertEqual(run.call_count, 3)

    def test_cache_expires(self):
        ok = subprocess.CompletedProcess([], 0, stdout="nas: smb\n", stderr="")
        with mock.patch.object(subprocess, "run", return_value=ok) as run, mock.patch.object(rc, "CACHE_TTL", 0.0):
            rc.remotes(cache=True); rc.remotes(cache=True)
            self.assertEqual(run.call_count, 2)


class StaleDaemon(unittest.TestCase):
    """Pid files from killed app runs: a daemon is stopped only when its recorded parent is gone and the pid still runs rclone rcd."""
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        from callido_restore import paths
        p = mock.patch.object(paths, "CONF_DIR", __import__("pathlib").Path(self.d)); p.start(); self.addCleanup(p.stop)
        self.dir = __import__("pathlib").Path(self.d)

    def _pidfile(self, name, pid, parent):
        (self.dir / name).write_text(json.dumps({"pid": pid, "parent": parent}))

    def test_no_pid_file(self):
        self.assertEqual(rc.kill_stale(), 0)

    def test_running_instance_is_left_alone(self):
        self._pidfile("rclone-rcd-777.pid", 4242, 777)
        with mock.patch.object(rc, "_alive", return_value=True), mock.patch.object(os, "kill") as kill:
            self.assertEqual(rc.kill_stale(), 0); kill.assert_not_called()
        self.assertTrue((self.dir / "rclone-rcd-777.pid").exists())

    def test_pid_reused_by_another_program_is_left_alone(self):
        self._pidfile("rclone-rcd-777.pid", 4242, 777)
        with mock.patch.object(rc, "_alive", return_value=False), mock.patch.object(rc, "_cmdline", return_value="/usr/bin/vim notes.txt"), \
             mock.patch.object(os, "kill") as kill:
            self.assertEqual(rc.kill_stale(), 0); kill.assert_not_called()
        self.assertFalse((self.dir / "rclone-rcd-777.pid").exists())

    def test_orphan_rcd_is_stopped(self):
        self._pidfile("rclone-rcd-777.pid", 4242, 777)
        with mock.patch.object(rc, "_alive", return_value=False), \
             mock.patch.object(rc, "_cmdline", return_value="/Applications/X.app/Contents/Frameworks/rclone rcd --rc-addr 127.0.0.1:5"), \
             mock.patch.object(os, "kill") as kill:
            self.assertEqual(rc.kill_stale(), 1); kill.assert_called_once_with(4242, 15)

    def test_rcd_that_ignores_terminate_is_killed(self):
        self._pidfile("rclone-rcd-777.pid", 4242, 777)
        alive = lambda pid: pid == 4242                        # parent gone, the daemon survives SIGTERM
        with mock.patch.object(rc, "_alive", side_effect=alive), mock.patch.object(rc.time, "sleep"), \
             mock.patch.object(rc, "_cmdline", return_value="/Applications/X.app/Contents/Frameworks/rclone rcd --rc-addr 127.0.0.1:5"), \
             mock.patch.object(os, "kill") as kill:
            self.assertEqual(rc.kill_stale(), 1)
        self.assertEqual(kill.call_args_list, [mock.call(4242, 15), mock.call(4242, 9)])

    def test_old_format_file_is_dropped(self):
        (self.dir / "rclone-rcd-1.pid").write_text("4242")
        with mock.patch.object(os, "kill") as kill:
            self.assertEqual(rc.kill_stale(), 0); kill.assert_not_called()
        self.assertFalse((self.dir / "rclone-rcd-1.pid").exists())

    def test_own_pidfile_is_written_and_removed(self):
        if not (rc.RCLONE and shutil.which(rc.RCLONE)): self.skipTest("needs an rclone binary")
        self.assertTrue(rc.start(timeout=15)); self.addCleanup(rc.stop)
        f = rc.PIDFILE(); self.assertTrue(f.exists()); self.assertEqual(json.loads(f.read_text())["parent"], os.getpid())
        rc.stop(); self.assertFalse(f.exists())


class Orphans(unittest.TestCase):
    """rclone processes of a crashed app: the bundled binary with a dead parent is stopped, anything else is left alone."""
    ROWS = [(500, 1, "/Volumes/Callido Restore/Callido Restore.app/Contents/Frameworks/rclone"),                  # the crashed app's copy
            (501, 1, "/Volumes/Callido Restore/Callido Restore.app/Contents/Frameworks/rclone"),                  # and its daemon
            (502, 300, "/Applications/Callido Restore.app/Contents/Frameworks/rclone"),                          # another running app (parent alive)
            (503, 1, "/opt/homebrew/bin/rclone"),                                                                # not ours
            (504, 1, "/Applications/Other.app/Contents/Frameworks/rclone"),
            (505, 1, "/usr/bin/xattr"),                                                                          # a launchd job naming our rclone as an argument
            (506, 1, "/Applications/Callido Restore.app/Contents/MacOS/Callido Restore")]                        # the app itself, not its rclone
    def test_only_bundled_rclone_with_dead_parent_is_stopped(self):
        with mock.patch.object(rc, "_ps", return_value=self.ROWS), mock.patch.object(rc, "_alive", return_value=False), \
             mock.patch.object(os, "kill") as kill:
            self.assertEqual(rc.kill_orphans(), 2)
        self.assertEqual(kill.call_args_list, [mock.call(500, 15), mock.call(501, 15)])

    @unittest.skipIf(os.name == "nt", "no ps on Windows: _ps() returns [] there by design")
    def test_ps_rows_are_parsed(self):
        out = subprocess.CompletedProcess([], 0, stdout="  500     1 /Applications/Callido Restore.app/Contents/Frameworks/rclone\n  bad line\n", stderr="")
        with mock.patch.object(subprocess, "run", return_value=out) as run:
            self.assertEqual(rc._ps(), [(500, 1, "/Applications/Callido Restore.app/Contents/Frameworks/rclone")])
        self.assertEqual(run.call_args[0][0], ["ps", "-axo", "pid=,ppid=,comm="])                                  # the executable, not the command line


class DeadDaemon(unittest.TestCase):
    """A daemon that was killed: the helpers fall back to a plain rclone process instead of failing, and it is restarted later."""
    def test_connection_refused_falls_back_to_subprocess(self):
        import urllib.error
        ok = subprocess.CompletedProcess([], 0, stdout=json.dumps([{"Path": "a", "Name": "a", "IsDir": True, "Size": -1}]), stderr="")
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(rc.DAEMON, "_call", side_effect=urllib.error.URLError("[Errno 61] Connection refused")), \
             mock.patch.object(rc.DAEMON, "stop") as stop, mock.patch.object(subprocess, "run", return_value=ok) as run:
            self.assertEqual(rc.ls("nas:share", dirs_only=True)[0]["Name"], "a")
            rc.version()                                       # no exception: answered by the fallback
            stop.assert_called(); self.assertGreaterEqual(run.call_count, 2)

    def test_a_slow_answer_is_not_a_dead_daemon(self):
        """A read timeout (an SMB listing slower than the caller's timeout) raises RcloneError and leaves the daemon running;
        before, it stopped the daemon, fell back to a fresh rclone with the same timeout and reported '60 s' after 120+."""
        import urllib.error, socket
        for exc in (TimeoutError("timed out"), urllib.error.URLError(socket.timeout("timed out")), urllib.error.URLError("The read operation timed out")):
            with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), \
                 mock.patch.object(rc.DAEMON, "_call", side_effect=exc), mock.patch.object(rc.DAEMON, "stop") as stop:
                with self.assertRaises(rc.RcloneError) as cm: rc.DAEMON.rc("operations/list", timeout=60, fs="nas:", remote="x")
                self.assertNotIsInstance(cm.exception, rc.DaemonGone); self.assertIn("within 60 s", str(cm.exception)); stop.assert_not_called()

    def test_available_restarts_a_dead_daemon_at_most_once_a_minute(self):
        d = rc._Daemon(); d.proc = mock.Mock(); d.proc.poll.return_value = 1; d.last_start = 1.0    # started once, died long ago
        with mock.patch.object(d, "start", return_value=True) as start:
            self.assertTrue(d.available()); start.assert_called_once()
        d.last_start = __import__("time").time()
        with mock.patch.object(d, "start", return_value=True) as start:
            self.assertFalse(d.available()); start.assert_not_called()

    def test_never_started_daemon_is_not_started_by_a_call(self):
        d = rc._Daemon()
        with mock.patch.object(d, "start") as start:
            self.assertFalse(d.available()); start.assert_not_called()


class ViaDaemon(unittest.TestCase):
    """With the daemon 'running', calls go to operations/list etc. (rc() is faked here)."""
    def test_ls_uses_operations_list_with_max_depth(self):
        calls = []
        def fake_rc(method, timeout=None, **params):
            calls.append((method, params)); return {"list": [{"Path": "x", "Name": "x", "IsDir": False, "Size": 3}]}
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(rc.DAEMON, "rc", side_effect=fake_rc):
            out = rc.ls("gdrive:Synology", files_only=True, max_depth=1)
        self.assertEqual(out[0]["Name"], "x")
        m, p = calls[0]
        # one backend per connection: the remote root is the fs, the folder the remote part (SMB sessions and Drive's directory
        # cache are reused across folders); Paths are stripped to the listed folder like rclone lsjson (list_destination relies on it)
        self.assertEqual(m, "operations/list"); self.assertEqual((p["fs"], p["remote"]), ("gdrive:", "Synology"))
        self.assertTrue(p["opt"]["filesOnly"]); self.assertEqual(p["_config"], {"MaxDepth": 1})

    def test_ls_strips_the_folder_prefix_from_paths(self):
        def fake_rc(method, timeout=None, **params):
            return {"list": [{"Path": "Backups/x.hbk/Config", "Name": "Config", "IsDir": True, "Size": -1}, {"Path": "Backups/x.hbk/notes.txt", "Name": "notes.txt", "IsDir": False, "Size": 3}]}
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), mock.patch.object(rc.DAEMON, "rc", side_effect=fake_rc):
            out = rc.ls("nas:Backups/x.hbk")
        self.assertEqual([e["Path"] for e in out], ["Config", "notes.txt"])
        self.assertEqual(rc._split("nas:Backups/x.hbk/"), ("nas:", "Backups/x.hbk")); self.assertEqual(rc._split("gdrive:"), ("gdrive:", ""))
        self.assertEqual(rc._split("/Volumes/backup/backup_1.hbk"), ("/Volumes/backup/backup_1.hbk", ""))
        with mock.patch.object(rc.paths, "is_remote_path", lambda p, windows=None: ":" in p and not re.match(r"^[A-Za-z]:[\\\\/]", p)):
            self.assertEqual(rc._split("D:\\Backups\\backup_1.hbk"), ("D:\\Backups\\backup_1.hbk", ""))       # a Windows drive is local
            self.assertEqual(rc._split("C:/x/backup_1.hbk"), ("C:/x/backup_1.hbk", ""))

    def test_ls_prefix_strip_ignores_case_and_falls_back(self):
        """A case-insensitive SMB share may answer 'backups/X.hbk/…' for 'Backups/x.hbk': stripped all the same; an answer that
        does not carry the prefix at all makes ls list the folder as its own fs (every Path relative to it for sure)."""
        calls = []
        def fake_rc(method, timeout=None, **params):
            calls.append(params)
            if params["remote"] == "Backups/x.hbk": return {"list": [{"Path": "backups/X.hbk/Config", "Name": "Config", "IsDir": True, "Size": -1}]}
            if params["remote"] == "Other": return {"list": [{"Path": "elsewhere/Config", "Name": "Config", "IsDir": True, "Size": -1}]}
            return {"list": [{"Path": "Config", "Name": "Config", "IsDir": True, "Size": -1}]}
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), mock.patch.object(rc.DAEMON, "rc", side_effect=fake_rc):
            self.assertEqual([e["Path"] for e in rc.ls("nas:Backups/x.hbk")], ["Config"])
            self.assertEqual([e["Path"] for e in rc.ls("nas:Other")], ["Config"])
        self.assertEqual((calls[-1]["fs"], calls[-1]["remote"]), ("nas:Other", ""))                 # the fallback call

    def test_ls_config_reaches_the_daemon_and_the_fallback(self):
        seen = {}
        def fake_rc(method, timeout=None, **params): seen.update(params); return {"list": []}
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), mock.patch.object(rc.DAEMON, "rc", side_effect=fake_rc):
            rc.ls("nas:", dirs_only=True, max_depth=1, config={"ConnectTimeout": "20s", "LowLevelRetries": 1})
        self.assertEqual(seen["_config"], {"ConnectTimeout": "20s", "LowLevelRetries": 1, "MaxDepth": 1})
        ok = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
        with mock.patch.object(rc.DAEMON, "available", return_value=False), mock.patch.object(subprocess, "run", return_value=ok) as run:
            rc.ls("nas:", config={"ConnectTimeout": "20s", "LowLevelRetries": 1})
        args = run.call_args[0][0]; self.assertEqual(args[args.index("--contimeout") + 1], "20s"); self.assertEqual(args[args.index("--low-level-retries") + 1], "1")

    def test_remotes_and_version_via_rc(self):
        types = {"nas": {"type": "smb"}, "gdrive": {"type": "drive"}}
        answers = {"config/listremotes": {"remotes": ["nas", "gdrive"]}, "core/version": {"version": "v1.75.2"}}
        def fake_rc(m, timeout=None, **p): return types[p["name"]] if m == "config/get" else answers[m]
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), \
             mock.patch.object(rc.DAEMON, "rc", side_effect=fake_rc):
            self.assertEqual(rc.remotes(), [("nas:", "smb"), ("gdrive:", "drive")])
            self.assertEqual(rc.version(), "rclone v1.75.2")
            types["gdrive"]["client_id"] = "1-a.apps.googleusercontent.com"; types["gdrive"]["client_secret"] = "s"; types["gdrive"]["token"] = "{}"
            info = rc.remote_info()
            self.assertEqual(info, {"nas:": {"type": "smb", "own_key": False}, "gdrive:": {"type": "drive", "own_key": True}})   # never the key or token itself


@unittest.skipUnless(rc.RCLONE and shutil.which(rc.RCLONE), "needs an rclone binary")
class RealDaemon(unittest.TestCase):
    """Start the real daemon once, list a temp folder through it, stop it."""
    def test_round_trip_on_a_local_folder(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "sub")); open(os.path.join(d, "f.txt"), "w").close()
        self.assertTrue(rc.start(timeout=15)); self.addCleanup(rc.stop)
        self.assertTrue(rc.DAEMON.running)
        names = sorted(e["Name"] for e in rc.ls(d))
        self.assertEqual(names, ["f.txt", "sub"])
        self.assertEqual([e["Name"] for e in rc.ls(d, dirs_only=True)], ["sub"])
        open(os.path.join(d, "sub", "deep.txt"), "w").close()
        self.assertEqual([(e["Path"], e["Name"]) for e in rc.ls(os.path.join(d, "sub"))], [("deep.txt", "deep.txt")])   # Path relative to the listed folder
        self.assertEqual(sorted(e["Path"] for e in rc.ls(d, recurse=True, files_only=True)), ["f.txt", "sub/deep.txt"])
        self.assertTrue(rc.version().startswith("rclone v"))
        with self.assertRaises(rc.RcloneError): rc.ls(os.path.join(d, "nonexistent"))
        rc.stop(); self.assertFalse(rc.DAEMON.running)


class CopyFile(unittest.TestCase):
    """Downloads through the daemon: async copyfile, bytes/speed/retry note from core/stats, cancel, fallback."""
    def test_async_copy_reports_progress_and_finishes(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        dst = os.path.join(d, "f.bin"); calls = []; polls = {"n": 0}
        def fake_rc(m, timeout=None, **p):
            calls.append(m)
            if m == "operations/copyfile":
                self.assertEqual((p["srcFs"], p["srcRemote"], p["dstFs"], p["dstRemote"]), ("gdrive:", "x/file", d, "f.bin"))
                self.assertTrue(p["_async"]); return {"jobid": 7}
            if m == "job/status":
                polls["n"] += 1
                if polls["n"] >= 3:
                    open(dst, "wb").write(b"x" * 30); return {"finished": True, "success": True}
                return {"finished": False}
            if m == "core/stats":                     # rclone's group "bytes" already contains the in-flight bytes of "transferring"
                return {"bytes": 10 * polls["n"], "speed": 1234.5, "transferring": [{"bytes": 10 * polls["n"]}], "lastError": "googleapi: Error 403: userRateLimitExceeded" if polls["n"] == 1 else ""}
            if m == "core/stats-delete": return {}
            raise AssertionError(m)
        seen = []
        with mock.patch.object(rc.DAEMON, "available", return_value=True), mock.patch.object(rc.DAEMON, "rc", side_effect=fake_rc):
            rc.copyfile("gdrive:x/file", dst, progress=lambda b, s, n: seen.append((b, s, n)), poll=0)
        self.assertEqual(seen[0], (10, 1234.5, "rate-limited, retrying")); self.assertEqual(seen[1], (20, 1234.5, ""))   # not 20 and 40
        self.assertEqual(seen[-1], (30, 0.0, "")); self.assertIn("core/stats-delete", calls)

    def test_failed_job_raises(self):
        def fake_rc(m, timeout=None, **p):
            return {"jobid": 1} if m == "operations/copyfile" else {"finished": True, "success": False, "error": "directory not found"} if m == "job/status" else {}
        with mock.patch.object(rc.DAEMON, "available", return_value=True), mock.patch.object(rc.DAEMON, "rc", side_effect=fake_rc):
            with self.assertRaises(rc.RcloneError) as cm: rc.copyfile("nas:x/missing", "/tmp/never", poll=0)
        self.assertIn("directory not found", str(cm.exception))

    def test_cancel_stops_the_job(self):
        calls = []
        def fake_rc(m, timeout=None, **p):
            calls.append(m); return {"jobid": 1} if m == "operations/copyfile" else {"finished": False} if m == "job/status" else {}
        with mock.patch.object(rc.DAEMON, "available", return_value=True), mock.patch.object(rc.DAEMON, "rc", side_effect=fake_rc):
            with self.assertRaises(rc.Cancelled): rc.copyfile("nas:x/f", "/tmp/never", cancelled=lambda: True, poll=0)
        self.assertIn("job/stop", calls)

    def test_without_daemon_runs_copyto(self):
        ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(rc.DAEMON, "available", return_value=False), mock.patch.object(subprocess, "run", return_value=ok) as run:
            rc.copyfile("nas:x/f", "/tmp/out")
        self.assertEqual(run.call_args[0][0][1:4], ["copyto", "nas:x/f", "/tmp/out"])
        for flag in ("--retries", "--drive-pacer-min-sleep", "--low-level-retries"): self.assertIn(flag, run.call_args[0][0])   # transfer.COMMON

    @unittest.skipUnless(rc.RCLONE and shutil.which(rc.RCLONE), "needs an rclone binary")
    def test_real_daemon_copies_a_local_file(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        src = os.path.join(d, "src", "a.bin"); os.makedirs(os.path.dirname(src)); open(src, "wb").write(b"z" * 100000)
        self.assertTrue(rc.start(timeout=15)); self.addCleanup(rc.stop)
        seen = []
        rc.copyfile(src, os.path.join(d, "out", "a.bin"), progress=lambda b, s, n: seen.append(b), poll=0.05)
        self.assertEqual(open(os.path.join(d, "out", "a.bin"), "rb").read(), b"z" * 100000); self.assertEqual(seen[-1], 100000)


class _FakeAuthorize:
    """A Popen stand-in for `rclone authorize`: the lines rclone prints, then an exit code."""
    def __init__(self, lines, rc=0): self.stdout = iter(l + "\n" for l in lines); self.returncode = rc
    def wait(self): return self.returncode
    def kill(self): self.returncode = -9


class Authorize(unittest.TestCase):
    LINES = ['NOTICE: Make sure your Redirect URL is set to "http://127.0.0.1:53682/" in your custom config.',
             "NOTICE: Please go to the following link: http://127.0.0.1:53682/auth?state=AbC123",
             "NOTICE: Log in and authorize rclone for access", "NOTICE: Waiting for code...", "NOTICE: Got code",
             "Paste the following into your remote machine --->", '{"access_token":"a","token_type":"Bearer","refresh_token":"r","expiry":"2026-12-31T00:00:00Z"}', "<---End paste"]

    def test_opens_the_url_once_and_returns_the_token(self):
        opened = []
        with mock.patch.object(rc, "RCLONE", "/fake/rclone"), mock.patch.object(subprocess, "Popen", return_value=_FakeAuthorize(self.LINES)) as popen:
            tok = rc.authorize_drive("id.apps.googleusercontent.com", "sec", on_url=opened.append, template="/tpl/oauth.html")
        self.assertEqual(json.loads(tok)["refresh_token"], "r"); self.assertEqual(opened, ["http://127.0.0.1:53682/auth?state=AbC123"])
        args = popen.call_args[0][0]
        self.assertEqual(args[:5], ["/fake/rclone", "authorize", "drive", "id.apps.googleusercontent.com", "sec"])
        self.assertIn("--auth-no-open-browser", args); self.assertEqual(args[args.index("--template") + 1], "/tpl/oauth.html")
        self.assertEqual(args[args.index("--drive-scope") + 1], "drive.readonly")                                 # read-only consent, never auth/drive

    def test_without_a_key_rclone_uses_its_own(self):
        with mock.patch.object(rc, "RCLONE", "/fake/rclone"), mock.patch.object(subprocess, "Popen", return_value=_FakeAuthorize(self.LINES)) as popen:
            rc.authorize_drive()
        self.assertEqual(popen.call_args[0][0][:6], ["/fake/rclone", "authorize", "drive", "--drive-scope", "drive.readonly", "--auth-no-open-browser"])

    def test_failure_raises_with_rclone_text(self):
        lines = self.LINES[:4] + ["NOTICE: Fatal error: config failed to refresh token: Error: access_denied"]
        with mock.patch.object(rc, "RCLONE", "/fake/rclone"), mock.patch.object(subprocess, "Popen", return_value=_FakeAuthorize(lines, rc=1)):
            with self.assertRaises(rc.RcloneError) as cm: rc.authorize_drive(on_url=lambda u: None)
        self.assertIn("access_denied", str(cm.exception))


class ConfigCreate(unittest.TestCase):
    def test_questions_are_answered_no_until_rclone_is_done(self):
        states = ['{"State": "*oauth-confirm,teamdrive,oauth,", "Option": {"Name": "config_refresh_token"}}',
                  '{"State": "teamdrive_ok", "Option": {"Name": "config_team_drive"}}', "{}"]
        calls = []
        def run(args, **kw):
            calls.append(args); return subprocess.CompletedProcess(args, 0, stdout=states[len(calls) - 1], stderr="")
        with mock.patch.object(rc, "RCLONE", "/fake/rclone"), mock.patch.object(subprocess, "run", run):
            rc.config_create("gdrive", "drive", {"scope": "drive.readonly", "token": '{"a":1}', "client_id": "x"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][:5], ["/fake/rclone", "config", "create", "gdrive", "drive"]); self.assertIn("--non-interactive", calls[0]); self.assertIn('token={"a":1}', calls[0])
        self.assertEqual(calls[1][-4:], ["--state", "*oauth-confirm,teamdrive,oauth,", "--result", "false"])
        self.assertEqual(calls[2][-4:], ["--state", "teamdrive_ok", "--result", "false"])

    def test_update_changes_only_the_given_keys(self):
        with mock.patch.object(rc, "RCLONE", "/fake/rclone"), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="{}", stderr="")) as run:
            rc.config_update("gdrive", {"client_id": "x", "client_secret": "y", "token": "{}"})
        self.assertEqual(run.call_args[0][0], ["/fake/rclone", "config", "update", "gdrive", "client_id=x", "client_secret=y", "token={}", "--non-interactive"])

    def test_failure_raises(self):
        with mock.patch.object(rc, "RCLONE", "/fake/rclone"), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom")):
            with self.assertRaises(rc.RcloneError): rc.config_create("x", "drive", {})


class FsCache(unittest.TestCase):
    def test_clear_only_when_the_daemon_runs(self):
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=False), mock.patch.object(rc.DAEMON, "rc") as call:
            rc.fscache_clear(); call.assert_not_called()
        with mock.patch.object(type(rc.DAEMON), "running", new_callable=mock.PropertyMock, return_value=True), mock.patch.object(rc.DAEMON, "rc") as call:
            rc.fscache_clear(); call.assert_called_once_with("fscache/clear")


class StartFlags(unittest.TestCase):
    """The daemon runs with the same Drive throttling as the one-process fallback: four slice downloads at a time without a
    transactions-per-second cap hit Google's rate limit."""
    def test_rcd_command_line_carries_the_throttle_flags(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        proc = mock.Mock(); proc.poll.return_value = None; proc.pid = 4242
        daemon = rc._Daemon()
        with mock.patch.object(rc, "RCLONE", "/fake/rclone"), mock.patch.object(rc.paths, "CONF_DIR", __import__("pathlib").Path(d)), \
             mock.patch.object(rc, "kill_stale"), mock.patch.object(rc, "kill_orphans"), mock.patch.object(subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(daemon, "_call", return_value={"version": "v1.75.2"}):
            self.assertTrue(daemon.start(timeout=1))
        args = popen.call_args[0][0]
        self.assertEqual(args[:2], ["/fake/rclone", "rcd"])
        for flag, value in (("--tpslimit", "16"), ("--drive-pacer-min-sleep", "10ms"), ("--low-level-retries", "20")):
            self.assertIn(flag, args); self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(rc.THROTTLE, args[-len(rc.THROTTLE):])


if __name__ == "__main__":
    unittest.main()
