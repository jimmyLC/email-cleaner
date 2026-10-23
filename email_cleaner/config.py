"""Host-local paths. Everything under DATA_DIR is private and git-ignored."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Return the private data directory, creating it owner-only if needed."""
    configured = os.environ.get("EMAIL_CLEANER_DATA", "").strip()
    path = Path(configured).expanduser() if configured else PROJECT / "data"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "email-cleaner.sqlite"
