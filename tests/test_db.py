"""Offline schema checks with synthetic data."""

import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from email_cleaner import db


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "store.sqlite"
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def test_store_is_owner_only(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_messages_table_has_no_body_column(self):
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(messages)")}
        self.assertNotIn("body", columns)
        self.assertIn("sender_address", columns)

    def test_reopen_is_idempotent(self):
        self.conn.close()
        self.conn = db.connect(self.path)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM schema_version").fetchone()[0], 1)

    def test_rule_constraints(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO rules (scope, sender, action, origin) "
                              "VALUES ('domain_subject', 'shop.example', 'trash', 'user')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO rules (scope, sender, action, origin) "
                              "VALUES ('address', 'a@shop.example', 'archive', 'user')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO rules (scope, sender, action, origin) "
                              "VALUES ('address', 'a@shop.example', 'delete_forever', 'user')")
        self.conn.execute("INSERT INTO rules (scope, sender, subject_keyword, action, origin) "
                          "VALUES ('domain_subject', 'shop.example', '優惠', 'trash', 'claude')")

    def test_action_log_rejects_unknown_actor(self):
        self.conn.execute("INSERT INTO accounts (id, provider) VALUES ('acct', 'outlook')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO actions (batch_id, account_id, action, actor) "
                              "VALUES ('b1', 'acct', 'trash', 'stranger')")

    def test_old_schema_is_upgraded_in_place(self):
        self.conn.execute("DROP TABLE sender_reviews")
        self.conn.execute("UPDATE schema_version SET version = 1")
        self.conn.commit()
        self.conn.close()
        self.conn = db.connect(self.path)
        self.assertEqual(self.conn.execute("SELECT version FROM schema_version").fetchone()[0], db.SCHEMA_VERSION)
        self.conn.execute("SELECT count(*) FROM sender_reviews")

    def test_v2_store_gains_new_message_id_column(self):
        self.conn.execute("ALTER TABLE actions DROP COLUMN new_message_id")
        self.conn.execute("UPDATE schema_version SET version = 2")
        self.conn.commit()
        self.conn.close()
        self.conn = db.connect(self.path)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(actions)")}
        self.assertIn("new_message_id", columns)

    def test_newer_schema_is_refused(self):
        self.conn.execute("UPDATE schema_version SET version = 999")
        self.conn.commit()
        self.conn.close()
        with self.assertRaises(RuntimeError):
            self.conn = db.connect(self.path)
        self.conn = sqlite3.connect(self.path)


if __name__ == "__main__":
    unittest.main()
