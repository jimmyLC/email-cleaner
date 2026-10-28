"""Offline checks for the guarded executor and undo with a fake writer."""

import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

from email_cleaner import db, index, policy, rules
from email_cleaner.executor import Executor
from email_cleaner.policy import PlanItem
from tests.test_policy import Fake, msg

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


class FakeWriter:
    def __init__(self, conn, account="outlook:acct"):
        self.account, self.moves, self.created = account, [], []
        self.where = {row[0]: "INBOX" for row in conn.execute("SELECT message_id FROM messages WHERE folder='inbox'")}
        self.meta = {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT message_id, internet_message_id, sender_address FROM messages")}
        self.counter = 0

    def account_id(self):
        return self.account

    def folder_id(self, name):
        return {"inbox": "INBOX", "deleteditems": "TRASH"}[name]

    def ensure_folder(self, name):
        self.created.append(name)
        return "F:" + name

    def get_message(self, message_id):
        imid, sender = self.meta[message_id]
        return {"id": message_id, "internetMessageId": imid, "parentFolderId": self.where[message_id],
                "from": {"emailAddress": {"address": sender}}}

    def move(self, message_id, destination):
        self.counter += 1
        new_id = f"{message_id}~{self.counter}"
        self.meta[new_id] = self.meta[message_id]
        self.where[new_id] = destination
        del self.where[message_id]
        self.moves.append((message_id, destination))
        return new_id


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.folder.name) / "s.sqlite")
        index.sync(self.conn, Fake(
            [msg(1, "ads@shop.example"), msg(2, "boss@client.example"), msg(3, "shop@store.example", "電子發票")],
            [msg(9, "me@x.example", folder="sent", recipients=("boss@client.example",))]))
        rules.add_rule(self.conn, "address", "ads@shop.example", "trash", "user")
        self.writer = FakeWriter(self.conn)

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def run_plan(self, items=None, actor="user"):
        items = items if items is not None else policy.build_plan(self.conn, "outlook:acct", now=NOW)
        return Executor(self.conn, self.writer, "outlook:acct", actor).execute(items)

    def test_plan_moves_logs_and_updates_index(self):
        result = self.run_plan()
        self.assertEqual((result.moved, result.failed), (3, 0))
        self.assertIn(("inbox1", "TRASH"), self.writer.moves)
        self.assertIn(("inbox2", "F:客戶"), self.writer.moves)
        self.assertIn(("inbox3", "F:發票"), self.writer.moves)
        folders = dict(self.conn.execute("SELECT sender_address, folder FROM messages WHERE folder != 'sent'"))
        self.assertEqual(folders["ads@shop.example"], "trash")
        logged = self.conn.execute("SELECT count(*) FROM actions WHERE batch_id = ? AND new_message_id IS NOT NULL",
                                   (result.batch_id,)).fetchone()[0]
        self.assertEqual(logged, 3)

    def test_pace_sleeps_after_each_move_and_rests_between_blocks(self):
        sleeps = []
        items = policy.build_plan(self.conn, "outlook:acct", now=NOW)
        with unittest.mock.patch("email_cleaner.executor.REST_EVERY", 2):
            result = Executor(self.conn, self.writer, "outlook:acct", "user", pace=0.5,
                              sleep=sleeps.append).execute(items)
        self.assertEqual(result.moved, 3)
        self.assertEqual(sleeps, [0.5, 0.5, 20.0, 0.5])

    def test_forged_trash_of_client_is_blocked_at_run_time(self):
        forged = PlanItem("inbox2", "boss@client.example", "x", None, "trash", None, "forged by a caller")
        result = self.run_plan([forged], actor="mcp")
        self.assertEqual((result.moved, result.blocked), (0, 1))
        self.assertEqual(self.writer.moves, [])

    def test_unknown_actions_and_permanent_delete_do_not_exist(self):
        forged = PlanItem("inbox1", "ads@shop.example", "x", None, "delete", None, "forged")
        self.assertEqual(self.run_plan([forged]).skipped, 1)
        self.assertFalse(hasattr(self.writer, "delete"))
        with self.assertRaises(ValueError):
            Executor(self.conn, self.writer, "outlook:acct", "stranger")

    def test_changed_message_is_skipped(self):
        self.writer.where["inbox1"] = "SOMEWHERE"  # moved by the owner meanwhile
        result = self.run_plan()
        self.assertEqual(result.skipped, 1)
        self.assertNotIn(("inbox1", "TRASH"), self.writer.moves)

    def test_wrong_account_changes_nothing(self):
        self.writer.account = "outlook:other"
        with self.assertRaises(RuntimeError):
            self.run_plan()
        self.assertEqual(self.writer.moves, [])

    def test_undo_restores_batch_to_inbox(self):
        batch = self.run_plan().batch_id
        result = Executor(self.conn, self.writer, "outlook:acct", "user").undo(batch)
        self.assertEqual(result.moved, 3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM messages WHERE folder = 'inbox'").fetchone()[0], 3)
        self.assertEqual(Executor(self.conn, self.writer, "outlook:acct", "user").undo(batch).moved, 0)

    def test_moving_a_message_with_a_review_decision_keeps_the_decision_attached(self):
        self.conn.execute("""INSERT INTO review_queue (account_id, message_id, reason, resolution)
                             VALUES ('outlook:acct', 'inbox1', 'test', 'trash')""")
        self.conn.commit()
        result = self.run_plan()
        self.assertEqual((result.moved, result.failed), (3, 0))
        moved = self.conn.execute("SELECT message_id FROM messages WHERE sender_address = 'ads@shop.example'").fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT message_id FROM review_queue").fetchone()[0], moved)
        Executor(self.conn, self.writer, "outlook:acct", "user").undo(result.batch_id)
        restored = self.conn.execute("SELECT message_id FROM messages WHERE sender_address = 'ads@shop.example'").fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT message_id FROM review_queue").fetchone()[0], restored)

    def test_undo_can_be_limited_to_some_senders(self):
        batch = self.run_plan().batch_id
        result = Executor(self.conn, self.writer, "outlook:acct", "user").undo(batch, senders=["Ads@Shop.Example"])
        self.assertEqual(result.moved, 1)
        folders = dict(self.conn.execute("SELECT sender_address, folder FROM messages WHERE folder != 'sent'"))
        self.assertEqual(folders["ads@shop.example"], "inbox")
        self.assertEqual(folders["boss@client.example"], "archive:客戶")


if __name__ == "__main__":
    unittest.main()
