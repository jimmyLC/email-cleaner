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


def propose(conn: sqlite3.Connection, scope: str, sender: str, action: str, note: str,
            subject_keyword: str | None = None, target_folder: str | None = None, origin: str = "claude") -> int:
    """Store a rule draft. Drafts never act until the owner approves them."""
    return add_rule(conn, scope, sender, action, origin, subject_keyword=subject_keyword,
                    target_folder=target_folder, status="draft", note=note)


def drafts(conn: sqlite3.Connection, account_id: str) -> list[dict]:
    """Pending drafts with how many Inbox messages each would affect."""
    result = []
    for row in conn.execute("SELECT * FROM rules WHERE status = 'draft' ORDER BY id"):
        rule = Rule(row["id"], row["scope"], row["sender"], row["subject_keyword"], row["action"], row["target_folder"])
        like = f"%@{rule.sender}" if rule.scope != "address" else rule.sender
        candidates = conn.execute(
            "SELECT subject, sender_address FROM messages WHERE account_id = ? AND folder = 'inbox' AND sender_address LIKE ?",
            (account_id, like)).fetchall()
        matched = [c["subject"] for c in candidates if rule.matches(c["sender_address"], c["subject"])]
        result.append({"id": rule.id, "scope": rule.scope, "sender": rule.sender, "keyword": rule.subject_keyword,
                       "action": rule.action, "folder": rule.target_folder, "note": row["note"],
                       "origin": row["origin"], "matches": len(matched), "sample_subjects": matched[:3]})
    return result


def decide_draft(conn: sqlite3.Connection, rule_id: int, approve: bool) -> None:
    changed = conn.execute("UPDATE rules SET status = ? WHERE id = ? AND status = 'draft'",
                           ("active" if approve else "disabled", rule_id)).rowcount
    if changed != 1:
        raise ValueError(f"No draft rule {rule_id}")
    conn.commit()
