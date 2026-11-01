"""The UI scenarios of tests/e2e/ui_drive.py in headless Chrome, as tests: each runs the real app on a synthetic backup and
drives the page like a user; a scenario fails on a page exception, a wrong or missing restored file, or a failed check.

    HBK_E2E_UI=1 [HBK_CHROME=<chrome binary>] [HBK_E2E_UI_OUT=<folder for notes and screenshots>] python -m unittest tests.e2e.test_ui

Chrome: HBK_CHROME, else the usual install locations. POSIX only (the driver talks to Chrome over a pipe)."""
import os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANDIDATES = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
              "/usr/bin/chromium", "/usr/bin/chromium-browser"]


def chrome():
    c = os.environ.get("HBK_CHROME")
    if c: return c if os.path.exists(c) else None
    return next((p for p in CANDIDATES if os.path.exists(p)), None)


@unittest.skipUnless(os.environ.get("HBK_E2E_UI") == "1", "set HBK_E2E_UI=1 to run the UI scenarios in headless Chrome")
@unittest.skipIf(os.name == "nt", "the driver talks to Chrome over a pipe (POSIX)")
class UiScenarios(unittest.TestCase):
    def drive(self, name, *args, scale=6, bucket_kb=1024):
        """run one scenario; returns its notes (also kept under HBK_E2E_UI_OUT/<name> when set)"""
        self.assertIsNotNone(chrome(), "no Chrome found: set HBK_CHROME")
        keep = os.environ.get("HBK_E2E_UI_OUT")
        out = Path(keep, name) if keep else Path(tempfile.mkdtemp(prefix=f"hbk-ui-{name}-"))
        if out.exists(): shutil.rmtree(out)
        r = subprocess.run([sys.executable, "-m", "tests.e2e.ui_drive", "--chrome", chrome(), "--out", str(out), "--scale", str(scale),
                            "--bucket-kb", str(bucket_kb), *args], cwd=str(ROOT), capture_output=True, text=True, timeout=900,
                           env=dict(os.environ, PYTHONPATH=str(ROOT)))
        notes = (out / "notes.txt").read_text() if (out / "notes.txt").is_file() else ""
        if not keep: shutil.rmtree(out, ignore_errors=True)
        self.assertEqual(r.returncode, 0, f"{name}: driver failed\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
        self.assertTrue(notes, f"{name}: no notes\n{r.stdout[-2000:]}")
        page = [l for l in notes.splitlines() if l.startswith("[exception]")]
        self.assertEqual(page, [], f"{name}: page exceptions")
        return notes

    def assert_restored(self, notes, prefix=""):
        line = [l for l in notes.splitlines() if l.startswith(f"{prefix}on disk vs manifest:")][-1]
        m = re.search(r"ok=(\d+) missing=(\d+) wrong_bytes=(\[.*?\]) wrong_mtime=(\[.*?\])", line)
        self.assertTrue(m, line)
        self.assertEqual((m.group(2), m.group(3), m.group(4)), ("0", "[]", "[]"), line)
        self.assertGreater(int(m.group(1)), 20, line)

    def test_restore_like_a_person(self):                      # tick shares one at a time, set targets, Start
        self.assert_restored(self.drive("paced", "--chunk-index"))

    def test_restore_while_the_file_lists_load(self):          # the impatient user (#132): Start before the lists are in
        self.assert_restored(self.drive("fast", "--fast", "--chunk-index"))

    def test_queued_start_cancel_and_start_on_another_tab(self):   # #140/#144 on a backup without a chunk table
        notes = self.drive("queue", "--queue")
        self.assertIn("The queued restore was cancelled.", notes)
        self.assertIn("the queued restore has started", notes)
        self.assert_restored(notes, prefix="queue: ")

    def test_stop_and_resume(self):                             # #187
        notes = self.drive("stop", "--stop", "--chunk-index", scale=40, bucket_kb=2048)
        self.assertIn("stop: verdict OK", notes, notes)

    def test_hostile_update_notes_and_a_cross_site_page(self):   # #179/#181 and #182/#184 with a real browser
        notes = self.drive("security", "--update", "--csrf", scale=1, bucket_kb=256)
        self.assertIn("update: verdict OK", notes, notes)
        self.assertIn("csrf: verdict OK", notes, notes)



@unittest.skipUnless(os.environ.get("HBK_E2E_UI") == "1", "set HBK_E2E_UI=1 to run the UI scenarios in headless Chrome")
@unittest.skipIf(os.name == "nt", "the driver talks to Chrome over a pipe (POSIX)")
class SettingsDialog(unittest.TestCase):
    """#190 ⚙ Settings, one run of the --settings scenario: connections from the test's own rclone.conf (a NAS that does not
    answer, a Drive remote with its own key), appearance, the update choice, the cache, and editing the NAS."""
    @classmethod
    def setUpClass(cls):
        t = UiScenarios("test_restore_like_a_person")
        cls.notes = t.drive("settings", "--settings", scale=1, bucket_kb=256)
        cls.line = staticmethod(lambda key: next((l for l in cls.notes.splitlines() if l.startswith("settings: " + key)), ""))

    def test_connections_appearance_updates_cache(self):
        self.assertIn("127.0.0.1:1 · user alice", self.line("NAS section"))
        self.assertIn("your own Google key is in use", self.line("Drive section"))
        th = self.line("theme")
        self.assertIn("data-theme 'dark'", th); self.assertIn("after a reload 'dark'", th); self.assertIn("back to system -> None", th)
        self.assertIn("after 'off'=False, after 'mention'=True", self.line("updates"))
        c = self.line("cache before")
        self.assertIn("Clear while a job ran: (409", c); self.assertIn("after Clear 0 B", c); self.assertIn("Cleared", c)
        ask = self.line("cache asks first")                       # #196: the first click only asks, nothing is deleted yet
        self.assertIn("This deletes", ask); self.assertNotIn("still 0 B", ask)

    def test_a_failed_nas_edit_says_saved_and_can_be_put_back(self):
        """U9 (fixed in #198): the edit is saved even when the NAS does not answer (a VPN address set while away from it), and
        the dialog now says so ("Saved, but the NAS did not answer") with "Put back <old address>", which restores the previous
        address, port and user."""
        self.assertIn("Saved, but the NAS did not answer", self.line("NAS edit"))
        self.assertIn("host=127.0.0.2 port=4455 user=bob", self.line("rclone.conf [nas] afterwards"))
        self.assertIn("Put back 127.0.0.1:1", self.line("Put back button"))
        self.assertIn("host=127.0.0.1 port=1 user=alice", self.line("rclone.conf [nas] after Put back"))

    def test_a_nas_that_does_not_answer_is_reported_within_the_promised_time(self):
        """U10 (fixed in #199): the page treated a failed job as a failed call and polled it 60 more times (about 80 s for a
        20 s server answer)."""
        took = int(re.search(r"NAS edit took (\d+)s", self.line("NAS edit")).group(1))
        self.assertLess(took, 40, self.line("NAS edit"))

if __name__ == "__main__":
    unittest.main()
