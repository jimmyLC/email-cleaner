"""Offline checks for the read-only Graph provider; no network calls."""

import unittest
from types import SimpleNamespace

from email_cleaner.providers import outlook


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status, payload or {}, headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Records GETs and fails loudly on any write method."""

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        return self.responses.pop(0)

    def __getattr__(self, name):
        if name in {"post", "patch", "put", "delete", "request"}:
            raise AssertionError(f"read-only provider attempted {name}")
        raise AttributeError(name)


def raw(i, **extra):
    return {"id": f"m{i}", "internetMessageId": f"<{i}@x>", "subject": f"S{i}",
            "from": {"emailAddress": {"address": "News@Shop.Example", "name": "Shop"}},
            "receivedDateTime": "2026-01-01T00:00:00Z", "isRead": False, "hasAttachments": False,
            "inferenceClassification": "other",
            "internetMessageHeaders": [{"name": "List-Unsubscribe", "value": "<https://x>"}],
            "bodyPreview": "Sale", **extra}


def provider(responses):
    sleeps = []
    p = outlook.OutlookProvider(SimpleNamespace(token=lambda: "t"), FakeSession(responses), sleeps.append)
    return p, sleeps


class OutlookTests(unittest.TestCase):
    def test_scopes_are_read_only(self):
        self.assertTrue(all(scope.endswith(".Read") for scope in outlook.READ_SCOPES))

    def test_select_never_requests_full_body(self):
        fields = outlook.SELECT.split(",")
        self.assertNotIn("body", fields)
        self.assertNotIn("uniqueBody", fields)
        self.assertIn("internetMessageHeaders", fields)

    def test_pages_follow_next_link_without_reapplying_params(self):
        p, _ = provider([
            FakeResponse(payload={"value": [raw(1)], "@odata.nextLink": "https://graph/next"}),
            FakeResponse(payload={"value": [raw(2)]}),
        ])
        items = list(p.list_messages("inbox"))
        self.assertEqual([m.message_id for m in items], ["m1", "m2"])
        self.assertEqual(p.session.calls[1], ("https://graph/next", None))

    def test_incremental_filter_and_folder_mapping(self):
        p, _ = provider([FakeResponse(payload={"value": []})])
        list(p.list_messages("sent", received_after="2026-02-01T00:00:00Z"))
        url, params = p.session.calls[0]
        self.assertTrue(url.endswith("/mailFolders/sentitems/messages"))
        self.assertEqual(params["$filter"], "receivedDateTime gt 2026-02-01T00:00:00Z")
        with self.assertRaises(ValueError):
            list(p.list_messages("deleteditems"))

    def test_throttling_honours_retry_after(self):
        p, sleeps = provider([FakeResponse(429, headers={"Retry-After": "7"}),
                              FakeResponse(payload={"value": [raw(1)]})])
        self.assertEqual(len(list(p.list_messages("inbox"))), 1)
        self.assertEqual(sleeps, [7.0])

    def test_metadata_mapping(self):
        meta = outlook.to_meta(raw(1, toRecipients=[{"emailAddress": {"address": "Client@Corp.Example"}}]), "sent")
        self.assertEqual(meta.sender_address, "news@shop.example")
        self.assertEqual(meta.recipients, ("client@corp.example",))
        self.assertIn("outlook:other", meta.categories)
        self.assertEqual(meta.headers, (("List-Unsubscribe", "<https://x>"),))

    def test_account_id_is_prefixed_and_cached(self):
        p, _ = provider([FakeResponse(payload={"id": "abc"})])
        self.assertEqual(p.account_id(), "outlook:abc")
        self.assertEqual(p.account_id(), "outlook:abc")
        self.assertEqual(len(p.session.calls), 1)

    def test_contacts_are_normalised(self):
        p, _ = provider([FakeResponse(payload={"value": [
            {"emailAddresses": [{"address": " A@Client.Example "}, {"address": ""}]}]})])
        self.assertEqual(list(p.list_contact_addresses()), ["a@client.example"])

    def test_writer_allows_only_move_and_folder_creation(self):
        writer = outlook.OutlookWriter(SimpleNamespace(token=lambda: "t"), FakeSession([]))
        for path in ("/me/messages/x", "/me/sendMail", "/me/messages/x/permanentDelete", "/me/messages/x/send"):
            with self.assertRaises(PermissionError):
                writer._call("POST", path)
        for method in ("DELETE", "PATCH"):
            with self.assertRaises(PermissionError):
                writer._call(method, "/me/messages/x")
        self.assertFalse(any(hasattr(writer, name) for name in ("delete", "send", "permanent_delete")))
        with self.assertRaises(ValueError):
            writer.ensure_folder("DeletedItems")

    def test_archive_names_use_outlooks_own_archive_folder_and_never_create_one(self):
        session = FakeSession([FakeResponse(payload={"id": "ARCHIVE"})])
        writer = outlook.OutlookWriter(SimpleNamespace(token=lambda: "t"), session)
        self.assertEqual(writer.ensure_folder("封存"), "ARCHIVE")
        self.assertEqual(writer.ensure_folder("Archive"), "ARCHIVE")
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0][0].endswith("/me/mailFolders/archive"))


if __name__ == "__main__":
    unittest.main()
