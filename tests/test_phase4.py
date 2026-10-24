"""Offline checks for rule drafts, Jev classification, reviews and the MCP surface."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from email_cleaner import db, index, jev, policy, review, rules
from tests.test_policy import Fake, msg

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
BULK = (("List-Unsubscribe", "<https://x>"),)


def jev_client(category, confidence=0.95, kind="bulk_or_automated", kind_confidence=0.95):
    client = Mock()
    client.system_one.return_value = SimpleNamespace(choices={
        "category": SimpleNamespace(choice=category, confidence=confidence),
        "message_type": SimpleNamespace(choice=kind, confidence=kind_confidence)})
    return client


class Phase4Tests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.folder.name) / "s.sqlite")
        index.sync(self.conn, Fake([
            msg(1, "news@letter.example", "Weekly digest", headers=BULK),
            msg(2, "editor@letter.example", "Weekly digest 2"),
            msg(3, "friend@mail.example", "週末聚餐"),
            msg(4, "boss@client.example", "報價"),
        ], [msg(9, "me@x.example", folder="sent", recipients=("boss@client.example",))]))

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def test_redaction_masks_links_contacts_and_numbers(self):
        text = jev.redact("<b>Hi</b> call 0912-345-678, mail a@b.example, see https://x.example/p?id=1, card 4111111111111111")
        for secret in ("0912", "a@b.example", "https://", "4111"):
            self.assertNotIn(secret, text)
        self.assertEqual(len(jev.redact("x" * 5000)), jev.EXCERPT_CHARS)

    def test_state_sent_to_jev_has_no_full_address_or_raw_body(self):
        row = self.conn.execute("SELECT * FROM messages WHERE message_id = 'inbox1'").fetchone()
        client = jev_client("newsletter")
        jev.classify(client, row, "Visit https://track.example/u?id=123456789 now")
        state = client.system_one.call_args.kwargs["state"]
        self.assertEqual(state["sender_domain"], "letter.example")
        self.assertNotIn("sender_address", state)
        self.assertNotIn("https://", state["body_excerpt"])

    def test_disposition_thresholds_and_bulk_evidence(self):
        C = jev.Classification
        self.assertEqual(jev.disposition(C("newsletter", .9, "bulk_or_automated", .95), True)[0], "trash")
        self.assertEqual(jev.disposition(C("newsletter", .9, "bulk_or_automated", .95), False)[0], "review")
        self.assertEqual(jev.disposition(C("newsletter", .8, "bulk_or_automated", .95), True)[0], "review")
        self.assertEqual(jev.disposition(C("promotion", .9, "bulk_or_automated", .89), True)[0], "review")
        self.assertEqual(jev.disposition(C("receipt", .9, "bulk_or_automated", .99), True)[:2], ("archive", "收據"))
        self.assertEqual(jev.disposition(C("personal", .9, "private_or_unclear", .9), False)[:2], ("archive", "個人"))
        self.assertEqual(jev.disposition(C("phishing", .99, "bulk_or_automated", .99), True)[0], "review")
        self.assertEqual(jev.disposition(C("account", .9, "bulk_or_automated", .99), False)[0], "trash_if_old")
        self.assertEqual(jev.disposition(C("account", .7, "bulk_or_automated", .99), False)[0], "review")

    def test_classified_messages_flow_into_plan_and_review_queue(self):
        jev.classify_pending(self.conn, "outlook:acct", ["inbox1", "inbox2"], jev_client("newsletter"), lambda _id: "")
        # Already classified messages are not sent again.
        again = jev_client("newsletter")
        self.assertEqual(jev.classify_pending(self.conn, "outlook:acct", ["inbox1"], again, lambda _id: "")["skipped"], 1)
        again.system_one.assert_not_called()
        plan = {i.message_id: i for i in policy.build_plan(self.conn, "outlook:acct", now=NOW)}
        self.assertEqual(plan["inbox1"].action, "trash")      # has bulk evidence
        self.assertEqual(plan["inbox2"].action, "review")     # Jev sure, but no local evidence
        self.assertEqual(review.enqueue(self.conn, "outlook:acct", list(plan.values())), 1)
        self.assertEqual(review.enqueue(self.conn, "outlook:acct", list(plan.values())), 0)

    def test_review_decision_can_create_a_rule(self):
        jev.classify_pending(self.conn, "outlook:acct", ["inbox2"], jev_client("newsletter"), lambda _id: "")
        review.enqueue(self.conn, "outlook:acct", policy.build_plan(self.conn, "outlook:acct", now=NOW))
        item = review.pending(self.conn, "outlook:acct")[0]
        rule_id = review.resolve(self.conn, "outlook:acct", item["id"], "trash", rule_scope="domain")
        rule = rules.active_rules(self.conn)[0]
        self.assertEqual((rule.id, rule.scope, rule.sender, rule.action), (rule_id, "domain", "letter.example", "trash"))
        self.assertEqual(review.pending(self.conn, "outlook:acct"), [])
        with self.assertRaises(ValueError):
            review.resolve(self.conn, "outlook:acct", item["id"], "trash")

    def test_reviews_decided_by_a_new_rule_are_closed(self):
        jev.classify_pending(self.conn, "outlook:acct", ["inbox2"], jev_client("newsletter"), lambda _id: "")
        review.enqueue(self.conn, "outlook:acct", policy.build_plan(self.conn, "outlook:acct", now=NOW))
        rules.add_rule(self.conn, "address", "editor@letter.example", "trash", "user")
        plan = policy.build_plan(self.conn, "outlook:acct", now=NOW)
        self.assertEqual(review.prune(self.conn, "outlook:acct", plan), 1)
        self.assertEqual(review.pending(self.conn, "outlook:acct"), [])

    def test_one_off_review_decisions_stick_without_a_rule(self):
        jev.classify_pending(self.conn, "outlook:acct", ["inbox2", "inbox3"], jev_client("newsletter", 0.5), lambda _id: "")
        review.enqueue(self.conn, "outlook:acct", policy.build_plan(self.conn, "outlook:acct", now=NOW))
        ids = {i["subject"]: i["id"] for i in review.pending(self.conn, "outlook:acct")}
        review.resolve(self.conn, "outlook:acct", ids["Weekly digest 2"], "trash")
        review.resolve(self.conn, "outlook:acct", ids["週末聚餐"], "keep")
        plan = {i.message_id: i for i in policy.build_plan(self.conn, "outlook:acct", now=NOW)}
        self.assertEqual((plan["inbox2"].action, plan["inbox3"].action), ("trash", "keep"))
        self.assertEqual(review.enqueue(self.conn, "outlook:acct", list(plan.values())), 0)

    def test_drafts_do_nothing_until_approved(self):
        draft = rules.propose(self.conn, "domain", "letter.example", "trash", "weekly newsletter")
        self.assertEqual(rules.active_rules(self.conn), [])
        listed = rules.drafts(self.conn, "outlook:acct")
        self.assertEqual((listed[0]["id"], listed[0]["matches"]), (draft, 2))
        rules.decide_draft(self.conn, draft, approve=True)
        self.assertEqual(rules.active_rules(self.conn)[0].id, draft)
        with self.assertRaises(ValueError):
            rules.decide_draft(self.conn, draft, approve=False)


class McpSurfaceTests(unittest.TestCase):
    def test_owner_only_decisions_are_not_exposed(self):
        from email_cleaner.mcp_server import server
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertIn("propose_rule", names)
        words = {word for name in names for word in name.split("_")}
        for forbidden in ("resolve", "decide", "unprotect", "protection", "delete", "send"):
            self.assertNotIn(forbidden, words)

    def test_mcp_cannot_draft_trash_for_a_protected_sender(self):
        from email_cleaner import mcp_server
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"EMAIL_CLEANER_DATA": folder}):
            conn = db.connect(Path(folder) / "email-cleaner.sqlite")
            index.sync(conn, Fake([msg(1, "boss@client.example")],
                                  [msg(9, "me@x.example", folder="sent", recipients=("boss@client.example",))]))
            conn.close()
            with self.assertRaises(ValueError):
                mcp_server.propose_rule("address", "boss@client.example", "trash", "x")
            draft = mcp_server.propose_rule("address", "ads@shop.example", "trash", "ads")
            self.assertEqual(draft["status"], "draft")


if __name__ == "__main__":
    unittest.main()
