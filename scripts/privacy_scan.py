#!/usr/bin/env python3
"""Check that nothing private is in the Git history before a push.

Private values come from the host only: `.env` values, and addresses, domains
and rule text stored in the local SQLite index. The scan searches every commit
reachable from the given refs plus the staged index, and lists forbidden
filenames anywhere in history. It prints locations and counts, never values.

Usage: python scripts/privacy_scan.py [--env PATH ...] [--json PATH ...] [REF ...]
Exit status 0 means clean, 1 means a finding, 2 means the scan could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = Path(__file__).resolve().parent / "privacy_allowlist.txt"
MIN_LENGTH = 6
FORBIDDEN_PATH = re.compile(
    r"(^|/)(\.env(?!\.example$)[^/]*|data/.*|.*\.sqlite[^/]*|.*\.db|.*token[^/]*|.*credential[^/]*|"
    r".*secret[^/]*|client_secret[^/]*\.json|.*\.(pem|key|p12|log))$",
    re.IGNORECASE,
)


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if check and result.returncode not in (0, 1):
        raise RuntimeError(f"git {args[0]} failed")
    return result.stdout


def env_values(path: Path) -> set[str]:
    values = set()
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            values.add(value.strip().strip("'\""))
    return values


def json_strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(json_strings(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(json_strings(item) for item in value)) if value else set()
    return set()


def database_values(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    queries = (
        "SELECT sender_address FROM messages", "SELECT subject FROM messages",
        "SELECT address FROM known_correspondents", "SELECT domain FROM client_domains",
        "SELECT sender FROM rules", "SELECT subject_keyword FROM rules", "SELECT id FROM accounts",
    )
    values = set()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for query in queries:
            try:
                values.update(str(row[0]) for row in connection.execute(query) if row[0])
            except sqlite3.OperationalError:
                continue  # table absent in an older or partial store
    finally:
        connection.close()
    return values


def private_values(env_paths: list[Path], json_paths: list[Path], db_path: Path) -> set[str]:
    values: set[str] = set()
    for path in env_paths:
        values |= env_values(path)
    for path in json_paths:
        if path.is_file():
            values |= json_strings(json.loads(path.read_text(encoding="utf-8")))
    values |= database_values(db_path)
    # Short or generic strings would match ordinary source text. The allowlist
    # holds reviewed generic phrases (e.g. a common subject line) that are not
    # private even when they also appear in the owner's mailbox.
    return {value for value in values
            if len(value) >= MIN_LENGTH and value.strip() and value not in allowed_values()}


def allowed_values() -> set[str]:
    if not ALLOWLIST.is_file():
        return set()
    return {line.rstrip("\n") for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")}


def scan(refs: list[str], values: set[str]) -> list[str]:
    findings = []
    commits = [line for line in git("rev-list", *refs).splitlines() if line] if refs else []
    for line in git("log", "--all", "--name-only", "--format=").splitlines():
        if line and FORBIDDEN_PATH.search(line):
            findings.append(f"forbidden filename in history: {line}")
    for line in git("ls-files").splitlines():
        if FORBIDDEN_PATH.search(line):
            findings.append(f"forbidden filename staged: {line}")
    if not values:
        return sorted(set(findings))
    fd, pattern_file = tempfile.mkstemp(prefix="privacy-scan-")
    try:
        os.chmod(pattern_file, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(sorted(values)) + "\n")
        for commit in commits:
            for hit in git("grep", "-I", "-l", "-F", "-f", pattern_file, commit).splitlines():
                findings.append(f"private value in commit {hit}")
        for hit in git("grep", "-I", "-l", "-F", "--cached", "-f", pattern_file).splitlines():
            findings.append(f"private value staged in {hit}")
    finally:
        os.unlink(pattern_file)
    return sorted(set(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", action="append", type=Path, default=[], help="extra .env file to compare")
    parser.add_argument("--json", action="append", type=Path, default=[], help="private JSON file to compare")
    parser.add_argument("--db", type=Path, default=None, help="SQLite store (default: data/email-cleaner.sqlite)")
    parser.add_argument("refs", nargs="*", default=["--all"])
    args = parser.parse_args()
    data = Path(os.environ.get("EMAIL_CLEANER_DATA") or ROOT / "data").expanduser()
    try:
        values = private_values([ROOT / ".env", *args.env], args.json, args.db or data / "email-cleaner.sqlite")
        findings = scan(args.refs, values)
    except Exception as exc:  # noqa: BLE001 - report without leaking values
        print(f"privacy scan could not run: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"compared {len(values)} private values against history")
    for finding in findings:
        print(finding)
    print("clean" if not findings else f"{len(findings)} finding(s); do not push")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
