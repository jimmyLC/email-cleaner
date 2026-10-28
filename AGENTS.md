# AGENTS.md

Guide for AI coding agents working in this repository. Read this first, then `PROGRESS.md`, then `docs/DESIGN.md` and `docs/PLAN.md`. If the docs and the code disagree, trust the code and fix the docs.

## What this is

A personal, self-hosted inbox cleaner for Outlook and Gmail (Gmail is built but not yet used on a real mailbox). It indexes message metadata, applies readable rules, classifies the rest with TypeSafe Jev, protects client correspondence, and offers unsubscribe without sending mail. It runs on the owner's own machine and is reached only over a private network (Tailscale). **This repo is public**, so nothing private may ever be committed.

Owner: Jimmy. Talk to him in Traditional Chinese.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -t .   # tests
python3 scripts/privacy_scan.py                        # run before EVERY push
.venv/bin/email-cleaner --help                         # login, sync, stats, groups, rule, plan, apply, undo, classify, review, mcp ...
```

## Hard rules

- **Public repo: run `python3 scripts/privacy_scan.py` before every push.** Credentials, tokens, rules, the mail index, action logs and `.env` stay on the host and are never committed. Test fixtures must not contain real subjects, addresses or names.
- **No message bodies are ever stored.** The index holds sender, subject, dates, flags, categories and computed signals only.
- **Never trash protected client mail** (anyone Jimmy has sent mail to, contacts, client domains). Senders reviewed as `not_client` are not filed under 客戶 but are still never trashed. The executor must call `deletion_guard()` before any trash and re-read the message before moving it.
- **No permanent delete, no sending mail.** There is no `Mail.Send` scope, Gmail uses `gmail.modify` (never `https://mail.google.com/`), and there is no mailto unsubscribe. Only archive and trash moves, all logged with undo.
- **Before any push, check the commit emails:** `git log --format='%ae %ce' <range> | sort -u` must show only the GitHub noreply address, never the owner's real Gmail.
- **Checkpoints need Jimmy's explicit OK** (see `docs/PLAN.md`): pushing or creating GitHub resources, force-pushes, live pilots, phishing tests, full sweeps, the first live Gmail run.
- **Run a full sync before planning or moving**; Jimmy moves mail by hand between runs.
- **Pace writes.** A large unpaced run (about 3.5 moves per second) triggered an Outlook web `OverBudgetException`. Batch with delays for any big run.
- Do not trust Jev's phishing labels (mostly wrong in the 500-message trial). Jev confidence is often 0.4 to 0.85, so reviews are normal; owner decisions become rules.
- Only the owner makes review and protection decisions. MCP tools must not expose them.
- Entra app permissions: phase 1 is read-only (`Mail.Read`, `Contacts.Read`, `User.Read`, GET only). Write scope (`login outlook --write`) is consented separately.

## Before you finish

1. Run the tests and the privacy scan.
2. Update `PROGRESS.md`.
3. Say plainly what was and was not verified, and whether anything was moved in the real mailbox.
