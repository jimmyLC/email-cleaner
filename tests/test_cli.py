"""CLI smoke checks against a temporary data directory; no mailbox access."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from email_cleaner import cli


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_client_domain_round_trip(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"EMAIL_CLEANER_DATA": folder}):
            self.assertEqual(self.run_cli("client-domain", "add", "Client.Example")[1].strip(), "client.example")
            self.assertEqual(self.run_cli("client-domain", "remove", "client.example")[1].strip(), "")

    def test_groups_before_sync_reports_error_instead_of_traceback(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"EMAIL_CLEANER_DATA": folder}):
            self.assertEqual(self.run_cli("groups", "outlook")[0], 1)


if __name__ == "__main__":
    unittest.main()
