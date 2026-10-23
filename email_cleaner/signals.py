"""Deterministic local evidence about a message.

Signals are short labels, never message text, so they are safe to store in
the index and to show in the UI. Only headers, subject, sender and the short
provider preview are inspected; bodies are not needed here.
"""

from __future__ import annotations

import re

from email_cleaner.providers.base import MessageMeta

_UNSUBSCRIBE = re.compile(
    r"unsubscribe|opt[- ]out|manage (?:your )?(?:email )?(?:preferences|subscription)|"
    r"view (?:this )?(?:email )?in (?:your )?browser|取消訂閱|退訂|不想再收到|電子報|訂閱設定",
    re.IGNORECASE,
)
_BULK_SENDER = re.compile(
    r"^(?:newsletter|news|promo|promotions?|marketing|edm|deals?|offers?|campaign|mailer|digest|updates?)\b",
    re.IGNORECASE,
)
_BULK_PRECEDENCE = {"bulk", "list", "junk"}

_PROTECT_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"verification code|one[- ]time (?:pass|code)|\botp\b|驗證碼|認證碼|動態密碼", "驗證碼"),
        (r"reset (?:your )?password|password reset|forgot (?:your )?password|change (?:your )?password|"
         r"(?:重設|重置|變更|修改|忘記)(?:您的|你的)?密碼|密碼(?:重設|重置|變更|已更改)", "密碼／帳號存取"),
        (r"security alert|suspicious|unusual (?:sign|activity)|安全性?警示|異常登入|登入通知|新裝置登入", "安全通知"),
        (r"\breceipt\b|\binvoice\b|收據|發票|電子發票|請款", "收據／發票"),
        (r"order (?:confirmation|confirmed|#)|訂單(?:確認|成立)|付款(?:成功|完成)|payment (?:received|confirmation)", "訂單／付款"),
        (r"booking confirm|reservation|itinerary|boarding pass|訂位|訂房|電子機票|登機證", "預訂／行程"),
        (r"friend request|shared (?:a|an|with)|invited you|好友邀請|與您共用|邀請您", "邀請／共用"),
    )
)
_REPLY_PREFIX = re.compile(r"^\s*(?:re|fw|fwd|回覆|轉寄)\s*[:：]", re.IGNORECASE)

# Brands often impersonated in a display name while the address is elsewhere.
_BRANDS = ("microsoft", "outlook", "apple", "icloud", "google", "gmail", "paypal", "amazon",
           "netflix", "dhl", "fedex", "ups", "line", "facebook", "instagram")
# "none" only means the sender publishes no policy; it is common for legitimate mail.
_AUTH_RESULT = re.compile(r"\b(spf|dkim|dmarc)=(fail|softfail|permerror)\b", re.IGNORECASE)
_EMBEDDED_ADDRESS = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")


def header_map(meta: MessageMeta) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in meta.headers:
        key = name.strip().casefold()
        if key and key not in result:
            result[key] = value.strip()
    return result


def bulk_signals(meta: MessageMeta, headers: dict[str, str]) -> list[str]:
    signals = []
    if "list-unsubscribe" in headers:
        signals.append("List-Unsubscribe 標頭")
    if "list-unsubscribe-post" in headers:
        signals.append("支援一鍵退訂")
    if "list-id" in headers:
        signals.append("List-Id 標頭")
    if headers.get("precedence", "").casefold() in _BULK_PRECEDENCE:
        signals.append("Precedence: bulk")
    if headers.get("auto-submitted", "no").casefold() not in {"", "no"}:
        signals.append("自動產生郵件")
    if _UNSUBSCRIBE.search(meta.preview):
        signals.append("內文含取消訂閱")
    if _BULK_SENDER.match(meta.sender_address.partition("@")[0]):
        signals.append("寄件者為電子報／行銷帳號")
    return signals


def protect_signals(meta: MessageMeta) -> list[str]:
    signals = []
    if _REPLY_PREFIX.match(meta.subject):
        signals.append("回覆／轉寄串")
    haystack = f"{meta.subject}\n{meta.preview}"
    signals.extend(label for pattern, label in _PROTECT_PHRASES if pattern.search(haystack))
    return signals


def risk_signals(meta: MessageMeta, headers: dict[str, str]) -> list[str]:
    signals = []
    for mechanism, verdict in _AUTH_RESULT.findall(headers.get("authentication-results", "")):
        signals.append(f"{mechanism.upper()} {verdict.lower()}")
    domain = meta.sender_address.partition("@")[2]
    name = meta.sender_name.casefold()
    embedded = _EMBEDDED_ADDRESS.search(name)
    if embedded and domain and embedded.group(1) != domain:
        signals.append("顯示名稱含其他網域的地址")
    for brand in _BRANDS:
        if re.search(rf"\b{brand}\b", name) and brand not in domain:
            signals.append(f"顯示名稱冒充 {brand}")
            break
    return sorted(set(signals))


def collect(meta: MessageMeta) -> dict[str, list[str]]:
    headers = header_map(meta)
    return {
        "bulk": bulk_signals(meta, headers),
        "protect": protect_signals(meta),
        "risk": risk_signals(meta, headers),
    }
