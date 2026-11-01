"""The server side of the automatic update setting: it starts only when nothing runs, and the install asks again."""
import unittest
from unittest import mock

from callido_restore import update
from callido_restore.server import api


class AutoTick(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(api.JOBS, {}, clear=True); p.start(); self.addCleanup(p.stop)

    def job(self, label, state="running"): api.JOBS[label] = {"id": label, "label": label, "state": state}

    def test_busy(self):
        self.assertIsNone(api.update_busy())
        self.job("restore 3 files", "paused"); self.assertIsNone(api.update_busy())   # a paused restore continues afterwards
        self.job("build pool map"); self.assertEqual(api.update_busy(), "preparation")
        api.JOBS.clear(); self.job("restore 3 files"); self.assertEqual(api.update_busy(), "restore")
        api.JOBS.clear(); self.job("update"); self.assertEqual(api.update_busy(), "update"); self.assertIsNone(api.update_busy(include_update=False))

    def test_tick(self):
        with mock.patch.object(update, "auto_due", return_value=False), mock.patch.object(api, "start_job") as sj:
            self.assertIsNone(api.auto_update_tick()); sj.assert_not_called()
        with mock.patch.object(update, "auto_due", return_value=True), mock.patch.object(api, "start_job") as sj:
            self.job("restore 3 files"); self.assertIsNone(api.auto_update_tick()); sj.assert_not_called()   # waits for the restore
            api.JOBS.clear(); sj.return_value = {"id": "u1"}
            self.assertEqual(api.auto_update_tick(), "u1"); self.assertEqual(sj.call_args.args[1], "update")
        with mock.patch.object(update, "install") as inst:
            sj.call_args.args[0]({})                                                   # the job body
            may = inst.call_args.kwargs["may_install"]; self.assertTrue(inst.call_args.kwargs["auto"])
            self.assertTrue(may()); self.job("build pool map"); self.assertFalse(may())


class Settings(unittest.TestCase):
    """The UI's {check, auto} and the plain {mode} both set the update setting."""
    def test_settings_over_http(self):
        import json, os, shutil, tempfile, threading, pathlib
        from callido_restore.server.api import LocalServer
        from urllib.request import Request, urlopen
        from callido_restore import paths
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        p = mock.patch.object(paths, "CONF_DIR", pathlib.Path(d)); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(update, "_checking", [True]); p.start(); self.addCleanup(p.stop)
        srv = LocalServer(("127.0.0.1", 0), api.H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        def post(body):
            req = Request(f"http://127.0.0.1:{srv.server_address[1]}/api/update/settings", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urlopen(req) as r: return json.loads(r.read())
        self.assertEqual(post({"check": True, "auto": True})["mode"], "auto")
        self.assertTrue(post({"check": True, "auto": True})["auto"])
        self.assertEqual(post({"check": True, "auto": False})["mode"], "mention")
        self.assertEqual(post({"check": False})["mode"], "off")
        self.assertEqual(post({"check": False, "auto": True})["mode"], "off")
        self.assertEqual(post({"mode": "auto"})["mode"], "auto")
