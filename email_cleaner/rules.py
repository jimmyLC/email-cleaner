"""Readable cleanup rules: sender address, sender domain, or domain + subject keyword."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

ACTIONS = ("archive", "trash", "report")
# More specific rules win when several match one message.
SPECIFICITY = {"domain_subject": 3, "address": 2, "domain": 1}


@dataclass(frozen=True)
class Rule:
    id: int
    scope: str
    sender: str
    subject_keyword: str | None
    action: str
    target_folder: str | None

    def matches(self, sender_address: str, subject: str) -> bool:
        address = sender_address.casefold()
        domain = address.rpartition("@")[2]
        if self.scope == "address":
            return address == self.sender
        if self.scope == "domain":
            return domain == self.sender
        return domain == self.sender and (self.subject_keyword or "").casefold() in subject.casefold()


def add_rule(conn: sqlite3.Connection, scope: str, sender: str, action: str, origin: str,
             subject_keyword: str | None = None, target_folder: str | None = None,
             status: str = "active", note: str | None = None) -> int:
    sender = sender.strip().casefold().lstrip("@")
    if scope == "address" and "@" not in sender:
        raise ValueError("address rules need a full sender address")
    if scope in ("domain", "domain_subject") and ("@" in sender or "." not in sender):
        raise ValueError("domain rules need a domain such as shop.example")
    if scope == "domain_subject" and not (subject_keyword or "").strip():
        raise ValueError("domain + subject rules need a keyword")
    cursor = conn.execute(
        """INSERT INTO rules (scope, sender, subject_keyword, action, target_folder, status, origin, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (scope, sender, (subject_keyword or "").strip() or None, action, target_folder, status, origin, note))
    conn.commit()
    return cursor.lastrowid


def active_rules(conn: sqlite3.Connection) -> list[Rule]:
    rows = conn.execute("""SELECT id, scope, sender, subject_keyword, action, target_folder
                           FROM rules WHERE status = 'active'""").fetchall()
    rules = [Rule(*row) for row in rows]
    return sorted(rules, key=lambda rule: (-SPECIFICITY[rule.scope], rule.id))


def first_match(rules: list[Rule], sender_address: str, subject: str) -> Rule | None:
    return next((rule for rule in rules if rule.matches(sender_address, subject)), None)


def set_status(conn: sqlite3.Connection, rule_id: int, status: str) -> None:
    if conn.execute("UPDATE rules SET status = ? WHERE id = ?", (status, rule_id)).rowcount != 1:
        raise ValueError(f"No rule {rule_id}")
    conn.commit()
