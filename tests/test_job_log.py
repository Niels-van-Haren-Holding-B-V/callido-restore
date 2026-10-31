"""A failed job leaves a line in the log (app.log in the packaged app): Niels's failed SMB connect on Windows left none."""
import io, time, unittest
from contextlib import redirect_stdout

from callido_restore.restore.jobs import start_job


class FailedJobIsLogged(unittest.TestCase):
    def test_error_line(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            job = start_job(lambda j: (_ for _ in ()).throw(RuntimeError("nas did not answer")), "connect smb")
            for _ in range(100):
                if job["state"] != "running": break
                time.sleep(0.01)
        self.assertEqual(job["state"], "error")
        self.assertIn("job 'connect smb' failed after", buf.getvalue()); self.assertIn("nas did not answer", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
