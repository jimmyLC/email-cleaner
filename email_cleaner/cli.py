"""Command line entry point. Phase 1 commands only read from the mailbox."""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

from email_cleaner import db, index
from email_cleaner.config import PROJECT, db_path


def outlook_provider():
    from email_cleaner.providers.outlook import OutlookAuth, OutlookProvider
    return OutlookProvider(OutlookAuth())


def account_for(conn, provider_name: str) -> str:
    row = conn.execute("SELECT id FROM accounts WHERE provider = ? ORDER BY created_at LIMIT 1",
                       (provider_name,)).fetchone()
    if not row:
        raise RuntimeError(f"No {provider_name} account indexed yet; run `sync {provider_name}` first.")
    return row[0]


def cmd_login(args) -> int:
    from email_cleaner.providers.outlook import OutlookAuth
    OutlookAuth().device_login()
    print("Outlook sign-in complete (read-only scopes).")
    return 0


def cmd_sync(args) -> int:
    with db.connect(db_path()) as conn:
        provider = outlook_provider()
        result = index.sync(conn, provider, full=args.full)
        flagged = index.flag_protection_reviews(conn, provider.account_id())
    print(f"Indexed inbox {result.inbox}, sent {result.sent}; "
          f"{result.correspondents} sent-to addresses, {result.contacts} contact addresses. No mail was changed.")
    if flagged:
        print(f"{flagged} protected sender(s) look like newsletters; see `protection-review outlook`.")
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
    parser = argparse.ArgumentParser(prog="email-cleaner", description="Personal inbox cleaner (read-only phase)")
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="Sign in to a mailbox in this terminal")
    login.add_argument("provider", choices=["outlook"])
    login.set_defaults(func=cmd_login)

    sync = commands.add_parser("sync", help="Index Inbox and Sent Items metadata (read-only)")
    sync.add_argument("provider", choices=["outlook"])
    sync.add_argument("--full", action="store_true", help="re-read everything instead of only newer mail")
    sync.set_defaults(func=cmd_sync)

    for name, func, text in (("stats", cmd_stats, "Show index totals"),
                             ("groups", cmd_groups, "List the largest sender groups")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("provider", choices=["outlook"])
        sub.set_defaults(func=func)
        if name == "groups":
            sub.add_argument("--limit", type=int, default=50)
            sub.add_argument("--json", action="store_true")

    review = commands.add_parser("protection-review",
                                 help="List or decide protected senders that look like newsletters")
    review.add_argument("provider", choices=["outlook"])
    review.add_argument("decision", nargs="?", default="list", choices=["list", "keep", "unprotect", "not-client"])
    review.add_argument("address", nargs="?", default="")
    review.add_argument("--json", action="store_true")
    review.set_defaults(func=cmd_protection_review)

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
