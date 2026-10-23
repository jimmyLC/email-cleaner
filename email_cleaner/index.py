"""Build the local mail index and answer questions about it.

Sync is read-only against the mailbox. It stores metadata and signals, never
bodies, and records protection evidence (people the owner has written to and
contacts) so client mail can be recognised without asking an AI.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from email_cleaner import signals
from email_cleaner.providers.base import MessageMeta, Provider


@dataclass
class SyncResult:
    inbox: int = 0
    sent: int = 0
    correspondents: int = 0
    contacts: int = 0


def domain_of(address: str) -> str:
    return address.rpartition("@")[2].casefold()


def store_message(conn: sqlite3.Connection, account_id: str, meta: MessageMeta) -> None:
    conn.execute(
        """INSERT INTO messages (account_id, message_id, internet_message_id, folder, sender_address,
               sender_name, subject, received_at, is_read, has_attachments, provider_categories, signals,
               last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT (account_id, message_id) DO UPDATE SET
               folder = excluded.folder, is_read = excluded.is_read,
               provider_categories = excluded.provider_categories, signals = excluded.signals,
               last_seen_at = excluded.last_seen_at""",
        (account_id, meta.message_id, meta.internet_message_id, meta.folder, meta.sender_address,
         meta.sender_name, meta.subject, meta.received_at, int(meta.is_read), int(meta.has_attachments),
         json.dumps(list(meta.categories), ensure_ascii=False),
         json.dumps(signals.collect(meta), ensure_ascii=False)),
    )


def add_correspondents(conn: sqlite3.Connection, account_id: str, addresses: Iterable[str], source: str) -> int:
    rows = {(account_id, address.casefold(), source) for address in addresses if "@" in address}
    conn.executemany("INSERT OR IGNORE INTO known_correspondents (account_id, address, source) VALUES (?, ?, ?)",
                     sorted(rows))
    return len(rows)


def latest_received(conn: sqlite3.Connection, account_id: str, folder: str) -> str | None:
    row = conn.execute("SELECT max(received_at) FROM messages WHERE account_id = ? AND folder = ?",
                       (account_id, folder)).fetchone()
    return row[0]


def sync(conn: sqlite3.Connection, provider: Provider, full: bool = False,
         commit_every: int = 500) -> SyncResult:
    """Index Inbox and Sent Items. Incremental unless `full` is set."""
    account_id = provider.account_id()
    conn.execute("INSERT OR IGNORE INTO accounts (id, provider) VALUES (?, ?)", (account_id, provider.name))
    result = SyncResult()
    for folder in ("inbox", "sent"):
        since = None if full else latest_received(conn, account_id, folder)
        for count, meta in enumerate(provider.list_messages(folder, received_after=since), 1):
            store_message(conn, account_id, meta)
            if folder == "sent":
                result.correspondents += add_correspondents(conn, account_id, meta.recipients, "sent")
            setattr(result, folder, count)
            if count % commit_every == 0:
                conn.commit()  # keep progress if a long first sync is interrupted
        conn.commit()
    result.contacts = add_correspondents(conn, account_id, provider.list_contact_addresses(), "contact")
    conn.commit()
    return result


def protection_reason(conn: sqlite3.Connection, account_id: str, address: str) -> str | None:
    """Why mail from this address is protected, or None. Deterministic, no AI."""
    address = address.casefold()
    sources = {row[0] for row in conn.execute(
        "SELECT source FROM known_correspondents WHERE account_id = ? AND address = ?", (account_id, address))}
    if "sent" in sources:
        return "曾寄信給對方"
    if "contact" in sources:
        return "在聯絡人中"
    if address and conn.execute("SELECT 1 FROM client_domains WHERE domain = ?", (domain_of(address),)).fetchone():
        return "客戶網域"
    return None


def sender_groups(conn: sqlite3.Connection, account_id: str, folder: str = "inbox",
                  limit: int = 50, samples: int = 3) -> list[dict]:
    """Senders ranked by message count, with protection and evidence summary."""
    rows = conn.execute(
        """SELECT sender_address, max(sender_name) AS name, count(*) AS total,
                  sum(1 - is_read) AS unread, min(received_at) AS first, max(received_at) AS last,
                  sum(json_array_length(signals, '$.bulk') > 0) AS with_bulk,
                  sum(json_array_length(signals, '$.risk') > 0) AS with_risk
           FROM messages WHERE account_id = ? AND folder = ?
           GROUP BY sender_address ORDER BY total DESC, sender_address LIMIT ?""",
        (account_id, folder, limit)).fetchall()
    groups = []
    for row in rows:
        subjects = [r[0] for r in conn.execute(
            """SELECT subject FROM messages WHERE account_id = ? AND folder = ? AND sender_address = ?
               GROUP BY subject ORDER BY max(received_at) DESC LIMIT ?""",
            (account_id, folder, row["sender_address"], samples))]
        groups.append({
            "sender": row["sender_address"], "name": row["name"], "total": row["total"],
            "unread": row["unread"], "first": row["first"], "last": row["last"],
            "bulk_ratio": round(row["with_bulk"] / row["total"], 2),
            "risk": row["with_risk"],
            "protected": protection_reason(conn, account_id, row["sender_address"]),
            "sample_subjects": subjects,
        })
    return groups


def stats(conn: sqlite3.Connection, account_id: str) -> dict:
    folders = dict(conn.execute(
        "SELECT folder, count(*) FROM messages WHERE account_id = ? GROUP BY folder", (account_id,)).fetchall())
    senders = conn.execute("SELECT count(DISTINCT sender_address) FROM messages WHERE account_id = ? AND folder = 'inbox'",
                           (account_id,)).fetchone()[0]
    top = conn.execute(
        """SELECT coalesce(sum(total), 0) FROM (SELECT count(*) AS total FROM messages
           WHERE account_id = ? AND folder = 'inbox' GROUP BY sender_address ORDER BY total DESC LIMIT 200)""",
        (account_id,)).fetchone()[0]
    return {"folders": folders, "inbox_senders": senders, "inbox_in_top_200_senders": top}
