"""The version lives in two places (about.py for the app, pyproject.toml for packaging); they must agree."""
import pathlib, tomllib, unittest

from callido_restore.about import VERSION


class VersionTests(unittest.TestCase):
    def test_pyproject_matches_about(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        with open(root / "pyproject.toml", "rb") as f: meta = tomllib.load(f)
        self.assertEqual(meta["project"]["version"], VERSION)
