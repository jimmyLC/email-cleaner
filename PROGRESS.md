# Progress

Last updated: 2026-10-28 (check `git log` for anything newer)

## Current state

- **Outlook is fully processed.** Rules, Jev classification, review queue, paced executor and a local MCP server (10 tools) are built. The owner's Inbox went from about 50,000 messages to the newest 100; everything else was filed or trashed in verified batches (0 failures, sampled folders checked against Outlook).
- **Folders (Outlook folders / Gmail labels):** 封存 (Outlook's own Archive, never create a new one), 電子報, 發票, 銀行, 投資, 客戶. 個人 and 收據 are retired and their mail goes to 封存. 語言教學 and 重要信 have no rules yet.
- **Policy order:** client (sent-to, contacts, client domains) → owner's one-off review decision → owner rules (most specific wins) → built-ins (e-invoice subject → 發票, statement subject → 銀行, old security notices → trash) → Jev verdict → leftovers. `plan --finish --keep-newest 100` files everything undecided into 封存.
- **Jev is used per sender:** up to 3 spread-out messages per sender are classified; a sender verdict applies only when at least 66% agree. Promotion → trash (needs bulk evidence and high confidence), newsletter → 電子報 (never trashed), everything else → 封存. About 6,000 classifications cost roughly 0.3 USD (an estimate; usage is not logged).
- **Gmail provider is built and tested offline (fake Gmail, 15 tests); it has not touched a real mailbox.** Labels instead of folders; the writer can only modify labels, trash and untrash (scope `gmail.modify`, no permanent delete, no send). Setup guide: `docs/GMAIL_SETUP.md`. Live use needs the owner to create a Google OAuth client (External audience, In production) and fill `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in `.env`.
- 89 offline tests pass; the privacy scan is clean.

## Known issues and lessons

- Moves must be paced (`DEFAULT_PACE` 0.8 s, rest every 300). An unpaced 15,000-message run broke Outlook on the web (`OverBudgetException`). A large run takes hours; keep the Mac awake.
- Graph assigns a new message ID on every move, and moving a message back to a folder it was in before returns the old ID. The index re-keys messages and their review rows in one transaction (`Executor._rekey`).
- The owner moves mail by hand between runs. **Run a full sync before planning or moving** (`sync outlook --full`, about 25 minutes for 50,000 messages); the executor also re-reads each message before moving it.
- Jev phishing labels are mostly wrong; never report or trash on them. The 客戶 folder holds everyone the owner ever wrote to, including service mailboxes and newsletters; review it with the owner.
- Python's `email.utils.getaddresses` returns nothing when given two empty values in a row; drop empty headers first (the Gmail provider does).
- The first 11 public commits carried the owner's real Gmail as author email. History was rewritten to the GitHub noreply address on 2026-10-28. Always check `git log --format='%ae %ce'` before a push.

## Next

- [ ] Owner creates the Google OAuth client; then Gmail read-only sync, preview, small pilot, full run (see `docs/GMAIL_SETUP.md`)
- [ ] 語言教學 and 重要信 rules; review the 電子報 folder for senders the owner does not actually read
- [ ] Safe reader and unsubscribe flow (RFC 8058 POST with safe-URL checks); `SPAM` label and block rules
- [ ] Mobile-first PWA with swipe, on the Mac mini behind Tailscale; daily 06:00 job; weekly Claude pass via local MCP
- [ ] Phishing handling (Graph `reportMessage` is beta and untested on outlook.com; otherwise junk)

## Decisions

- Customers (sent-to recipients, contacts, client-domain list) are filed under 客戶 and never trashed. Senders the owner names explicitly as "not a client, delete it" are set to `unprotect` by the owner only.
- Even AI and MCP follow the hard rules in `AGENTS.md`; MCP may create rules but cannot make review or protection decisions. No push notifications or summary email; backend log only.
- Python and SQLite (metadata only). The repo is public; the old private `jev-outlook-cleaner` rules are reference only and are never imported.
