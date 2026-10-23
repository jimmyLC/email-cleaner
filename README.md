# email-cleaner

A personal, self-hosted inbox cleaner for Outlook and Gmail. It learns readable rules from your confirmations, classifies the rest with TypeSafe Jev, protects client correspondence, and offers one-click unsubscribe without sending email. It runs on your own machine and is reached only over your private network.

Status: phase 1 (read-only Outlook index). See [docs/DESIGN.md](docs/DESIGN.md) and [docs/PLAN.md](docs/PLAN.md).

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # set OUTLOOK_CLIENT_ID (and TYPESAFE_API_KEY for later phases)
chmod 600 .env
```

The Microsoft Entra app must allow public client (device code) flows. Phase 1 requests only `Mail.Read`, `Contacts.Read` and `User.Read`, and the Graph client issues GET requests only.

## Phase 1 commands (read-only)

```sh
.venv/bin/email-cleaner login outlook          # device-code sign-in in the terminal
.venv/bin/email-cleaner sync outlook           # index Inbox + Sent Items metadata (add --full to re-read all)
.venv/bin/email-cleaner stats outlook
.venv/bin/email-cleaner groups outlook --limit 50
.venv/bin/email-cleaner client-domain add client.example
```

The index stores sender, subject, dates, flags, provider categories and computed signals. It never stores message bodies. Protection is decided locally: anyone you have sent mail to, your contacts, and your client domains.

## Tests and privacy scan

```sh
.venv/bin/python -m unittest discover -s tests -t .
python3 scripts/privacy_scan.py     # run before every push
```

Credentials, tokens, rules, the mail index and action logs stay on the host and are never committed.

## License

MIT
