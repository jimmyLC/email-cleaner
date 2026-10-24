"""MCP server so an AI assistant can review and tidy the mailbox.

Run on the host with `email-cleaner mcp` (stdio). Every mailbox change goes
through the executor, so the hard rules hold for AI callers too: no trashing
protected or "never delete" senders, no permanent delete, no sending. Rules the
AI creates are logged as actions. Review decisions and protection decisions stay
with the owner and are deliberately not exposed here.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from email_cleaner import db, index, policy, review, rules
from email_cleaner.config import db_path

server = FastMCP("email-cleaner")


def _conn():
    return db.connect(db_path())


def _account(conn) -> str:
    row = conn.execute("SELECT id FROM accounts WHERE provider = 'outlook' ORDER BY created_at LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("No mailbox indexed yet; the owner must run `email-cleaner sync outlook`.")
    return row[0]


@server.tool()
def stats() -> dict:
    """Index totals: messages per folder, distinct senders, and the planned actions."""
    with _conn() as conn:
        account = _account(conn)
        return {"index": index.stats(conn, account), "plan": policy.summarize(policy.build_plan(conn, account))}


@server.tool()
def list_sender_groups(limit: int = 50, only_uncovered: bool = True) -> list[dict]:
    """Largest Inbox sender groups with sample subjects. By default skips senders an active rule already covers.

    Use this to draft rules. Fields: sender, total, unread, bulk_ratio (share with newsletter evidence),
    risk, protected (client reason or null), never_delete (guard reason or null), sample_subjects.
    """
    with _conn() as conn:
        account = _account(conn)
        active = rules.active_rules(conn)
        groups = []
        for group in index.sender_groups(conn, account, limit=limit * 3 if only_uncovered else limit):
            if only_uncovered and any(rule.matches(group["sender"], s) for rule in active
                                      for s in group["sample_subjects"] or [""]):
                continue
            group["never_delete"] = index.deletion_guard(conn, account, group["sender"])
            groups.append(group)
            if len(groups) >= limit:
                break
        return groups


@server.tool()
def list_rules(status: str = "") -> list[dict]:
    """Rules with their status (draft, active, disabled). Pass status to filter."""
    with _conn() as conn:
        query, params = "SELECT * FROM rules", ()
        if status:
            query, params = query + " WHERE status = ?", (status,)
        return [dict(row) for row in conn.execute(query + " ORDER BY id", params)]


def _check_rule(conn, account: str, scope: str, sender: str, action: str) -> None:
    if action not in ("archive", "trash"):
        raise ValueError("action must be archive or trash")
    if action == "trash" and scope == "address" and index.deletion_guard(conn, account, sender):
        raise ValueError("That sender is protected or marked never-delete; propose archive instead.")


@server.tool()
def propose_rule(scope: str, sender: str, action: str, note: str,
                 subject_keyword: str = "", target_folder: str = "") -> dict:
    """Draft a rule for the owner to approve. scope: address | domain | domain_subject. action: archive | trash.

    Archive needs target_folder (客戶, 個人, 收據 or another name). Explain the reason in note.
    Drafts never act until approved.
    """
    with _conn() as conn:
        account = _account(conn)
        _check_rule(conn, account, scope, sender, action)
        rule_id = rules.propose(conn, scope, sender, action, note, subject_keyword or None, target_folder or None)
        return {"draft_id": rule_id, "status": "draft"}


@server.tool()
def create_rule(scope: str, sender: str, action: str, note: str,
                subject_keyword: str = "", target_folder: str = "") -> dict:
    """Create an active rule immediately. It is logged so the owner can review it later. Prefer propose_rule."""
    with _conn() as conn:
        account = _account(conn)
        _check_rule(conn, account, scope, sender, action)
        rule_id = rules.add_rule(conn, scope, sender, action, "claude", subject_keyword or None,
                                 target_folder or None, status="active", note=note)
        conn.execute("""INSERT INTO actions (batch_id, account_id, action, rule_id, actor, detail)
                        VALUES (?, ?, 'create_rule', ?, 'mcp', ?)""", (f"rule-{rule_id}", account, rule_id, note))
        conn.commit()
        return {"rule_id": rule_id, "status": "active"}


@server.tool()
def preview_plan(groups: int = 0) -> dict:
    """What applying the rules would do now, without changing anything. groups limits to the N largest senders."""
    with _conn() as conn:
        account = _account(conn)
        senders = policy.top_senders(conn, account, groups) if groups else None
        return policy.summarize(policy.build_plan(conn, account, senders))


@server.tool()
def list_review_queue(limit: int = 50) -> list[dict]:
    """Messages waiting for the owner's decision (Jev was unsure). Read-only for AI callers."""
    with _conn() as conn:
        return review.pending(conn, _account(conn), limit)


@server.tool()
def get_action_log(limit: int = 50) -> list[dict]:
    """Most recent mailbox actions, newest first, with what is needed to undo each one."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


@server.tool()
def apply_plan(groups: int = 0) -> dict:
    """Carry out the current plan (archive or Deleted Items only), all guards applied. Returns a batch to undo."""
    from email_cleaner.executor import Executor
    from email_cleaner.providers.outlook import WRITE_SCOPES, OutlookAuth, OutlookWriter
    with _conn() as conn:
        account = _account(conn)
        senders = policy.top_senders(conn, account, groups) if groups else None
        plan = policy.build_plan(conn, account, senders)
        review.enqueue(conn, account, plan)
        result = Executor(conn, OutlookWriter(OutlookAuth(scopes=WRITE_SCOPES)), account, "mcp").execute(plan)
        return json.loads(json.dumps(result.__dict__))


@server.tool()
def undo_batch(batch_id: str) -> dict:
    """Move every message of a batch back to the Inbox."""
    from email_cleaner.executor import Executor
    from email_cleaner.providers.outlook import WRITE_SCOPES, OutlookAuth, OutlookWriter
    with _conn() as conn:
        result = Executor(conn, OutlookWriter(OutlookAuth(scopes=WRITE_SCOPES)), _account(conn), "mcp").undo(batch_id)
        return json.loads(json.dumps(result.__dict__))


def main() -> None:
    server.run()
