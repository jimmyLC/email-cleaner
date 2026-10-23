"""Provider-neutral message metadata. Bodies are never part of this record."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol


@dataclass(frozen=True)
class MessageMeta:
    message_id: str
    folder: str
    internet_message_id: str = ""
    sender_address: str = ""
    sender_name: str = ""
    subject: str = ""
    received_at: str | None = None
    is_read: bool = False
    has_attachments: bool = False
    # Provider hints: Gmail categories, Outlook focused/other.
    categories: tuple[str, ...] = ()
    # Evidence used to compute signals; not stored in the index.
    headers: tuple[tuple[str, str], ...] = ()
    preview: str = ""
    recipients: tuple[str, ...] = field(default=())


class Provider(Protocol):
    name: str

    def account_id(self) -> str: ...

    def list_messages(self, folder: str, received_after: str | None = None) -> Iterator[MessageMeta]: ...

    def list_contact_addresses(self) -> Iterator[str]: ...
