"""Offline checks for rules and the dry-run policy."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from email_cleaner import db, index, policy, rules
from email_cleaner.providers.base import MessageMeta

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
OLD, RECENT = "2026-01-01T00:00:00Z", "2026-10-20T00:00:00Z"


def msg(i, sender, subject="Hello", received=OLD, folder="inbox", **kw):
    return MessageMeta(message_id=f"{folder}{i}", folder=folder, internet_message_id=f"<{i}@x>",
                       sender_address=sender, subject=subject, received_at=received, **kw)


class Fake:
    name = "outlook"

    def __init__(self, inbox, sent=()):
        self.data = {"inbox": inbox, "sent": list(sent)}

    def account_id(self):
        return "outlook:acct"

    def list_messages(self, folder, received_after=None):
        return iter(self.data[folder])

    def list_contact_addresses(self):
        return iter([])


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.folder.name) / "s.sqlite")

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def plan(self, inbox, sent=()):
        index.sync(self.conn, Fake(inbox, sent))
        return {item.message_id: item for item in policy.build_plan(self.conn, "outlook:acct", now=NOW)}

    def test_rule_validation_and_specificity(self):
        with self.assertRaises(ValueError):
            rules.add_rule(self.conn, "address", "shop.example", "trash", "user")
        with self.assertRaises(ValueError):
            rules.add_rule(self.conn, "domain_subject", "shop.example", "trash", "user")
        broad = rules.add_rule(self.conn, "domain", "@Shop.Example", "trash", "user")
        narrow = rules.add_rule(self.conn, "domain_subject", "shop.example", "archive", "user",
                                subject_keyword="訂單", target_folder="收據")
        active = rules.active_rules(self.conn)
        self.assertEqual(rules.first_match(active, "a@shop.example", "您的訂單已出貨").id, narrow)
        self.assertEqual(rules.first_match(active, "a@shop.example", "限時優惠").id, broad)
        self.assertIsNone(rules.first_match(active, "a@shop.example.evil", "限時優惠"))
        rules.set_status(self.conn, broad, "disabled")
        self.assertIsNone(rules.first_match(rules.active_rules(self.conn), "a@shop.example", "限時優惠"))

    def test_clients_are_filed_after_30_days_and_never_trashed(self):
        rules.add_rule(self.conn, "domain", "client.example", "trash", "user")
        plan = self.plan([msg(1, "boss@client.example"), msg(2, "boss@client.example", received=RECENT)],
                         sent=[msg(9, "me@x.example", folder="sent", recipients=("boss@client.example",))])
        self.assertEqual((plan["inbox1"].action, plan["inbox1"].target_folder), ("archive", "客戶"))
        self.assertEqual(plan["inbox2"].action, "keep")

    def test_trash_rule_is_blocked_by_not_client_guard(self):
        bulk = (("List-Unsubscribe", "<https://x>"),)
        index.sync(self.conn, Fake([msg(1, "news@letter.example", headers=bulk)],
                                   [msg(9, "me@x.example", folder="sent", recipients=("news@letter.example",))]))
        index.flag_protection_reviews(self.conn, "outlook:acct")
        index.resolve_protection_review(self.conn, "outlook:acct", "news@letter.example", "not_client")
        rules.add_rule(self.conn, "address", "news@letter.example", "trash", "user")
        item = policy.build_plan(self.conn, "outlook:acct", now=NOW)[0]
        self.assertEqual(item.action, "keep")
        self.assertTrue(item.blocked)

    def test_builtin_receipts_and_old_security_notices(self):
        plan = self.plan([
            msg(1, "shop@store.example", "您的電子發票已開立"),
            msg(2, "shop@store.example", "訂單確認", received=RECENT),
            msg(3, "account@site.example", "Your one-time code"),
            msg(4, "account@site.example", "驗證碼 123456", received=RECENT),
            msg(5, "news@letter.example", "Weekly"),
            msg(6, "ebill@bank.example", "電子對帳單 2026年9月", preview="開啟密碼為身分證字號"),
        ])
        self.assertEqual((plan["inbox1"].action, plan["inbox1"].target_folder), ("archive", "收據"))
        self.assertEqual(plan["inbox2"].action, "keep")
        self.assertEqual(plan["inbox3"].action, "trash")
        self.assertEqual(plan["inbox4"].action, "keep")
        self.assertEqual(plan["inbox5"].reason, "尚未分類（待 Jev／確認）")
        self.assertEqual((plan["inbox6"].action, plan["inbox6"].target_folder), ("archive", "收據"))

    def test_recent_archive_rule_waits_but_trash_rule_does_not(self):
        rules.add_rule(self.conn, "address", "ads@shop.example", "trash", "user")
        rules.add_rule(self.conn, "address", "news@paper.example", "archive", "user", target_folder="個人")
        plan = self.plan([msg(1, "ads@shop.example", received=RECENT), msg(2, "news@paper.example", received=RECENT),
                          msg(3, "news@paper.example")])
        self.assertEqual(plan["inbox1"].action, "trash")
        self.assertEqual(plan["inbox2"].action, "keep")
        self.assertEqual((plan["inbox3"].action, plan["inbox3"].target_folder), ("archive", "個人"))

    def test_plan_can_be_limited_to_top_senders_and_summarized(self):
        index.sync(self.conn, Fake([msg(i, "big@a.example") for i in range(3)] + [msg(9, "small@b.example")]))
        senders = policy.top_senders(self.conn, "outlook:acct", 1)
        self.assertEqual(senders, ["big@a.example"])
        plan = policy.build_plan(self.conn, "outlook:acct", senders, now=NOW)
        summary = policy.summarize(plan)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(json.loads(json.dumps(policy.to_json(plan)))[0]["sender"], "big@a.example")


if __name__ == "__main__":
    unittest.main()
