"""Offline checks for the Gmail provider and writer against an in-memory fake Gmail."""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from email_cleaner import db, index, policy, rules
from email_cleaner.executor import Executor
from email_cleaner.providers import gmail

AUTH = SimpleNamespace(token=lambda: "t")


class Response:
    def __init__(self, payload=None, status=200, text=""):
        self.status_code, self._payload, self.text, self.headers = status, payload, text, {}
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def raw(message_id, labels=("INBOX",), sender="News <news@shop.example>", subject="Hello", date=1700000000000, **kw):
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
               {"name": "Message-ID", "value": f"<{message_id}@x>"}]
    headers += [{"name": k, "value": v} for k, v in kw.pop("extra", {}).items()]
    return {"id": message_id, "labelIds": list(labels), "internalDate": str(date), "snippet": kw.pop("snippet", ""),
            "payload": {"mimeType": kw.pop("mime", "text/plain"), "headers": headers}}


class FakeGmail:
    """Just enough of the Gmail API; any call it does not know is a test failure."""

    def __init__(self, messages, labels=()):
        self.messages = {m["id"]: m for m in messages}
        self.labels = {name: f"Label_{i}" for i, name in enumerate(labels, 1)}
        self.calls = []

    def _path(self, url):
        return url.replace(gmail.API, "")

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("GET", url, params))
        if url == gmail.PEOPLE:
            return Response({"connections": [{"emailAddresses": [{"value": " Friend@Mail.Example "}, {"value": ""}]}]})
        path = self._path(url)
        if path == "/profile":
            return Response({"emailAddress": "Me@Example.com"})
        if path == "/labels":
            return Response({"labels": [{"id": i, "name": n} for n, i in self.labels.items()]})
        if path == "/messages":
            matching = sorted((m for m in self.messages.values() if params["labelIds"] in m["labelIds"]),
                              key=lambda m: -int(m["internalDate"]))
            if "q" in params:
                floor = int(params["q"].removeprefix("after:")) * 1000
                matching = [m for m in matching if int(m["internalDate"]) >= floor]
            start, size = int(params.get("pageToken", 0)), params["maxResults"]
            page = {"messages": [{"id": m["id"]} for m in matching[start:start + size]]}
            if start + size < len(matching):
                page["nextPageToken"] = str(start + size)
            return Response(page)
        if path.startswith("/messages/"):
            return Response(self.messages[path.rsplit("/", 1)[1]])
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, params=None, json=None, timeout=None, headers=None):
        self.calls.append(("POST", url, json))
        path = self._path(url)
        if path == "/labels":
            self.labels[json["name"]] = f"Label_{len(self.labels) + 1}"
            return Response({"id": self.labels[json["name"]], "name": json["name"]})
        _, _, message_id, verb = path.split("/")
        labels = self.messages[message_id]["labelIds"]
        if verb == "trash":
            self.messages[message_id]["labelIds"] = [x for x in labels if x != "INBOX"] + ["TRASH"]
        elif verb == "untrash":
            self.messages[message_id]["labelIds"] = [x for x in labels if x != "TRASH"]
        elif verb == "modify":
            kept = [x for x in labels if x not in json["removeLabelIds"]]
            self.messages[message_id]["labelIds"] = kept + [x for x in json["addLabelIds"] if x not in kept]
        else:
            raise AssertionError(f"unexpected POST {url}")
        return Response(self.messages[message_id])

    def __getattr__(self, name):
        if name in {"delete", "patch", "put", "request"}:
            raise AssertionError(f"Gmail client attempted {name}")
        raise AttributeError(name)


def provider(fake):
    return gmail.GmailProvider(AUTH, fake, lambda _s: None)


class GmailProviderTests(unittest.TestCase):
    def test_message_fields_are_decoded_and_signals_have_their_headers(self):
        encoded = "=?UTF-8?B?" + base64.b64encode("週報".encode()).decode() + "?="
        message = raw("m1", labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS"),
                      sender=f'"Smith, Jo" <Jo@Shop.Example>', subject=encoded, snippet="Tom &amp; Jerry",
                      mime="multipart/mixed",
                      extra={"To": "Me <me@example.com>, other@example.com", "List-Unsubscribe": "<https://x>"})
        meta = gmail.to_meta(message, "inbox")
        self.assertEqual((meta.sender_address, meta.sender_name, meta.subject), ("jo@shop.example", "Smith, Jo", "週報"))
        self.assertEqual(meta.recipients, ("me@example.com", "other@example.com"))
        self.assertEqual(meta.categories, ("gmail:promotions",))
        self.assertFalse(meta.is_read)
        self.assertTrue(meta.has_attachments)
        self.assertEqual(meta.preview, "Tom & Jerry")
        self.assertEqual(meta.received_at, "2023-11-14T22:13:20Z")
        self.assertIn(("List-Unsubscribe", "<https://x>"), meta.headers)

    def test_listing_pages_newest_first_and_incremental_sync_uses_an_after_query(self):
        fake = FakeGmail([raw(f"m{i}", date=1700000000000 + i * 1000) for i in range(5)] + [raw("s1", ("SENT",))])
        with patch.object(gmail, "PAGE_SIZE", 2):
            ids = [m.message_id for m in provider(fake).list_messages("inbox")]
            newer = [m.message_id for m in provider(fake).list_messages("inbox", "2023-11-14T22:13:22Z")]
        self.assertEqual(ids, ["m4", "m3", "m2", "m1", "m0"])
        self.assertEqual(newer, ["m4", "m3", "m2"])
        self.assertTrue(all(call[0] == "GET" for call in fake.calls))  # reading never writes

    def test_account_id_and_contacts(self):
        p = provider(FakeGmail([]))
        self.assertEqual(p.account_id(), "gmail:me@example.com")
        self.assertEqual(list(p.list_contact_addresses()), ["friend@mail.example"])

    def test_contacts_are_skipped_when_the_people_api_is_unavailable(self):
        fake = FakeGmail([])
        fake.get = lambda url, **kw: Response({}, status=403) if url == gmail.PEOPLE else Response({})
        self.assertEqual(list(provider(fake).list_contact_addresses()), [])

    def test_body_text_prefers_plain_over_html(self):
        data = lambda text: base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
        message = raw("m1")
        message["payload"] = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": data("<b>html</b>")}},
            {"mimeType": "text/plain", "body": {"data": data("plain 內容")}}]}
        self.assertEqual(provider(FakeGmail([message])).get_body_text("m1"), "plain 內容")

    def test_throttled_requests_are_retried_with_the_server_delay(self):
        sleeps, answers = [], [Response({}, status=429), Response({"emailAddress": "A@B.C"})]
        answers[0].headers = {"Retry-After": "3"}
        session = SimpleNamespace(get=lambda *a, **k: answers.pop(0))
        self.assertEqual(gmail.GmailProvider(AUTH, session, sleeps.append).account_id(), "gmail:a@b.c")
        self.assertEqual(sleeps, [3.0])

    def test_login_needs_client_credentials_and_a_stored_token(self):
        with patch.dict(os.environ, {"GMAIL_CLIENT_ID": "", "GMAIL_CLIENT_SECRET": ""}):
            with self.assertRaises(RuntimeError):
                gmail.GmailAuth()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"EMAIL_CLEANER_DATA": folder}):
            with self.assertRaises(gmail.LoginRequired):
                gmail.GmailAuth("id", "secret").token()


class GmailWriterTests(unittest.TestCase):
    def writer(self, fake):
        return gmail.GmailWriter(AUTH, fake, lambda _s: None)

    def test_only_four_write_calls_exist(self):
        writer = self.writer(FakeGmail([raw("m1")]))
        for path in ("/messages/m1", "/messages/m1/send", "/messages/batchDelete", "/messages/m1/delete",
                     "/drafts/send", "/messages/send"):
            with self.assertRaises(PermissionError):
                writer._call("POST", path)
        for method in ("DELETE", "PATCH", "PUT"):
            with self.assertRaises(PermissionError):
                writer._call(method, "/messages/m1")
        self.assertFalse(any(hasattr(writer, name) for name in ("delete", "send", "permanent_delete")))
        self.assertIn("gmail.modify", gmail.WRITE_SCOPES[0])
        self.assertNotIn("https://mail.google.com/", gmail.WRITE_SCOPES)  # the scope that can delete for good

    def test_labels_are_created_once_and_system_labels_are_refused(self):
        fake = FakeGmail([raw("m1")], labels=["投資"])
        writer = self.writer(fake)
        self.assertEqual(writer.ensure_folder("投資"), "Label_1")            # reused, not created
        first = writer.ensure_folder("電子報")
        self.assertEqual(writer.ensure_folder("電子報"), first)
        self.assertEqual(sum(1 for call in fake.calls if call[0] == "POST"), 1)
        for name in ("INBOX", "Trash", "spam", " "):
            with self.assertRaises(ValueError):
                writer.ensure_folder(name)

    def test_archive_names_never_create_a_label(self):
        fake = FakeGmail([])
        writer = self.writer(fake)
        self.assertEqual((writer.ensure_folder("封存"), writer.ensure_folder("Archive")), ("ARCHIVE", "ARCHIVE"))
        self.assertEqual(fake.calls, [])


class GmailEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.folder.name) / "s.sqlite")
        self.fake = FakeGmail([
            raw("ads", sender="ads@shop.example", date=1600000000000, extra={"List-Unsubscribe": "<https://x>"}),
            raw("boss", sender="Boss <boss@client.example>", date=1600000001000),
            raw("memo", sender="memo@news.example", date=1600000002000),
            raw("sent1", ("SENT",), sender="Me <me@example.com>", date=1600000003000,
                extra={"To": "boss@client.example"})])
        index.sync(self.conn, provider(self.fake))
        rules.add_rule(self.conn, "address", "ads@shop.example", "trash", "user")
        rules.add_rule(self.conn, "address", "memo@news.example", "archive", "user", None, "電子報")
        self.writer = gmail.GmailWriter(AUTH, self.fake, lambda _s: None)
        self.account = "gmail:me@example.com"

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def labels(self, message_id):
        return set(self.fake.messages[message_id]["labelIds"])

    def test_gmail_mail_is_indexed_under_a_gmail_account(self):
        self.assertEqual(tuple(self.conn.execute("SELECT id, provider FROM accounts").fetchone()), (self.account, "gmail"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM messages WHERE folder = 'inbox'").fetchone()[0], 3)
        self.assertEqual(index.protection_reason(self.conn, self.account, "boss@client.example"), "曾寄信給對方")

    def test_plan_runs_through_the_executor_and_can_be_undone(self):
        plan = policy.build_plan(self.conn, self.account, finish=True)
        result = Executor(self.conn, self.writer, self.account, "user").execute(plan)
        self.assertEqual((result.moved, result.failed, result.blocked), (3, 0, 0))
        self.assertEqual(self.labels("ads"), {"TRASH"})                      # trashed, not deleted
        self.assertEqual(self.labels("boss"), {self.fake.labels["客戶"]})    # filed under 客戶, left the Inbox
        self.assertIn(self.fake.labels["電子報"], self.labels("memo"))
        self.assertTrue(all("INBOX" not in self.labels(m) for m in ("ads", "boss", "memo")))
        self.assertEqual(self.conn.execute("SELECT folder FROM messages WHERE message_id = 'ads'").fetchone()[0], "trash")
        restored = Executor(self.conn, self.writer, self.account, "user").undo(result.batch_id)
        self.assertEqual((restored.moved, restored.failed), (3, 0))
        for message_id in ("ads", "boss", "memo"):
            self.assertEqual(self.labels(message_id), {"INBOX"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM messages WHERE folder = 'inbox'").fetchone()[0], 3)

    def test_archive_folder_only_removes_the_inbox_label(self):
        self.conn.execute("UPDATE rules SET status = 'disabled' WHERE target_folder = '電子報'")
        self.conn.commit()
        rules.add_rule(self.conn, "address", "memo@news.example", "archive", "user", None, "封存")
        Executor(self.conn, self.writer, self.account, "user").execute(
            [i for i in policy.build_plan(self.conn, self.account, finish=True) if i.sender == "memo@news.example"])
        self.assertEqual(self.labels("memo"), set())
        self.assertNotIn("封存", self.fake.labels)

    def test_a_trash_rule_never_trashes_a_client(self):
        forged = policy.PlanItem("boss", "boss@client.example", "x", None, "trash", None, "forged")
        result = Executor(self.conn, self.writer, self.account, "user").execute([forged])
        self.assertEqual((result.moved, result.blocked), (0, 1))
        self.assertEqual(self.labels("boss"), {"INBOX"})

    def test_a_message_that_left_the_inbox_by_hand_is_skipped(self):
        self.fake.messages["ads"]["labelIds"] = []  # the owner archived it in Gmail meanwhile
        plan = [i for i in policy.build_plan(self.conn, self.account) if i.sender == "ads@shop.example"]
        result = Executor(self.conn, self.writer, self.account, "user").execute(plan)
        self.assertEqual((result.moved, result.skipped), (0, 1))


if __name__ == "__main__":
    unittest.main()
