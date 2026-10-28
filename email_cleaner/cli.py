"""Command line entry point.

Only `apply` and `undo` change the mailbox, and both go through the executor's
guards. Everything else reads the mailbox or only the local index.
"""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

from email_cleaner import db, index, policy, review, rules
from email_cleaner.config import PROJECT, db_path
from email_cleaner.providers import registry


def any_account(conn) -> str:
    """The first indexed account, for commands that are not tied to one provider."""
    row = conn.execute("SELECT id FROM accounts ORDER BY provider = 'outlook' DESC, created_at LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("No mailbox indexed yet; run `sync` first.")
    return row[0]


def account_for(conn, provider_name: str) -> str:
    row = conn.execute("SELECT id FROM accounts WHERE provider = ? ORDER BY created_at LIMIT 1",
                       (provider_name,)).fetchone()
    if not row:
        raise RuntimeError(f"No {provider_name} account indexed yet; run `sync {provider_name}` first.")
    return row[0]


def cmd_login(args) -> int:
    print(registry.login(args.provider, args.write))
    return 0


def cmd_sync(args) -> int:
    with db.connect(db_path()) as conn:
        provider = registry.reader(args.provider)
        result = index.sync(conn, provider, full=args.full)
        flagged = index.flag_protection_reviews(conn, provider.account_id())
    print(f"Indexed inbox {result.inbox}, sent {result.sent}; "
          f"{result.correspondents} sent-to addresses, {result.contacts} contact addresses. No mail was changed.")
    if flagged:
        print(f"{flagged} protected sender(s) look like newsletters; see `protection-review {args.provider}`.")
    return 0


def cmd_stats(args) -> int:
    with db.connect(db_path()) as conn:
        print(json.dumps(index.stats(conn, account_for(conn, args.provider)), ensure_ascii=False, indent=2))
    return 0


def cmd_groups(args) -> int:
    with db.connect(db_path()) as conn:
        groups = index.sender_groups(conn, account_for(conn, args.provider), limit=args.limit)
    if args.json:
        print(json.dumps(groups, ensure_ascii=False, indent=2))
        return 0
    for number, group in enumerate(groups, 1):
        flag = f"保護（{group['protected']}）" if group["protected"] else f"群發佐證 {group['bulk_ratio']:.0%}"
        risk = f" · 風險 {group['risk']}" if group["risk"] else ""
        print(f"{number:>3}. {group['total']:>5} 封  {group['sender']}  {flag}{risk}")
        for subject in group["sample_subjects"]:
            print(f"        - {subject}")
    return 0


def cmd_protection_review(args) -> int:
    with db.connect(db_path()) as conn:
        account = account_for(conn, args.provider)
        if args.decision != "list":
            resolution = args.decision.replace("-", "_")
            index.resolve_protection_review(conn, account, args.address, resolution)
            labels = {"keep": "維持保護", "unprotect": "解除保護", "not_client": "不列為客戶，但永不刪除"}
            print(f"{args.address}: {labels[resolution]}")
            return 0
        pending = index.pending_protection_reviews(conn, account)
    if args.json:
        print(json.dumps(pending, ensure_ascii=False, indent=2))
        return 0
    for number, item in enumerate(pending, 1):
        print(f"{number:>3}. {item['address']}  {item['reason']}")
        for subject in item["sample_subjects"]:
            print(f"        - {subject}")
    print(f"{len(pending)} pending. Decide with: protection-review {args.provider} keep|unprotect|not-client ADDRESS")
    return 0


def cmd_rule(args) -> int:
    with db.connect(db_path()) as conn:
        if args.rule_command == "drafts":
            account = any_account(conn)
            for draft in rules.drafts(conn, account):
                keyword = f" 主旨含「{draft['keyword']}」" if draft["keyword"] else ""
                folder = f" → {draft['folder']}" if draft["folder"] else ""
                print(f"#{draft['id']:<4} {draft['sender']}{keyword}  {draft['action']}{folder}  "
                      f"影響 {draft['matches']} 封  理由：{draft['note']}")
                for subject in draft["sample_subjects"]:
                    print(f"        - {subject}")
            return 0
        if args.rule_command in ("approve", "reject"):
            for rule_id in args.ids:
                rules.decide_draft(conn, rule_id, approve=args.rule_command == "approve")
            print(f"{args.rule_command}d {len(args.ids)} draft(s)")
            return 0
        if args.rule_command == "add":
            rule_id = rules.add_rule(conn, args.scope, args.sender, args.action, "user",
                                     subject_keyword=args.keyword, target_folder=args.folder)
            print(f"Added rule #{rule_id}")
        elif args.rule_command in ("disable", "enable"):
            rules.set_status(conn, args.id, "disabled" if args.rule_command == "disable" else "active")
            print(f"Rule #{args.id} {args.rule_command}d")
        for row in conn.execute("SELECT * FROM rules ORDER BY id"):
            keyword = f" 主旨含「{row['subject_keyword']}」" if row["subject_keyword"] else ""
            folder = f" → {row['target_folder']}" if row["target_folder"] else ""
            print(f"#{row['id']:<4} {row['status']:<8} {row['scope']:<14} {row['sender']}{keyword}  "
                  f"{row['action']}{folder}  ({row['origin']})")
    return 0


def _plan(conn, args):
    account = account_for(conn, args.provider)
    senders = args.sender or (policy.top_senders(conn, account, args.groups) if args.groups else None)
    keep = args.keep_newest if args.finish else 0
    return account, policy.build_plan(conn, account, senders, finish=args.finish, keep_newest=keep)


def cmd_plan(args) -> int:
    with db.connect(db_path()) as conn:
        _, plan = _plan(conn, args)
    if args.json:
        print(json.dumps(policy.to_json(plan), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(policy.summarize(plan), ensure_ascii=False, indent=2))
        print("Preview only; no mail was changed.")
    return 0


def cmd_apply(args) -> int:
    from email_cleaner.executor import DEFAULT_PACE, Executor
    with db.connect(db_path()) as conn:
        account, plan = _plan(conn, args)
        summary = policy.summarize(plan)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not args.yes:
            print("Preview only. Re-run with --yes to move these messages (archive or Deleted Items only).")
            return 0
        result = Executor(conn, registry.writer(args.provider), account, actor="user",
                          pace=DEFAULT_PACE).execute(plan)
    print(f"Batch {result.batch_id}: moved {result.moved}, skipped {result.skipped}, "
          f"blocked {result.blocked}, failed {result.failed}. Undo with: undo {args.provider} {result.batch_id}")
    return 0 if result.failed == 0 else 1


def cmd_undo(args) -> int:
    from email_cleaner.executor import DEFAULT_PACE, Executor
    with db.connect(db_path()) as conn:
        account = account_for(conn, args.provider)
        result = Executor(conn, registry.writer(args.provider), account, actor="user",
                          pace=DEFAULT_PACE).undo(args.batch)
    print(f"Batch {args.batch}: restored {result.moved}, already in Inbox {result.skipped}, failed {result.failed}.")
    return 0 if result.failed == 0 else 1


def _spread_per_sender(conn, account: str, plan, per_sender: int) -> list[str]:
    """For each sender, up to `per_sender` unclassified messages spread from oldest to newest."""
    by_sender: dict[str, list[str]] = {}
    for item in plan:
        if item.reason == "尚未分類（待 Jev／確認）":
            by_sender.setdefault(item.sender, []).append(item.message_id)  # plan is oldest first
    done = dict(conn.execute(
        """SELECT m.sender_address, count(*) FROM classifications c JOIN messages m
             ON m.account_id = c.account_id AND m.message_id = c.message_id
           WHERE c.account_id = ? AND m.folder = 'inbox' GROUP BY m.sender_address""", (account,)).fetchall())
    picked = []
    for sender, ids in sorted(by_sender.items(), key=lambda kv: -len(kv[1])):
        need = min(per_sender - done.get(sender, 0), len(ids))
        if need <= 0:
            continue
        picked += [ids[round(i * (len(ids) - 1) / max(need - 1, 1))] for i in range(need)] if need > 1 else [ids[-1]]
    return list(dict.fromkeys(picked))


def cmd_classify(args) -> int:
    from typesafe_sdk import TypeSafeClient
    from email_cleaner import jev
    with db.connect(db_path()) as conn:
        account = account_for(conn, args.provider)
        senders = policy.top_senders(conn, account, args.groups) if args.groups else None
        plan = policy.build_plan(conn, account, senders)
        # Only messages no rule or built-in policy has decided are sent to Jev.
        todo = [item.message_id for item in plan if item.reason == "尚未分類（待 Jev／確認）"]
        if args.per_sender:
            todo = _spread_per_sender(conn, account, plan, args.per_sender)
        elif args.sample:
            import random
            # Spread a trial across the mailbox instead of taking only the oldest mail.
            todo = random.Random(args.seed).sample(todo, min(args.limit, len(todo)))
        todo = todo[:args.limit]
        print(f"Sending {len(todo)} messages to Jev (sender, subject, signals, redacted excerpt).")
        provider = registry.reader(args.provider)
        with TypeSafeClient() as client:
            counts = jev.classify_pending(conn, account, todo, client, provider.get_body_text)
        queued = review.enqueue(conn, account, policy.build_plan(conn, account, senders))
    print(f"Classified {counts['classified']}, skipped {counts['skipped']}; {queued} new review item(s). "
          "No mail was changed.")
    return 0


def cmd_review(args) -> int:
    with db.connect(db_path()) as conn:
        account = account_for(conn, args.provider)
        if args.review_command == "decide":
            rule_id = review.resolve(conn, account, args.id, args.action, args.folder, args.rule)
            print(f"Review #{args.id}: {args.action}" + (f"; created rule #{rule_id}" if rule_id else ""))
            return 0
        plan = policy.build_plan(conn, account)
        review.prune(conn, account, plan)
        review.enqueue(conn, account, plan)
        for item in review.pending(conn, account, args.limit):
            print(f"#{item['id']:<5} {item['sender_address']}  {item['subject'][:60]}  ({item['reason']})")
    return 0


def cmd_mcp(args) -> int:
    from email_cleaner.mcp_server import main as serve
    serve()
    return 0


def cmd_client_domain(args) -> int:
    with db.connect(db_path()) as conn:
        if args.action == "add":
            conn.execute("INSERT OR IGNORE INTO client_domains (domain) VALUES (?)", (args.domain.casefold(),))
        elif args.action == "remove":
            conn.execute("DELETE FROM client_domains WHERE domain = ?", (args.domain.casefold(),))
        conn.commit()
        for (domain,) in conn.execute("SELECT domain FROM client_domains ORDER BY domain"):
            print(domain)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT / ".env", override=False)
    parser = argparse.ArgumentParser(prog="email-cleaner", description="Personal inbox cleaner")
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="Sign in to a mailbox in this terminal")
    login.add_argument("provider", choices=list(registry.PROVIDERS))
    login.add_argument("--write", action="store_true", help="also grant permission to move mail")
    login.set_defaults(func=cmd_login)

    sync = commands.add_parser("sync", help="Index Inbox and Sent Items metadata (read-only)")
    sync.add_argument("provider", choices=list(registry.PROVIDERS))
    sync.add_argument("--full", action="store_true", help="re-read everything instead of only newer mail")
    sync.set_defaults(func=cmd_sync)

    for name, func, text in (("stats", cmd_stats, "Show index totals"),
                             ("groups", cmd_groups, "List the largest sender groups")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("provider", choices=list(registry.PROVIDERS))
        sub.set_defaults(func=func)
        if name == "groups":
            sub.add_argument("--limit", type=int, default=50)
            sub.add_argument("--json", action="store_true")

    review = commands.add_parser("protection-review",
                                 help="List or decide protected senders that look like newsletters")
    review.add_argument("provider", choices=list(registry.PROVIDERS))
    review.add_argument("decision", nargs="?", default="list", choices=["list", "keep", "unprotect", "not-client"])
    review.add_argument("address", nargs="?", default="")
    review.add_argument("--json", action="store_true")
    review.set_defaults(func=cmd_protection_review)

    rule = commands.add_parser("rule", help="List, add, approve or disable cleanup rules")
    rule_commands = rule.add_subparsers(dest="rule_command")
    rule_commands.add_parser("list")
    rule_commands.add_parser("drafts", help="Show drafts waiting for approval")
    for name in ("approve", "reject"):
        decide = rule_commands.add_parser(name)
        decide.add_argument("ids", type=int, nargs="+")
    add = rule_commands.add_parser("add")
    add.add_argument("--scope", required=True, choices=["address", "domain", "domain_subject"])
    add.add_argument("--sender", required=True, help="address, or domain for domain scopes")
    add.add_argument("--keyword", help="subject keyword for domain_subject")
    add.add_argument("--action", required=True, choices=["archive", "trash"])
    add.add_argument("--folder", help="target folder for archive")
    for name in ("disable", "enable"):
        toggle = rule_commands.add_parser(name)
        toggle.add_argument("id", type=int)
    rule.set_defaults(func=cmd_rule, rule_command="list")

    for name, func, text in (("plan", cmd_plan, "Preview what would happen (no changes)"),
                             ("apply", cmd_apply, "Carry out the plan; needs --yes"),):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("provider", choices=list(registry.PROVIDERS))
        sub.add_argument("--groups", type=int, default=0, help="limit to the N largest sender groups")
        sub.add_argument("--sender", action="append", help="limit to this sender (repeatable)")
        sub.add_argument("--finish", action="store_true",
                         help="file everything no rule decided into 封存 and skip the 30-day wait")
        sub.add_argument("--keep-newest", type=int, default=100,
                         help="with --finish, leave this many newest messages in the Inbox")
        sub.set_defaults(func=func)
        if name == "plan":
            sub.add_argument("--json", action="store_true")
        else:
            sub.add_argument("--yes", action="store_true")

    classify = commands.add_parser("classify", help="Send unclassified messages to Jev (no mail is changed)")
    classify.add_argument("provider", choices=list(registry.PROVIDERS))
    classify.add_argument("--limit", type=int, default=50)
    classify.add_argument("--groups", type=int, default=0)
    classify.add_argument("--sample", action="store_true", help="pick a random sample instead of the oldest mail")
    classify.add_argument("--seed", type=int, default=0)
    classify.add_argument("--per-sender", type=int, default=0,
                          help="classify up to N spread-out messages per sender; the verdict then covers the sender")
    classify.set_defaults(func=cmd_classify)

    reviews = commands.add_parser("review", help="List or decide messages Jev was unsure about")
    reviews.add_argument("provider", choices=list(registry.PROVIDERS))
    review_commands = reviews.add_subparsers(dest="review_command")
    review_list = review_commands.add_parser("list")
    review_list.add_argument("--limit", type=int, default=50)
    decide = review_commands.add_parser("decide")
    decide.add_argument("id", type=int)
    decide.add_argument("action", choices=["trash", "archive", "keep"])
    decide.add_argument("--folder")
    decide.add_argument("--rule", choices=["address", "domain"], help="also create a rule for this sender")
    reviews.set_defaults(func=cmd_review, review_command="list", limit=50)

    commands.add_parser("mcp", help="Run the MCP server on stdio").set_defaults(func=cmd_mcp)

    undo = commands.add_parser("undo", help="Move a batch's messages back to the Inbox")
    undo.add_argument("provider", choices=list(registry.PROVIDERS))
    undo.add_argument("batch")
    undo.set_defaults(func=cmd_undo)

    domains = commands.add_parser("client-domain", help="Manage the client domain list")
    domains.add_argument("action", choices=["add", "remove", "list"])
    domains.add_argument("domain", nargs="?", default="")
    domains.set_defaults(func=cmd_client_domain)

    args = parser.parse_args(argv)
    if args.command == "client-domain" and args.action != "list" and "." not in args.domain:
        parser.error("give a domain such as client.example")
    if args.command == "protection-review" and args.decision != "list" and "@" not in args.address:
        parser.error("give the sender address to decide")
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - show a short message, not a traceback with mail data
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
