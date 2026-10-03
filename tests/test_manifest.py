import json
import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


class ManifestTest(unittest.TestCase):
    def test_backend_runs_as_root(self):
        # Decky Loader only elevates when the exact flag "root" is present
        # ("_root" in the template is a disabled placeholder).
        with open(os.path.join(ROOT, "plugin.json")) as f:
            manifest = json.load(f)
        self.assertIn("root", manifest["flags"])

    def test_versions_present(self):
        with open(os.path.join(ROOT, "package.json")) as f:
            self.assertRegex(json.load(f)["version"], r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
