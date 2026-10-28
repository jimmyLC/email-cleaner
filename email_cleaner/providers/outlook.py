"""Read-only Microsoft Graph provider for Outlook.

Phase 1 asks only for read scopes, and the HTTP layer refuses anything but GET,
so this module cannot change a mailbox even by mistake.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Iterator
from urllib.parse import quote

import msal
import requests

from email_cleaner.config import data_dir
from email_cleaner.providers.base import MessageMeta

GRAPH = "https://graph.microsoft.com/v1.0"
READ_SCOPES = ["Mail.Read", "Contacts.Read", "User.Read"]
FOLDERS = {"inbox": "inbox", "sent": "sentitems"}
PAGE_SIZE = 100
MAX_RETRIES = 5
MAX_RETRY_AFTER = 60
# internetMessageHeaders is only returned when named in $select.
SELECT = ",".join((
    "id", "internetMessageId", "subject", "sender", "from", "receivedDateTime", "isRead",
    "hasAttachments", "categories", "inferenceClassification", "internetMessageHeaders",
    "bodyPreview", "toRecipients", "ccRecipients", "bccRecipients",
))
DEVICE_PREFIX = "EMAIL_CLEANER_DEVICE_LOGIN "


class LoginRequired(RuntimeError):
    """Raised when no cached sign-in exists and interactive login is not allowed."""


class OutlookAuth:
    def __init__(self, client_id: str | None = None, authority: str | None = None,
                 scopes: list[str] | None = None) -> None:
        client_id = (client_id or os.environ.get("OUTLOOK_CLIENT_ID", "")).strip()
        if not client_id:
            raise RuntimeError("Set OUTLOOK_CLIENT_ID in .env (Microsoft Entra application ID).")
        self.scopes = scopes or READ_SCOPES
        self.cache_path = data_dir() / "outlook-token-cache.bin"
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text(encoding="utf-8"))
        self.app = msal.PublicClientApplication(
            client_id, token_cache=self.cache,
            authority=authority or os.environ.get("OUTLOOK_AUTHORITY", "https://login.microsoftonline.com/common"),
        )

    def _save(self) -> None:
        if self.cache.has_state_changed:
            self.cache_path.write_text(self.cache.serialize(), encoding="utf-8")
            self.cache_path.chmod(0o600)

    def token(self) -> str:
        """Return a valid access token; MSAL refreshes it when close to expiry."""
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(self.scopes, account=accounts[0]) if accounts else None
        if not result or "access_token" not in result:
            raise LoginRequired("Outlook sign-in required: run `email-cleaner login outlook`.")
        self._save()
        return result["access_token"]

    def device_login(self, emit: Callable[[str], None] = print) -> None:
        flow = self.app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise RuntimeError("Could not start Microsoft sign-in.")
        # Structured line for a future Web UI, then Microsoft's own message.
        emit(DEVICE_PREFIX + json.dumps({"url": flow.get("verification_uri"), "code": flow["user_code"]}))
        emit(flow["message"])
        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError("Microsoft sign-in did not complete.")
        self._save()


def _address(value: dict[str, Any] | None) -> tuple[str, str]:
    email = (value or {}).get("emailAddress") or {}
    return (email.get("address") or "").strip().casefold(), (email.get("name") or "").strip()


def to_meta(raw: dict[str, Any], folder: str) -> MessageMeta:
    address, name = _address(raw.get("from") or raw.get("sender"))
    recipients = tuple(
        addr for key in ("toRecipients", "ccRecipients", "bccRecipients")
        for addr, _ in map(_address, raw.get(key) or []) if addr
    )
    categories = tuple(raw.get("categories") or ())
    if raw.get("inferenceClassification"):
        categories += (f"outlook:{raw['inferenceClassification']}",)
    return MessageMeta(
        message_id=raw["id"],
        folder=folder,
        internet_message_id=raw.get("internetMessageId") or "",
        sender_address=address,
        sender_name=name,
        subject=raw.get("subject") or "",
        received_at=raw.get("receivedDateTime"),
        is_read=bool(raw.get("isRead")),
        has_attachments=bool(raw.get("hasAttachments")),
        categories=categories,
        headers=tuple((h.get("name") or "", h.get("value") or "")
                      for h in raw.get("internetMessageHeaders") or []),
        preview=raw.get("bodyPreview") or "",
        recipients=recipients,
    )


class OutlookProvider:
    name = "outlook"

    def __init__(self, auth: OutlookAuth, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.auth = auth
        self.session = session or requests.Session()
        self.sleep = sleep
        self._account: str | None = None

    def _get(self, url: str, params: dict[str, str] | None = None,
             extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        # GET only: this provider is read-only by construction.
        for attempt in range(MAX_RETRIES + 1):
            response = self.session.get(url, params=params, timeout=45, headers={
                "Authorization": f"Bearer {self.auth.token()}",
                "Accept": "application/json",
                **(extra_headers or {}),
            })
            if response.status_code in (429, 503, 504) and attempt < MAX_RETRIES:
                try:
                    wait = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    wait = 2 ** attempt
                self.sleep(min(max(wait, 1), MAX_RETRY_AFTER))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Microsoft Graph kept throttling; try again later.")

    def _pages(self, url: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        next_url: str | None = url
        next_params: dict[str, str] | None = params
        while next_url:
            page = self._get(next_url, next_params)
            yield from page.get("value", [])
            # nextLink already carries the query string.
            next_url, next_params = page.get("@odata.nextLink"), None

    def account_id(self) -> str:
        if self._account is None:
            self._account = "outlook:" + self._get(f"{GRAPH}/me", {"$select": "id"})["id"]
        return self._account

    def list_messages(self, folder: str, received_after: str | None = None) -> Iterator[MessageMeta]:
        if folder not in FOLDERS:
            raise ValueError(f"Unknown folder: {folder}")
        params = {"$select": SELECT, "$top": str(PAGE_SIZE), "$orderby": "receivedDateTime desc"}
        if received_after:
            params["$filter"] = f"receivedDateTime gt {received_after}"
        url = f"{GRAPH}/me/mailFolders/{quote(FOLDERS[folder])}/messages"
        for raw in self._pages(url, params):
            yield to_meta(raw, folder)

    def get_body_text(self, message_id: str) -> str:
        """Plain-text body for one message, fetched on demand and never stored."""
        raw = self._get(f"{GRAPH}/me/messages/{quote(message_id, safe='')}", {"$select": "body"},
                        {"Prefer": 'outlook.body-content-type="text"'})
        return ((raw.get("body") or {}).get("content")) or ""

    def list_contact_addresses(self) -> Iterator[str]:
        for contact in self._pages(f"{GRAPH}/me/contacts", {"$select": "emailAddresses", "$top": str(PAGE_SIZE)}):
            for entry in contact.get("emailAddresses") or []:
                address = (entry.get("address") or "").strip().casefold()
                if address:
                    yield address


# --- Writes -----------------------------------------------------------------
# Separate from the read-only provider. Only two write calls exist: move a
# message, and create a top-level mail folder. There is no delete or send.

WRITE_SCOPES = ["Mail.ReadWrite", "Contacts.Read", "User.Read"]
_ALLOWED_POST = re.compile(r"^/me/(?:messages/[^/]+/move|mailFolders)$")
_RESERVED_FOLDERS = {"inbox", "deleteditems", "junkemail", "sentitems", "drafts", "outbox"}
# Outlook's own Archive folder (shown as 封存 in zh-TW). Filing here never creates a folder.
_ARCHIVE_NAMES = {"archive", "封存"}


class OutlookWriter:
    def __init__(self, auth: OutlookAuth, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.auth = auth
        self.session = session or requests.Session()
        self.sleep = sleep
        self._folders: dict[str, str] = {}
        self._account: str | None = None

    def _call(self, method: str, path: str, params: dict[str, str] | None = None,
              body: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "POST" and not _ALLOWED_POST.match(path):
            raise PermissionError(f"Write call not allowed: {path}")
        if method not in ("GET", "POST"):
            raise PermissionError(f"HTTP method not allowed: {method}")
        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self.auth.token()}", "Accept": "application/json"}
            if method == "GET":
                response = self.session.get(GRAPH + path, params=params, timeout=45, headers=headers)
            else:
                response = self.session.post(GRAPH + path, json=body, timeout=45, headers=headers)
            if response.status_code in (429, 503, 504) and attempt < MAX_RETRIES:
                try:
                    wait = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    wait = 2 ** attempt
                self.sleep(min(max(wait, 1), MAX_RETRY_AFTER))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Microsoft Graph kept throttling; try again later.")

    def account_id(self) -> str:
        if self._account is None:
            self._account = "outlook:" + self._call("GET", "/me", {"$select": "id"})["id"]
        return self._account

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self._call("GET", f"/me/messages/{quote(message_id, safe='')}",
                          {"$select": "id,internetMessageId,subject,from,parentFolderId"})

    def folder_id(self, well_known: str) -> str:
        if well_known not in self._folders:
            self._folders[well_known] = self._call("GET", f"/me/mailFolders/{well_known}", {"$select": "id"})["id"]
        return self._folders[well_known]

    def ensure_folder(self, display_name: str) -> str:
        """Find or create a top-level folder by display name and return its ID."""
        key = "name:" + display_name
        if key in self._folders:
            return self._folders[key]
        if not display_name.strip() or display_name.casefold() in _RESERVED_FOLDERS:
            raise ValueError(f"Not a usable archive folder: {display_name!r}")
        if display_name.casefold() in _ARCHIVE_NAMES:
            self._folders[key] = self.folder_id("archive")
            return self._folders[key]
        escaped = display_name.replace("'", "''")
        found = self._call("GET", "/me/mailFolders",
                           {"$filter": f"displayName eq '{escaped}'", "$select": "id,displayName"}).get("value", [])
        folder = found[0] if found else self._call("POST", "/me/mailFolders", body={"displayName": display_name})
        self._folders[key] = folder["id"]
        return folder["id"]

    def move(self, message_id: str, destination_id: str) -> str:
        """Move a message and return its new ID (Graph assigns one on move)."""
        moved = self._call("POST", f"/me/messages/{quote(message_id, safe='')}/move",
                           body={"destinationId": destination_id})
        if not moved.get("id"):
            raise RuntimeError("Graph did not return the moved message ID")
        return moved["id"]
