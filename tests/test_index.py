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

    def test_full_sync_marks_mail_the_mailbox_no_longer_holds(self):
        provider = FakeProvider([msg(1), msg(2), msg(3)])
        index.sync(self.conn, provider)
        provider.data["inbox"] = [msg(1), msg(3)]  # the owner moved inbox2 by hand
        index.sync(self.conn, provider)
        self.assertEqual(self.conn.execute("SELECT folder FROM messages WHERE message_id = 'inbox2'").fetchone()[0], "inbox")
        index.sync(self.conn, provider, full=True)
        folders = dict(self.conn.execute("SELECT message_id, folder FROM messages").fetchall())
        self.assertEqual(folders, {"inbox1": "inbox", "inbox2": "missing", "inbox3": "inbox"})

    def test_owner_can_unprotect_a_sender_that_was_never_queued_for_review(self):
        provider = FakeProvider(inbox=[msg(1, sender="support@service.example")],
                                sent=[msg(1, "sent", sender="me@home.example", recipients=("Support@Service.Example",))])
        index.sync(self.conn, provider)
        self.assertEqual(index.deletion_guard(self.conn, "outlook:acct", "support@service.example"), "曾寄信給對方")
        index.set_protection_decision(self.conn, "outlook:acct", "Support@Service.Example", "unprotect")
        self.assertIsNone(index.deletion_guard(self.conn, "outlook:acct", "support@service.example"))
        with self.assertRaises(ValueError):
            index.set_protection_decision(self.conn, "outlook:acct", "support@service.example", "delete")

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

    def test_newsletter_you_once_replied_to_is_queued_not_unprotected(self):
        bulk = (("List-Unsubscribe", "<https://x>"),)
        inbox = ([msg(i, sender="member@letter.example", headers=bulk) for i in range(5)]
                 + [msg(10 + i, sender="boss@client.example") for i in range(3)]
                 + [msg(20 + i, sender="friend@mail.example", headers=bulk) for i in range(3)])
        sent = [msg(1, "sent", recipients=("member@letter.example", "boss@client.example", "friend@mail.example"))]
        index.sync(self.conn, FakeProvider(inbox, sent, contacts=["friend@mail.example"]))
        # Contacts you also wrote to are queued too; the client you mail is not (no bulk evidence).
        self.assertEqual(index.flag_protection_reviews(self.conn, "outlook:acct"), 2)
        self.assertEqual(index.flag_protection_reviews(self.conn, "outlook:acct"), 0)  # idempotent
        pending = index.pending_protection_reviews(self.conn, "outlook:acct")
        self.assertEqual([p["address"] for p in pending], ["member@letter.example", "friend@mail.example"])
        self.assertEqual(pending[0]["total"], 5)
        reason = lambda a: index.protection_reason(self.conn, "outlook:acct", a)
        self.assertEqual(reason("member@letter.example"), "曾寄信給對方")  # still protected while pending

        index.resolve_protection_review(self.conn, "outlook:acct", "Member@Letter.Example", "unprotect")
        self.assertIsNone(reason("member@letter.example"))
        self.assertEqual([p["address"] for p in index.pending_protection_reviews(self.conn, "outlook:acct")],
                         ["friend@mail.example"])
        self.assertEqual(reason("boss@client.example"), "曾寄信給對方")

    def test_not_client_decision_blocks_trash_without_client_filing(self):
        bulk = (("List-Unsubscribe", "<https://x>"),)
        index.sync(self.conn, FakeProvider([msg(1, sender="news@letter.example", headers=bulk),
                                            msg(2, sender="ads@shop.example", headers=bulk)],
                                           [msg(1, "sent", recipients=("news@letter.example", "ads@shop.example"))]))
        index.flag_protection_reviews(self.conn, "outlook:acct")
        index.resolve_protection_review(self.conn, "outlook:acct", "news@letter.example", "not_client")
        index.resolve_protection_review(self.conn, "outlook:acct", "ads@shop.example", "unprotect")
        self.assertIsNone(index.protection_reason(self.conn, "outlook:acct", "news@letter.example"))
        self.assertEqual(index.deletion_guard(self.conn, "outlook:acct", "news@letter.example"), "使用者指定不刪除")
        self.assertIsNone(index.deletion_guard(self.conn, "outlook:acct", "ads@shop.example"))

    def test_service_mailboxes_you_wrote_to_are_queued(self):
        inbox = [msg(1, sender="support@service.example"), msg(2, sender="anna.lee@client.example"),
                 msg(3, sender="customercare@gadget.example")]
        sent = [msg(1, "sent", recipients=("support@service.example", "anna.lee@client.example",
                                           "customercare@gadget.example"))]
        index.sync(self.conn, FakeProvider(inbox, sent))
        self.assertEqual(index.flag_protection_reviews(self.conn, "outlook:acct"), 2)
        pending = {p["address"]: p["reason"] for p in index.pending_protection_reviews(self.conn, "outlook:acct")}
        self.assertEqual(set(pending), {"support@service.example", "customercare@gadget.example"})
        self.assertIn("客服／服務地址", pending["support@service.example"])
        self.assertFalse(index.ROLE_ADDRESS.match("supportive.person"))

    def test_client_domain_still_protects_an_unprotected_address(self):
        bulk = (("List-Unsubscribe", "<https://x>"),)
        index.sync(self.conn, FakeProvider([msg(1, sender="news@partner.example", headers=bulk)],
                                           [msg(1, "sent", recipients=("news@partner.example",))]))
        index.flag_protection_reviews(self.conn, "outlook:acct")
        index.resolve_protection_review(self.conn, "outlook:acct", "news@partner.example", "unprotect")
        self.conn.execute("INSERT INTO client_domains (domain) VALUES ('partner.example')")
        self.assertEqual(index.protection_reason(self.conn, "outlook:acct", "news@partner.example"), "客戶網域")
        with self.assertRaises(ValueError):
            index.resolve_protection_review(self.conn, "outlook:acct", "news@partner.example", "delete")
        with self.assertRaises(ValueError):
            index.resolve_protection_review(self.conn, "outlook:acct", "nobody@x.example", "keep")


if __name__ == "__main__":
    unittest.main()
