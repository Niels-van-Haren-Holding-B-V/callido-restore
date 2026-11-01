"""Windows auto-update step (update_windows): the MSI comes through the shared signed download (update.download_verified), and
the helper waits for the app, runs msiexec /passive and starts the app again."""
import tempfile, unittest
from pathlib import Path
from unittest import mock

from callido_restore import update_windows as uw

class Download(unittest.TestCase):
    def test_uses_the_signed_shared_download(self):
        from callido_restore import update
        rel = {"tag_name": "v9.9.9", "assets": []}; prog, canc = (lambda d, t: None), (lambda: False)
        with mock.patch.object(update, "download_verified", return_value=Path("x.msi")) as dv:
            self.assertEqual(uw.download(rel, progress=prog, cancelled=canc), Path("x.msi"))
        dv.assert_called_once_with(rel, uw.MSI_NAME, progress=prog, cancelled=canc)
        self.assertEqual(uw.MSI_NAME, update.checksum_list(uw.MSI_NAME)[:-len(".sha256")])   # the list that covers the MSI

    def test_refusals_pass_through(self):
        from callido_restore import update
        with mock.patch.object(update, "download_verified", side_effect=RuntimeError("the signature of x does not verify")):
            with self.assertRaises(RuntimeError): uw.download({"tag_name": "v9.9.9"})


class Helper(unittest.TestCase):
    def test_hash_is_checked_again_before_msiexec(self):          # hbk-4: the cached MSI must not be swapped before UAC elevates it
        script = uw.helper_command(r"C:\c\x.msi", r"C:\P\a.exe", 1, r"C:\l.txt", "ab" * 32)[-1]
        i_hash, i_check, i_msi = script.index("[IO.File]::OpenRead('C:\\c\\x.msi')"), script.index("if ($h -ne '" + "AB" * 32 + "')"), script.index("Start-Process -FilePath 'msiexec.exe'")
        self.assertTrue(i_hash < i_check < i_msi)
        self.assertIn("throw ('the MSI changed after it was verified", script)
        for bad in (None, "", "zz" * 32, "ab" * 31): 
            with self.assertRaises(RuntimeError): uw.helper_command("x.msi", "a.exe", 1, "l", bad)

    def test_command(self):
        cmd = uw.helper_command(r"C:\Users\x\.hbk-explorer\cache\updates\1.2.3\CallidoRestore-x64.msi",
                                r"C:\Program Files\Callido\Callido Restore\Callido Restore.exe", 4321, r"C:\log's.txt", "ab" * 32)
        self.assertEqual(cmd[0], "powershell.exe"); script = cmd[-1]
        self.assertIn("Wait-Process -Id 4321", script); self.assertIn("catch {", script)
        self.assertIn("""-ArgumentList '/i "C:\\Users\\x\\.hbk-explorer\\cache\\updates\\1.2.3\\CallidoRestore-x64.msi" /passive /norestart /l*v "C:\\log''s.txt"'""", script)
        self.assertIn("'C:\\log''s.txt.result'", script)                           # single quotes doubled for PowerShell
        self.assertTrue(script.endswith("finally { Start-Process -FilePath 'C:\\Program Files\\Callido\\Callido Restore\\Callido Restore.exe' }"))

    def test_paths_with_spaces_stay_one_argument(self):
        script = uw.helper_command(r"C:\Users\Jan de Vries\.hbk-explorer\cache\updates\1.2.3\x.msi", r"C:\P\a.exe", 1, r"C:\Users\Jan de Vries\u.log", "ab" * 32)[-1]
        self.assertIn('/i "C:\\Users\\Jan de Vries\\.hbk-explorer\\cache\\updates\\1.2.3\\x.msi" /passive', script)
        self.assertTrue(script.endswith("finally { Start-Process -FilePath 'C:\\P\\a.exe' }"))

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
            import hashlib
            self.assertIn(hashlib.sha256(b"x").hexdigest().upper(), args[0][-1])           # hashed right after the download's check


if __name__ == "__main__":
    unittest.main()
