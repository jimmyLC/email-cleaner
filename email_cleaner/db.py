"""SQLite store: mail index (metadata only), rules, review queue, action log.

Message bodies are never stored here; they are read from the mailbox on demand.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN ('outlook', 'gmail')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per message the index has seen. No body, only evidence for rules.
CREATE TABLE IF NOT EXISTS messages (
  account_id TEXT NOT NULL REFERENCES accounts(id),
  message_id TEXT NOT NULL,
  internet_message_id TEXT,
  folder TEXT NOT NULL,
  sender_address TEXT NOT NULL DEFAULT '',
  sender_name TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  received_at TEXT,
  is_read INTEGER NOT NULL DEFAULT 0,
  has_attachments INTEGER NOT NULL DEFAULT 0,
  provider_categories TEXT NOT NULL DEFAULT '[]',
  signals TEXT NOT NULL DEFAULT '{}',
  snapshot TEXT,
  last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (account_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_by_sender ON messages(account_id, sender_address);

-- Protection evidence: addresses the owner has written to, and contacts.
CREATE TABLE IF NOT EXISTS known_correspondents (
  account_id TEXT NOT NULL REFERENCES accounts(id),
  address TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('sent', 'contact')),
  PRIMARY KEY (account_id, address, source)
);

CREATE TABLE IF NOT EXISTS client_domains (
  domain TEXT PRIMARY KEY,
  added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('address', 'domain', 'domain_subject')),
  sender TEXT NOT NULL,
  subject_keyword TEXT,
  action TEXT NOT NULL CHECK (action IN ('archive', 'trash', 'report')),
  target_folder TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'disabled')),
  origin TEXT NOT NULL CHECK (origin IN ('user', 'claude', 'review', 'unsubscribe')),
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (scope != 'domain_subject' OR subject_keyword IS NOT NULL),
  CHECK (action != 'archive' OR target_folder IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY,
  account_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  suggested_action TEXT,
  confidence REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT,
  resolution TEXT,
  FOREIGN KEY (account_id, message_id) REFERENCES messages(account_id, message_id)
);

-- Every change to a mailbox, with enough detail to undo it.
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  message_id TEXT,
  action TEXT NOT NULL CHECK (action IN ('archive', 'trash', 'report', 'unsubscribe', 'create_rule', 'restore')),
  from_folder TEXT,
  to_folder TEXT,
  rule_id INTEGER REFERENCES rules(id),
  actor TEXT NOT NULL CHECK (actor IN ('user', 'schedule', 'mcp')),
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  undone_at TEXT
);
CREATE INDEX IF NOT EXISTS actions_by_batch ON actions(batch_id);

-- Sender-level questions for the owner, e.g. "you once wrote to this
-- newsletter; keep it protected?". Pending items keep the safe default.
CREATE TABLE IF NOT EXISTS sender_reviews (
  account_id TEXT NOT NULL REFERENCES accounts(id),
  address TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('protection')),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  resolution TEXT CHECK (resolution IN ('keep', 'unprotect')),
  resolved_at TEXT,
  PRIMARY KEY (account_id, address, kind)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open (and if needed create) the store with owner-only permissions."""
    new = not path.exists()
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    if new:
        path.chmod(0o600)
    migrate(connection)
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    row = connection.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        connection.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] > SCHEMA_VERSION:
        raise RuntimeError("Database was created by a newer email-cleaner; upgrade the code first.")
    elif row["version"] < SCHEMA_VERSION:
        # Every change so far only adds tables, which SCHEMA creates above.
        connection.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    connection.commit()
