"""Gmail provider: read-only listing plus a writer that can only relabel or trash.

Gmail has labels, not folders. The mailbox "folders" of this project map like this:
  inbox          -> the INBOX label
  封存 / Archive -> no label at all (the INBOX label is removed)
  trash          -> the TRASH label (recoverable for 30 days)
  any other name -> a user label; filing adds it and removes INBOX
Message IDs never change in Gmail, so "new_message_id" is the same ID.

Reading uses only the `gmail.readonly` scope. Writing needs `gmail.modify`, which can
relabel and trash but cannot delete permanently or send. The writer also refuses any
request outside four calls: modify, trash, untrash and label creation.
"""

from __future__ import annotations

import base64
import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from typing import Any, Callable, Iterator

import requests

from email_cleaner.config import data_dir
from email_cleaner.providers.base import MessageMeta

API = "https://gmail.googleapis.com/gmail/v1/users/me"
PEOPLE = "https://people.googleapis.com/v1/people/me/connections"
READ_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
               "https://www.googleapis.com/auth/contacts.readonly"]
WRITE_SCOPES = ["https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/contacts.readonly"]
LABELS = {"inbox": "INBOX", "sent": "SENT"}
PAGE_SIZE = 500
WORKERS = 8
MAX_RETRIES = 5
MAX_RETRY_AFTER = 60
# Headers the signals need; everything else stays on Google's side.
HEADERS = ("From", "To", "Cc", "Bcc", "Subject", "Message-ID", "Reply-To", "List-Unsubscribe",
           "List-Unsubscribe-Post", "List-Id", "Precedence", "Auto-Submitted", "Authentication-Results",
           "Received-SPF")
ARCHIVE = "ARCHIVE"  # sentinel destination: remove INBOX, add nothing
ARCHIVE_NAMES = {"archive", "封存"}
# Labels Gmail owns; this tool never creates, reuses or filters into them.
_SYSTEM_LABELS = {"inbox", "sent", "trash", "spam", "draft", "drafts", "unread", "starred", "important",
                  "chat", "scheduled", "snoozed", "all mail"}
# Labels this tool files mail under, removed again when a batch is undone.
FILING_LABELS = ("客戶", "個人", "投資", "電子報", "發票", "銀行", "收據", "語言教學", "重要信")
_ALLOWED_POST = re.compile(r"^/(?:messages/[^/]+/(?:modify|trash|untrash)|labels)$")
TOKEN_FILE = "gmail-token.json"


class LoginRequired(RuntimeError):
    """Raised when no stored Google sign-in exists."""


class GmailAuth:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 scopes: list[str] | None = None) -> None:
        self.client_id = (client_id or os.environ.get("GMAIL_CLIENT_ID", "")).strip()
        self.client_secret = (client_secret or os.environ.get("GMAIL_CLIENT_SECRET", "")).strip()
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env "
                               "(see docs/GMAIL_SETUP.md).")
        self.scopes = scopes or READ_SCOPES
        self.token_path = data_dir() / TOKEN_FILE

    def _config(self) -> dict[str, Any]:
        return {"installed": {
            "client_id": self.client_id, "client_secret": self.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}

    def token(self) -> str:
        """A valid access token, refreshed from the stored refresh token when needed."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        if not self.token_path.exists():
            raise LoginRequired("Gmail sign-in required: run `email-cleaner login gmail`.")
        creds = Credentials.from_authorized_user_file(str(self.token_path), self.scopes)
        if not creds.valid:
            if not creds.refresh_token:
                raise LoginRequired("Gmail sign-in expired: run `email-cleaner login gmail`.")
            creds.refresh(Request())
            self._save(creds.to_json())
        return creds.token

    def _save(self, text: str) -> None:
        self.token_path.write_text(text, encoding="utf-8")
        self.token_path.chmod(0o600)

    def login(self, emit: Callable[[str], None] = print) -> None:
        """Browser sign-in on this machine. Google does not allow Gmail scopes on the device flow."""
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(self._config(), self.scopes)
        emit("Open the link below in a browser on THIS computer and approve the access.")
        creds = flow.run_local_server(port=0, open_browser=False, prompt="consent", access_type="offline",
                                      authorization_prompt_message="{url}",
                                      success_message="Sign-in complete. You can close this tab.")
        self._save(creds.to_json())


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # noqa: BLE001 - keep the raw value when a header is malformed
        return value.strip()


def _iso(internal_date_ms: str | None) -> str | None:
    if not internal_date_ms:
        return None
    moment = datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def to_meta(raw: dict[str, Any], folder: str) -> MessageMeta:
    payload = raw.get("payload") or {}
    headers = tuple((h.get("name") or "", h.get("value") or "") for h in payload.get("headers") or [])
    by_name = {name.casefold(): value for name, value in headers}
    name, address = parseaddr(by_name.get("from", ""))  # parse first: decoded names may contain commas
    # getaddresses returns nothing at all when it is handed two empty values in a row, so drop them first.
    recipient_headers = [by_name[key] for key in ("to", "cc", "bcc") if by_name.get(key)]
    recipients = tuple(addr.strip().casefold() for _, addr in getaddresses(recipient_headers) if addr.strip())
    labels = raw.get("labelIds") or []
    categories = tuple(f"gmail:{label.removeprefix('CATEGORY_').casefold()}"
                       for label in labels if label.startswith("CATEGORY_"))
    return MessageMeta(
        message_id=raw["id"],
        folder=folder,
        internet_message_id=by_name.get("message-id", "").strip(),
        sender_address=address.strip().casefold(),
        sender_name=_decode(name),
        subject=_decode(by_name.get("subject", "")),
        received_at=_iso(raw.get("internalDate")),
        is_read="UNREAD" not in labels,
        has_attachments=str(payload.get("mimeType", "")).startswith("multipart/mixed"),
        categories=categories,
        headers=headers,
        preview=html.unescape(raw.get("snippet") or ""),
        recipients=recipients,
    )


class _Http:
    """Shared request code: bearer token, retry on throttling, only GET and POST."""

    def __init__(self, auth: Any, session: requests.Session | None,
                 sleep: Callable[[float], None]) -> None:
        self.auth, self.session, self.sleep = auth, session or requests.Session(), sleep

    def _request(self, method: str, url: str, params: Any = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self.auth.token()}", "Accept": "application/json"}
            if method == "GET":
                response = self.session.get(url, params=params, timeout=45, headers=headers)
            else:
                response = self.session.post(url, params=params, json=body, timeout=45, headers=headers)
            throttled = response.status_code in (429, 500, 503) or (
                response.status_code == 403 and "ratelimit" in (response.text or "").casefold())
            if throttled and attempt < MAX_RETRIES:
                try:
                    wait = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    wait = 2 ** attempt
                self.sleep(min(max(wait, 1), MAX_RETRY_AFTER))
                continue
            response.raise_for_status()
            return response.json() if response.content else {}
        raise RuntimeError("Gmail kept throttling; try again later.")


class GmailProvider(_Http):
    name = "gmail"

    def __init__(self, auth: Any, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(auth, session, sleep)
        self._account: str | None = None

    def _get(self, path: str, params: Any = None) -> dict[str, Any]:
        return self._request("GET", API + path, params)  # GET only: reading cannot change the mailbox

    def account_id(self) -> str:
        if self._account is None:
            self._account = "gmail:" + self._get("/profile")["emailAddress"].casefold()
        return self._account

    def _fetch(self, message_id: str) -> dict[str, Any]:
        return self._get(f"/messages/{message_id}", {"format": "metadata", "metadataHeaders": list(HEADERS)})

    def list_messages(self, folder: str, received_after: str | None = None) -> Iterator[MessageMeta]:
        if folder not in LABELS:
            raise ValueError(f"Unknown folder: {folder}")
        params: dict[str, Any] = {"labelIds": LABELS[folder], "maxResults": PAGE_SIZE}
        if received_after:
            params["q"] = f"after:{_epoch(received_after)}"
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            while True:
                page = self._get("/messages", params)
                ids = [item["id"] for item in page.get("messages") or []]
                for raw in pool.map(self._fetch, ids):  # keeps Gmail's newest-first order
                    yield to_meta(raw, folder)
                if not page.get("nextPageToken"):
                    return
                params = {**params, "pageToken": page["nextPageToken"]}

    def get_body_text(self, message_id: str) -> str:
        """Plain-text body for one message, fetched on demand and never stored."""
        raw = self._get(f"/messages/{message_id}", {"format": "full"})
        found: dict[str, str] = {}

        def walk(part: dict[str, Any]) -> None:
            mime, data = part.get("mimeType", ""), (part.get("body") or {}).get("data")
            if data and mime in ("text/plain", "text/html") and mime not in found:
                found[mime] = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            for child in part.get("parts") or []:
                walk(child)

        walk(raw.get("payload") or {})
        return found.get("text/plain") or found.get("text/html") or ""

    def list_contact_addresses(self) -> Iterator[str]:
        params: dict[str, Any] = {"personFields": "emailAddresses", "pageSize": 1000}
        try:
            while True:
                page = self._request("GET", PEOPLE, params)
                for person in page.get("connections") or []:
                    for entry in person.get("emailAddresses") or []:
                        address = (entry.get("value") or "").strip().casefold()
                        if address:
                            yield address
                if not page.get("nextPageToken"):
                    return
                params = {**params, "pageToken": page["nextPageToken"]}
        except requests.HTTPError:
            return  # People API not enabled or not granted: sent-to evidence still protects clients


class GmailWriter(_Http):
    """Implements the executor's Writer protocol with labels instead of folders."""

    def __init__(self, auth: Any, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(auth, session, sleep)
        self._account: str | None = None
        self._labels: dict[str, str] | None = None

    def _call(self, method: str, path: str, params: Any = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if method not in ("GET", "POST"):
            raise PermissionError(f"HTTP method not allowed: {method}")
        if method == "POST" and not _ALLOWED_POST.match(path):
            raise PermissionError(f"Write call not allowed: {path}")
        return self._request(method, API + path, params, body)

    def account_id(self) -> str:
        if self._account is None:
            self._account = "gmail:" + self._call("GET", "/profile")["emailAddress"].casefold()
        return self._account

    def get_message(self, message_id: str) -> dict[str, Any]:
        raw = self._call("GET", f"/messages/{message_id}",
                         {"format": "metadata", "metadataHeaders": ["From", "Message-ID"]})
        labels = raw.get("labelIds") or []
        headers = {(h.get("name") or "").casefold(): h.get("value") or ""
                   for h in (raw.get("payload") or {}).get("headers") or []}
        _, address = parseaddr(headers.get("from", ""))
        place = "INBOX" if "INBOX" in labels else "TRASH" if "TRASH" in labels else (labels or ["ARCHIVE"])[0]
        return {"id": raw["id"], "internetMessageId": headers.get("message-id", "").strip(),
                "parentFolderId": place, "labelIds": labels,
                "from": {"emailAddress": {"address": address}}}

    def folder_id(self, well_known: str) -> str:
        return {"inbox": "INBOX", "deleteditems": "TRASH"}[well_known]

    def _label_ids(self) -> dict[str, str]:
        if self._labels is None:
            listing = self._call("GET", "/labels").get("labels") or []
            self._labels = {item["name"].casefold(): item["id"] for item in listing}
        return self._labels

    def ensure_folder(self, display_name: str) -> str:
        """Find or create a user label. Archive names mean "no label": just leave the Inbox."""
        name = display_name.strip()
        if not name or name.casefold() in _SYSTEM_LABELS:
            raise ValueError(f"Not a usable label: {display_name!r}")
        if name.casefold() in ARCHIVE_NAMES:
            return ARCHIVE
        labels = self._label_ids()
        if name.casefold() not in labels:
            created = self._call("POST", "/labels", body={
                "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})
            labels[name.casefold()] = created["id"]
        return labels[name.casefold()]

    def _modify(self, message_id: str, add: list[str], remove: list[str]) -> None:
        self._call("POST", f"/messages/{message_id}/modify", body={"addLabelIds": add, "removeLabelIds": remove})

    def move(self, message_id: str, destination_id: str) -> str:
        """File, trash or restore a message. Returns its ID, which Gmail never changes."""
        if destination_id == "TRASH":
            self._call("POST", f"/messages/{message_id}/trash")
        elif destination_id == ARCHIVE:
            self._modify(message_id, [], ["INBOX"])
        elif destination_id == "INBOX":
            current = self.get_message(message_id)
            if "TRASH" in current["labelIds"]:
                self._call("POST", f"/messages/{message_id}/untrash")
            filed = [self._label_ids()[name.casefold()] for name in FILING_LABELS if name.casefold() in self._label_ids()]
            self._modify(message_id, ["INBOX"], [label for label in filed if label in current["labelIds"]])
        else:
            self._modify(message_id, [destination_id], ["INBOX"])
        return message_id
