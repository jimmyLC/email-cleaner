"""Decide what should happen to each indexed Inbox message, without touching the mailbox.

Order of precedence for one message:
1. Protected client mail is filed under 客戶 (after it is 30 days old) and is never trashed.
   The owner's decision on that exact message in the review queue comes next.
2. The owner's active rules, most specific first. A trash rule never overrides a deletion guard.
3. Built-in policies from local signals: e-invoices go to 發票 and statements to 銀行; account and
   security notices older than 30 days go to the trash.
4. Jev's classification, when present (see jev.disposition); unsure answers become "review".
5. Anything else is left unclassified until Jev has seen it.

Mail younger than 30 days stays in the Inbox unless a rule trashes it, so the Inbox
ends up holding only recent, unprocessed mail and items waiting for review.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

from email_cleaner import index, jev, signals
from email_cleaner.rules import Rule, active_rules, first_match

CLIENT_FOLDER = "客戶"
PERSONAL_FOLDER = "個人"
INVOICE_FOLDER = "發票"
BANK_FOLDER = "銀行"
MANAGED_FOLDERS = (CLIENT_FOLDER, PERSONAL_FOLDER, INVOICE_FOLDER, BANK_FOLDER)
# Subjects that are the document itself: a real e-invoice, or a statement (not a notification).
INVOICE_SUBJECT = re.compile(r"電子發票")
STATEMENT_SUBJECT = re.compile(r"對帳單|電子帳單|e-?statement", re.IGNORECASE)
RECENT_DAYS = 30
RETIRED_FOLDERS = ("個人", "收據")  # folders the owner no longer wants; their mail goes to the archive
FALLBACK_FOLDER = "封存"  # Outlook's own Archive: where mail goes when nothing more specific applies
SECURITY_LABELS = {"驗證碼", "密碼／帳號存取", "安全通知"}
RECEIPT_LABELS = {"收據／發票", "對帳單", "訂單／付款", "預訂／行程"}


@dataclass(frozen=True)
class PlanItem:
    message_id: str
    sender: str
    subject: str
    received_at: str | None
    action: str  # archive | trash | keep | review
    target_folder: str | None
    reason: str
    rule_id: int | None = None
    blocked: bool = False  # a rule wanted to trash it but a guard refused


def _age_days(received_at: str | None, now: datetime) -> float:
    if not received_at:
        return 0.0
    try:
        received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (now - received).total_seconds() / 86400


def decide(row: sqlite3.Row, rules: list[Rule], protection: str | None, guard: str | None,
           now: datetime, recent_days: int = RECENT_DAYS) -> PlanItem:
    sender, subject = row["sender_address"], row["subject"]
    recent = _age_days(row["received_at"], now) < recent_days
    item = lambda action, folder, reason, rule_id=None, blocked=False: PlanItem(
        row["message_id"], sender, subject, row["received_at"], action, folder, reason, rule_id, blocked)

    if protection:
        if recent:
            return item("keep", None, f"客戶（{protection}），30 天內留在收件匣")
        return item("archive", CLIENT_FOLDER, f"客戶（{protection}）")

    decision = row["review_decision"] if "review_decision" in row.keys() else None
    if decision:
        # The owner already decided this exact message in the review queue.
        if decision == "keep":
            return item("keep", None, "你決定保留")
        if decision == "trash":
            if guard:
                return item("keep", None, f"你決定刪除，但{guard}", blocked=True)
            return item("trash", None, "你決定刪除")
        if decision.startswith("archive:"):
            return item("archive", decision.partition(":")[2], "你決定歸檔")

    rule = first_match(rules, sender, subject)
    if rule:
        if rule.action == "trash":
            if guard:
                return item("keep", None, f"規則 #{rule.id} 要刪除，但{guard}", rule.id, blocked=True)
            return item("trash", None, f"規則 #{rule.id}", rule.id)
        if rule.action == "archive":
            if recent:
                return item("keep", None, f"規則 #{rule.id}，30 天內留在收件匣", rule.id)
            return item("archive", rule.target_folder, f"規則 #{rule.id}", rule.id)
        # report is handled in the unsubscribe/report phase
        return item("keep", None, f"規則 #{rule.id} 檢舉（尚未支援）", rule.id)

    labels = set(json.loads(row["signals"] or "{}").get("protect", []))
    document_folder = (INVOICE_FOLDER if INVOICE_SUBJECT.search(subject)
                       else BANK_FOLDER if STATEMENT_SUBJECT.search(subject) else None)
    if document_folder:
        if recent:
            return item("keep", None, f"{document_folder}，30 天內留在收件匣")
        return item("archive", document_folder, f"{document_folder}（發票／對帳單）")
    if labels & RECEIPT_LABELS:
        return item("keep", None, "訂單／收據類，尚未決定資料夾")
    if labels & SECURITY_LABELS and not recent:
        if guard:
            return item("keep", None, f"超過 30 天的帳號／安全通知，但{guard}", blocked=True)
        return item("trash", None, "超過 30 天的帳號／安全通知")
    if "category" in row.keys() and row["category"]:
        result = jev.Classification(row["category"], row["category_confidence"],
                                    row["message_type"], row["message_type_confidence"])
        # The subdomain check also covers rows indexed before that signal existed.
        bulk_evidence = bool(json.loads(row["signals"] or "{}").get("bulk")) or signals.bulk_subdomain(sender)
        action, folder, reason = jev.disposition(result, bulk_evidence)
        if action == "trash_if_old":
            action = "keep" if recent else "trash"
            reason = f"{reason}，30 天內留在收件匣" if recent else reason
        if action == "trash" and guard:
            return item("keep", None, f"{reason}，但{guard}", blocked=True)
        if action == "archive" and recent:
            return item("keep", None, f"{reason}，30 天內留在收件匣")
        return item(action, folder, reason)
    return item("keep", None, "尚未分類（待 Jev／確認）")


def build_plan(conn: sqlite3.Connection, account_id: str, senders: list[str] | None = None,
               now: datetime | None = None, finish: bool = False, keep_newest: int = 0) -> list[PlanItem]:
    """Plan for Inbox messages, optionally limited to some senders. Read-only.

    `finish` files everything no rule decided into Outlook's Archive and drops the
    30-day wait; `keep_newest` leaves that many of the newest messages in the Inbox.
    """
    now = now or datetime.now(timezone.utc)
    rules = active_rules(conn)
    # A sender's own Jev verdict (from sampled messages) covers its messages Jev has not seen.
    conn.execute("""CREATE TEMP TABLE IF NOT EXISTS sender_class (sender_address TEXT PRIMARY KEY, category TEXT,
                    category_confidence REAL, message_type TEXT, message_type_confidence REAL)""")
    conn.execute("DELETE FROM sender_class")
    conn.executemany("INSERT INTO sender_class VALUES (?, ?, ?, ?, ?)",
                     [(sender, c.category, c.category_confidence, c.message_type, c.message_type_confidence)
                      for sender, c in jev.sender_classifications(conn, account_id).items()])
    query = """SELECT m.*, COALESCE(c.category, sc.category) AS category,
                      COALESCE(c.category_confidence, sc.category_confidence) AS category_confidence,
                      COALESCE(c.message_type, sc.message_type) AS message_type,
                      COALESCE(c.message_type_confidence, sc.message_type_confidence) AS message_type_confidence,
                      (SELECT q.resolution FROM review_queue q
                       WHERE q.account_id = m.account_id AND q.message_id = m.message_id
                         AND (q.resolution IN ('keep', 'trash') OR q.resolution LIKE 'archive:%')
                       ORDER BY q.resolved_at DESC, q.id DESC LIMIT 1) AS review_decision
               FROM messages m LEFT JOIN classifications c
                 ON c.account_id = m.account_id AND c.message_id = m.message_id
               LEFT JOIN sender_class sc ON sc.sender_address = m.sender_address
               WHERE m.account_id = ? AND m.folder = 'inbox'"""
    params: list = [account_id]
    if senders is not None:
        query += f" AND m.sender_address IN ({','.join('?' * len(senders))})"
        params += senders
    cache: dict[str, tuple[str | None, str | None]] = {}
    plan = []
    recent_days = 0 if finish else RECENT_DAYS
    for row in conn.execute(query + " ORDER BY m.received_at", params):
        sender = row["sender_address"]
        if sender not in cache:
            cache[sender] = (index.protection_reason(conn, account_id, sender),
                             index.deletion_guard(conn, account_id, sender))
        plan.append(decide(row, rules, *cache[sender], now, recent_days))
    if finish or keep_newest:
        newest = {row[0] for row in conn.execute(
            "SELECT message_id FROM messages WHERE account_id = ? AND folder = 'inbox' "
            "ORDER BY received_at DESC LIMIT ?", (account_id, keep_newest))} if keep_newest else set()
        for position, item in enumerate(plan):
            if item.message_id in newest:
                plan[position] = replace(item, action="keep", target_folder=None, rule_id=None,
                                         reason=f"最新 {keep_newest} 封，留在收件匣")
            elif finish and item.action == "archive" and item.target_folder in RETIRED_FOLDERS:
                plan[position] = replace(item, target_folder=FALLBACK_FOLDER,
                                         reason=f"{item.reason}（{item.target_folder}已停用，改放封存）")
            elif finish and item.action in ("keep", "review") and item.reason != "你決定保留":
                plan[position] = replace(item, action="archive", target_folder=FALLBACK_FOLDER,
                                         reason=f"分類完成：保留到封存（{item.reason}）")
    return plan


def top_senders(conn: sqlite3.Connection, account_id: str, groups: int) -> list[str]:
    return [row[0] for row in conn.execute(
        """SELECT sender_address FROM messages WHERE account_id = ? AND folder = 'inbox'
           GROUP BY sender_address ORDER BY count(*) DESC, sender_address LIMIT ?""", (account_id, groups))]


def summarize(plan: list[PlanItem]) -> dict:
    actions = Counter(item.action for item in plan)
    # Group reasons by their stable prefix so per-rule and per-confidence variants collapse.
    reasons = Counter(re.sub(r"( #\d+| \d+%).*$", "", item.reason).split("（")[0].split("，")[0] for item in plan)
    folders = Counter(item.target_folder for item in plan if item.action == "archive")
    return {"total": len(plan), "actions": dict(actions), "archive_folders": dict(folders),
            "blocked": sum(item.blocked for item in plan), "reasons": dict(reasons.most_common(12))}


def to_json(plan: list[PlanItem]) -> list[dict]:
    return [asdict(item) for item in plan]

