"""Offline checks for index sync, protection and grouping with a fake provider."""

import json
import tempfile
import unittest
from pathlib import Path

from email_cleaner import db, index
from email_cleaner.providers.base import MessageMeta


def msg(i, folder="inbox", sender="news@shop.example", **kw):
    return MessageMeta(message_id=f"{folder}{i}", folder=folder, internet_message_id=f"<{folder}{i}@x>",
                       sender_address=sender, sender_name="N", subject=f"Subject {i % 3}",
                       received_at=f"2026-01-{i + 1:02d}T00:00:00Z", **kw)


class FakeProvider:
    name = "outlook"

    def __init__(self, inbox, sent=(), contacts=()):
        self.data = {"inbox": list(inbox), "sent": list(sent)}
        self.contacts, self.calls = list(contacts), []

    def account_id(self):
        return "outlook:acct"

    def list_messages(self, folder, received_after=None):
        self.calls.append((folder, received_after))
        return iter([m for m in self.data[folder] if not received_after or m.received_at > received_after])

    def list_contact_addresses(self):
        return iter(self.contacts)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.folder.name) / "s.sqlite")

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def test_sync_stores_metadata_and_signals_only(self):
        provider = FakeProvider([msg(1, headers=(("List-Unsubscribe", "<https://x>"),), preview="secret body text")])
        result = index.sync(self.conn, provider)
        self.assertEqual(result.inbox, 1)
        row = self.conn.execute("SELECT * FROM messages").fetchone()
        self.assertNotIn("secret body text", json.dumps(dict(row), ensure_ascii=False))
        self.assertIn("List-Unsubscribe 標頭", json.loads(row["signals"])["bulk"])

    def test_incremental_sync_asks_only_for_newer_mail(self):
        provider = FakeProvider([msg(1), msg(2)])
        index.sync(self.conn, provider)
        provider.data["inbox"].append(msg(5))
        result = index.sync(self.conn, provider)
        self.assertEqual(provider.calls[-2], ("inbox", "2026-01-03T00:00:00Z"))
        self.assertEqual(result.inbox, 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 3)
        index.sync(self.conn, provider, full=True)
        self.assertEqual(provider.calls[-2], ("inbox", None))

    def test_protection_from_sent_contacts_and_client_domains(self):
        provider = FakeProvider(
            inbox=[msg(1, sender="boss@client.example"), msg(2, sender="friend@mail.example"),
                   msg(3, sender="pm@partner.example"), msg(4)],
            sent=[msg(1, "sent", sender="me@home.example", recipients=("Boss@Client.Example",))],
            contacts=["friend@mail.example"])
        index.sync(self.conn, provider)
        self.conn.execute("INSERT INTO client_domains (domain) VALUES ('partner.example')")
        reason = lambda a: index.protection_reason(self.conn, "outlook:acct", a)
        self.assertEqual(reason("boss@client.example"), "曾寄信給對方")
        self.assertEqual(reason("friend@mail.example"), "在聯絡人中")
        self.assertEqual(reason("pm@partner.example"), "客戶網域")
        self.assertIsNone(reason("news@shop.example"))
        # Only the exact domain counts, not look-alike subdomains.
        self.assertIsNone(reason("x@partner.example.evil"))

    def test_sender_groups_rank_by_volume_with_evidence(self):
        bulk = (("List-Unsubscribe", "<https://x>"),)
        inbox = [msg(i, headers=bulk) for i in range(4)] + [msg(10, sender="boss@client.example")]
        provider = FakeProvider(inbox, sent=[msg(1, "sent", recipients=("boss@client.example",))])
        index.sync(self.conn, provider)
        groups = index.sender_groups(self.conn, "outlook:acct")
        self.assertEqual([g["sender"] for g in groups], ["news@shop.example", "boss@client.example"])
        self.assertEqual(groups[0]["total"], 4)
        self.assertEqual(groups[0]["bulk_ratio"], 1.0)
        self.assertIsNone(groups[0]["protected"])
        self.assertEqual(groups[1]["protected"], "曾寄信給對方")
        self.assertLessEqual(len(groups[0]["sample_subjects"]), 3)
        stats = index.stats(self.conn, "outlook:acct")
        self.assertEqual(stats["folders"], {"inbox": 5, "sent": 1})
        self.assertEqual(stats["inbox_senders"], 2)


if __name__ == "__main__":
    unittest.main()
