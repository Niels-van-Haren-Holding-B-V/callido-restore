"""The menu-bar / notification-area icon (app/tray.py) and its setting (appsettings, /api/app/settings): the hover text says
what runs (files, download and upload speed, current file), the setting is kept in app.json, and on macOS only the close button
or Cmd+W hides the window (Cmd+Q and the Dock's Quit still quit)."""
import ctypes, json, os, shutil, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

from callido_restore import appsettings, paths
from callido_restore.app import tray
from callido_restore.server.api import H, LocalServer

RESTORE = {"state": "running", "label": "restore", "done_files": 10582, "total_files": 107500, "progress": 0.2, "errors_total": 1,
           "rates": {"download": 321126, "upload": 5033164}, "message": "10585/107500: Photos/2021/clip 0001.MP4"}


class StatusText(unittest.TestCase):
    def test_a_restore_shows_files_speeds_and_the_current_file(self):
        self.assertEqual(tray.status_lines([RESTORE]), ["Restoring 10,582 / 107,500 files · 20 % · 1 error",
                                                        "↓ 313.6 KB/s from the backup · ↑ 4.8 MB/s to the target", "clip 0001.MP4"])

    def test_a_skipped_file_message_still_gives_the_name(self):
        j = dict(RESTORE, message="12/107500: already at the target, skipped: Photos/a.jpg")
        self.assertEqual(tray.status_lines([j])[-1], "a.jpg")

    def test_nothing_running(self):
        self.assertEqual(tray.status_lines([{"state": "done", "label": "restore"}]), ["Nothing running"])

    def test_another_job(self):
        self.assertEqual(tray.status_lines([{"state": "running", "label": "build pool map", "progress": 0.5, "message": "scanning"}]),
                         ["build pool map · 50 %", "scanning"])

    def test_the_hover_text_fits_windows(self):
        long = dict(RESTORE, message="1/2: " + "x" * 300)
        t = tray.tooltip(tray.status_lines([long]))
        self.assertLessEqual(len(t), 127); self.assertTrue(t.startswith("Callido Restore\n"))


class WindowsStruct(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit Windows layout")
    def test_notifyicondata_has_the_documented_size(self):
        self.assertEqual(ctypes.sizeof(tray._notifyicondata()), 976)


class MacCloseOrQuit(unittest.TestCase):
    def setUp(self): self.addCleanup(tray._LAST_EVENT.__setitem__, slice(None), [0.0, ""])

    def test_a_click_hides(self):
        tray._LAST_EVENT[:] = [time.monotonic(), "mouse"]; self.assertTrue(tray.mac_close_was_the_window())

    def test_cmd_w_hides(self):
        tray._LAST_EVENT[:] = [time.monotonic(), "cmd-w"]; self.assertTrue(tray.mac_close_was_the_window())

    def test_cmd_q_quits(self):
        tray._LAST_EVENT[:] = [time.monotonic(), "cmd-q"]; self.assertFalse(tray.mac_close_was_the_window())

    def test_an_old_click_does_not_count(self):                     # the Dock's Quit long after the last click in the window
        tray._LAST_EVENT[:] = [time.monotonic() - 30, "mouse"]; self.assertFalse(tray.mac_close_was_the_window())


class NotShownUntilAlive(unittest.TestCase):
    def test_a_failed_icon_never_lets_a_close_hide_the_window(self):
        t = tray.Tray(lambda: None, lambda: None, lambda: [])
        t.impl = mock.Mock(alive=lambda: False); self.assertFalse(t.shown)
        t.impl = mock.Mock(alive=lambda: True); self.assertTrue(t.shown)


class Setting(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        p = mock.patch.object(paths, "CONF_DIR", Path(d)); p.start(); self.addCleanup(p.stop)
        self.addCleanup(appsettings._listeners.clear)

    def test_default_on_and_kept(self):
        self.assertTrue(appsettings.get("tray"))
        seen = []; appsettings.on_change(seen.append)
        appsettings.update(tray=False)
        self.assertFalse(appsettings.get("tray")); self.assertEqual(seen, [{"tray": False}])

    def test_wrong_values_are_refused(self):
        with self.assertRaises(ValueError): appsettings.update(tray="yes")
        with self.assertRaises(ValueError): appsettings.update(other=True)

    def test_a_damaged_file_gives_the_defaults(self):
        (paths.CONF_DIR / "app.json").write_text("{nope"); self.assertEqual(appsettings.load(), {"tray": True})

    def test_api(self):
        srv = LocalServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with urlopen(f"{base}/api/app/settings", timeout=10) as r: a = json.loads(r.read())
            self.assertEqual((a["tray"], a["tray_available"]), (True, False)); self.assertIn(a["platform"], ("mac", "windows", "other"))
            req = Request(f"{base}/api/app/settings", data=json.dumps({"tray": False}).encode(), method="POST")
            with urlopen(req, timeout=10) as r: self.assertFalse(json.loads(r.read())["tray"])
            self.assertFalse(appsettings.get("tray"))
        finally:
            srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
