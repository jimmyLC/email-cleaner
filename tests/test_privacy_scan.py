"""Offline checks for the pre-push privacy scan using a throwaway Git repo."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "privacy_scan", Path(__file__).resolve().parent.parent / "scripts" / "privacy_scan.py")
privacy_scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(privacy_scan)

SECRET = "sk-test-value-not-real-123"


class PrivacyScanTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True)
        self.patch = patch.object(privacy_scan, "ROOT", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.folder.cleanup()

    def commit(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", name], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "x"], check=True)

    def test_clean_history_has_no_findings(self):
        self.commit("app.py", "print('hello')\n")
        self.assertEqual(privacy_scan.scan(["--all"], {SECRET}), [])

    def test_secret_in_earlier_commit_is_found_without_printing_it(self):
        self.commit("app.py", f"KEY = '{SECRET}'\n")
        self.commit("app.py", "KEY = ''\n")
        findings = privacy_scan.scan(["--all"], {SECRET})
        self.assertTrue(any("app.py" in item for item in findings))
        self.assertFalse(any(SECRET in item for item in findings))

    def test_forbidden_filenames_are_found(self):
        self.commit(".env", "X=1\n")
        self.commit("data/email-cleaner.sqlite", "x")
        findings = " ".join(privacy_scan.scan(["--all"], set()))
        self.assertIn(".env", findings)
        self.assertIn("data/email-cleaner.sqlite", findings)

    def test_env_example_is_allowed(self):
        self.commit(".env.example", "TYPESAFE_API_KEY=\n")
        self.assertEqual(privacy_scan.scan(["--all"], set()), [])

    def test_private_values_skip_short_and_empty_entries(self):
        env = self.root / "private.env"
        env.write_text(f"A={SECRET}\nB=\nC=abc\n# D=commented-value\n", encoding="utf-8")
        self.assertEqual(privacy_scan.private_values([env], [], self.root / "missing.sqlite"), {SECRET})

    def test_allowlisted_generic_phrase_is_ignored(self):
        allow = self.root / "allow.txt"
        allow.write_text("# comment\nGeneric phrase here\n", encoding="utf-8")
        env = self.root / "private.env"
        env.write_text(f"A=Generic phrase here\nB={SECRET}\n", encoding="utf-8")
        with patch.object(privacy_scan, "ALLOWLIST", allow):
            self.assertEqual(privacy_scan.private_values([env], [], self.root / "x.sqlite"), {SECRET})


if __name__ == "__main__":
    unittest.main()
