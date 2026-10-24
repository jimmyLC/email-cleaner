"""Messages Jev was unsure about. The owner decides; each decision can become a rule.

Only the owner's CLI or Web UI resolves reviews. MCP may list them but not decide.
"""

from __future__ import annotations

import sqlite3

from email_cleaner import rules
from email_cleaner.policy import PlanItem


def enqueue(conn: sqlite3.Connection, account_id: str, items: list[PlanItem]) -> int:
    added = 0
    for item in items:
        if item.action != "review":
            continue
        exists = conn.execute(
            "SELECT 1 FROM review_queue WHERE account_id = ? AND message_id = ? AND resolved_at IS NULL",
            (account_id, item.message_id)).fetchone()
        if not exists:
            conn.execute("INSERT INTO review_queue (account_id, message_id, reason) VALUES (?, ?, ?)",
                         (account_id, item.message_id, item.reason))
            added += 1
    conn.commit()
    return added


def prune(conn: sqlite3.Connection, account_id: str, items: list[PlanItem]) -> int:
    """Close open reviews the current plan no longer needs (a new rule or signal decided them)."""
    still_review = {item.message_id for item in items if item.action == "review"}
    planned = {item.message_id for item in items}
    stale = [row[0] for row in conn.execute(
        "SELECT id, message_id FROM review_queue WHERE account_id = ? AND resolved_at IS NULL", (account_id,))
        if row[1] in planned and row[1] not in still_review]
    conn.executemany("UPDATE review_queue SET resolved_at = datetime('now'), resolution = 'superseded' WHERE id = ?",
                     [(review_id,) for review_id in stale])
    conn.commit()
    return len(stale)


def pending(conn: sqlite3.Connection, account_id: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT q.id, q.reason, m.sender_address, m.sender_name, m.subject, m.received_at
           FROM review_queue q JOIN messages m ON m.account_id = q.account_id AND m.message_id = q.message_id
           WHERE q.account_id = ? AND q.resolved_at IS NULL ORDER BY m.sender_address, m.received_at LIMIT ?""",
        (account_id, limit)).fetchall()
    return [dict(row) for row in rows]


def resolve(conn: sqlite3.Connection, account_id: str, review_id: int, action: str,
            folder: str | None = None, rule_scope: str | None = None) -> int | None:
    """Record the owner's decision; with `rule_scope`, also create an active rule. Returns the rule ID."""
    if action not in ("trash", "archive", "keep"):
        raise ValueError("action must be trash, archive or keep")
    if action == "archive" and not folder:
        raise ValueError("archive needs a folder")
    row = conn.execute(
        """SELECT m.sender_address FROM review_queue q JOIN messages m
             ON m.account_id = q.account_id AND m.message_id = q.message_id
           WHERE q.id = ? AND q.account_id = ? AND q.resolved_at IS NULL""", (review_id, account_id)).fetchone()
    if row is None:
        raise ValueError(f"No open review {review_id}")
    rule_id = None
    if rule_scope and action != "keep":
        sender = row["sender_address"] if rule_scope == "address" else row["sender_address"].rpartition("@")[2]
        rule_id = rules.add_rule(conn, rule_scope, sender, action, "review",
                                 target_folder=folder if action == "archive" else None,
                                 note=f"from review #{review_id}")
    conn.execute("UPDATE review_queue SET resolved_at = datetime('now'), resolution = ? WHERE id = ?",
                 (action if action != "archive" else f"archive:{folder}", review_id))
    conn.commit()
    return rule_id
