"""One place that knows how to build each provider, so the CLI and MCP stay provider-neutral."""

from __future__ import annotations

from typing import Any

PROVIDERS = ("outlook", "gmail")


def reader(name: str) -> Any:
    """Read-only provider for sync and Jev body fetches."""
    if name == "gmail":
        from email_cleaner.providers.gmail import GmailAuth, GmailProvider
        return GmailProvider(GmailAuth())
    from email_cleaner.providers.outlook import OutlookAuth, OutlookProvider
    return OutlookProvider(OutlookAuth())


def writer(name: str) -> Any:
    """Writer for the executor; needs the write scope to have been granted."""
    if name == "gmail":
        from email_cleaner.providers.gmail import WRITE_SCOPES, GmailAuth, GmailWriter
        return GmailWriter(GmailAuth(scopes=WRITE_SCOPES))
    from email_cleaner.providers.outlook import WRITE_SCOPES, OutlookAuth, OutlookWriter
    return OutlookWriter(OutlookAuth(scopes=WRITE_SCOPES))


def login(name: str, write: bool) -> str:
    if name == "gmail":
        from email_cleaner.providers.gmail import WRITE_SCOPES, GmailAuth
        GmailAuth(scopes=WRITE_SCOPES if write else None).login()
        return "Gmail sign-in complete " + ("(can relabel and trash mail)." if write else "(read-only scopes).")
    from email_cleaner.providers.outlook import WRITE_SCOPES, OutlookAuth
    OutlookAuth(scopes=WRITE_SCOPES if write else None).device_login()
    return "Outlook sign-in complete " + ("(can move mail)." if write else "(read-only scopes).")
