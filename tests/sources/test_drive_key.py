"""Settings → Google Drive → Sign in again keeps the connection's own key: rclone.drive_key reads it back from the config."""
import json, unittest
from unittest import mock
from callido_restore.sources import rclone


class DriveKey(unittest.TestCase):
    def test_from_config_dump(self):
        dump = {"gdrive": {"type": "drive", "client_id": "123-abc.apps.googleusercontent.com", "client_secret": "s3cr3t"}, "plain": {"type": "drive"}}
        with mock.patch.object(rclone.DAEMON, "available", return_value=False), mock.patch.object(rclone, "_run", return_value=json.dumps(dump)):
            self.assertEqual(rclone.drive_key("gdrive:"), ("123-abc.apps.googleusercontent.com", "s3cr3t"))
            self.assertEqual(rclone.drive_key("plain"), ("", ""))
            self.assertEqual(rclone.drive_key("missing"), ("", ""))

    def test_from_the_daemon(self):
        with mock.patch.object(rclone.DAEMON, "available", return_value=True), \
             mock.patch.object(rclone.DAEMON, "rc", return_value={"client_id": "9-x.apps.googleusercontent.com", "client_secret": "y"}):
            self.assertEqual(rclone.drive_key("gdrive"), ("9-x.apps.googleusercontent.com", "y"))


if __name__ == "__main__":
    unittest.main()
