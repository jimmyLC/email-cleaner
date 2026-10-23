"""The only place that changes a mailbox. Every caller (CLI, Web, schedule, MCP) goes through here.

Hard rules enforced here, regardless of what a plan says:
- Only "archive" (move to a named folder) and "trash" (move to Deleted Items) exist.
  There is no permanent delete and no sending; the writer has no such calls.
- Trash is refused whenever `index.deletion_guard` names a reason (clients, and
  senders the owner marked "never delete"). The guard is re-checked at run time.
- Before each move the message is re-read: it must still be in the Inbox with the
  same Internet Message ID and sender as in the index.
- Every move is logged with the new message ID so the batch can be undone.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from email_cleaner import index
from email_cleaner.policy import PlanItem

MAX_CONSECUTIVE_FAILURES = 5


class Writer(Protocol):
    def account_id(self) -> str: ...
    def get_message(self, message_id: str) -> dict: ...
    def folder_id(self, well_known: str) -> str: ...
    def ensure_folder(self, display_name: str) -> str: ...
    def move(self, message_id: str, destination_id: str) -> str: ...


@dataclass
class Result:
    batch_id: str
    moved: int = 0
    skipped: int = 0
    blocked: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


def _sender(raw: dict) -> str:
    return (((raw.get("from") or {}).get("emailAddress") or {}).get("address") or "").casefold()


class Executor:
    def __init__(self, conn: sqlite3.Connection, writer: Writer, account_id: str, actor: str) -> None:
        if actor not in ("user", "schedule", "mcp"):
            raise ValueError("unknown actor")
        self.conn, self.writer, self.account_id, self.actor = conn, writer, account_id, actor

    def _check_account(self) -> None:
        if self.writer.account_id() != self.account_id:
            raise RuntimeError("Signed-in mailbox does not match the indexed account; nothing was changed.")

    def execute(self, items: Iterable[PlanItem]) -> Result:
        self._check_account()
        result = Result(batch_id="b-" + secrets.token_hex(6))
        inbox_id = self.writer.folder_id("inbox")
        trash_id = self.writer.folder_id("deleteditems")
        failures = 0
        for item in items:
            if item.action not in ("archive", "trash"):
                result.skipped += 1
                continue
            row = self.conn.execute(
                "SELECT * FROM messages WHERE account_id = ? AND message_id = ? AND folder = 'inbox'",
                (self.account_id, item.message_id)).fetchone()
            if row is None:
                result.skipped += 1
                continue
            if item.action == "trash":
                guard = index.deletion_guard(self.conn, self.account_id, row["sender_address"])
                if guard:
                    result.blocked += 1
                    result.notes.append(f"blocked: {row['sender_address']} ({guard})")
                    continue
            try:
                destination = trash_id if item.action == "trash" else self.writer.ensure_folder(item.target_folder or "")
                current = self.writer.get_message(item.message_id)
                if (current.get("parentFolderId") != inbox_id
                        or (current.get("internetMessageId") or "") != (row["internet_message_id"] or "")
                        or _sender(current) != row["sender_address"]):
                    result.skipped += 1
                    continue
                action_id = self.conn.execute(
                    """INSERT INTO actions (batch_id, account_id, message_id, action, from_folder, to_folder,
                           rule_id, actor, detail) VALUES (?, ?, ?, ?, 'inbox', ?, ?, ?, 'pending')""",
                    (result.batch_id, self.account_id, item.message_id, item.action,
                     "deleteditems" if item.action == "trash" else item.target_folder,
                     item.rule_id, self.actor)).lastrowid
                self.conn.commit()
                new_id = self.writer.move(item.message_id, destination)
                label = "trash" if item.action == "trash" else f"archive:{item.target_folder}"
                self.conn.execute("UPDATE actions SET new_message_id = ?, detail = ? WHERE id = ?",
                                  (new_id, item.reason, action_id))
                self.conn.execute("UPDATE messages SET message_id = ?, folder = ? WHERE account_id = ? AND message_id = ?",
                                  (new_id, label, self.account_id, item.message_id))
                self.conn.commit()
                result.moved += 1
                failures = 0
            except Exception as exc:  # noqa: BLE001 - count and continue; stop on a run of failures
                self.conn.execute("UPDATE actions SET detail = 'failed' WHERE batch_id = ? AND message_id = ? "
                                  "AND detail = 'pending'", (result.batch_id, item.message_id))
                self.conn.commit()
                result.failed += 1
                failures += 1
                result.notes.append(f"failed: {type(exc).__name__}")
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    result.notes.append("stopped after repeated failures")
                    break
        return result

    def undo(self, batch_id: str) -> Result:
        """Move every message from a batch back to the Inbox."""
        self._check_account()
        result = Result(batch_id=batch_id)
        inbox_id = self.writer.folder_id("inbox")
        rows = self.conn.execute(
            """SELECT * FROM actions WHERE batch_id = ? AND account_id = ? AND undone_at IS NULL
               AND new_message_id IS NOT NULL AND action IN ('archive', 'trash')""",
            (batch_id, self.account_id)).fetchall()
        for action in rows:
            try:
                current = self.writer.get_message(action["new_message_id"])
                if current.get("parentFolderId") == inbox_id:
                    result.skipped += 1
                    continue
                restored_id = self.writer.move(action["new_message_id"], inbox_id)
                self.conn.execute("UPDATE actions SET undone_at = datetime('now') WHERE id = ?", (action["id"],))
                self.conn.execute(
                    """INSERT INTO actions (batch_id, account_id, message_id, action, from_folder, to_folder,
                           actor, detail, new_message_id) VALUES (?, ?, ?, 'restore', ?, 'inbox', ?, ?, ?)""",
                    (batch_id, self.account_id, action["new_message_id"], action["to_folder"], self.actor,
                     f"undo #{action['id']}", restored_id))
                self.conn.execute("UPDATE messages SET message_id = ?, folder = 'inbox' WHERE account_id = ? "
                                  "AND message_id = ?", (restored_id, self.account_id, action["new_message_id"]))
                self.conn.commit()
                result.moved += 1
            except Exception as exc:  # noqa: BLE001
                result.failed += 1
                result.notes.append(f"failed: {type(exc).__name__}")
        return result
