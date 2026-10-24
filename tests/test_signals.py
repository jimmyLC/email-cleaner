"""Offline checks for local signals."""

import unittest

from email_cleaner import signals
from email_cleaner.providers.base import MessageMeta


def meta(**kw):
    base = dict(message_id="m", folder="inbox", sender_address="shop@shop.example", sender_name="Shop",
                subject="Weekly deals", preview="")
    return MessageMeta(**(base | kw))


class SignalTests(unittest.TestCase):
    def test_bulk_evidence_from_headers_preview_and_sender(self):
        result = signals.collect(meta(
            sender_address="newsletter@shop.example", preview="Click to unsubscribe",
            headers=(("List-Unsubscribe", "<https://x>"), ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
                     ("Precedence", "bulk"))))
        self.assertEqual(set(result["bulk"]), {"List-Unsubscribe 標頭", "支援一鍵退訂", "Precedence: bulk",
                                               "內文含取消訂閱", "寄件者為電子報／行銷帳號"})

    def test_protect_signals(self):
        self.assertIn("回覆／轉寄串", signals.collect(meta(subject="Re: 報價"))["protect"])
        self.assertIn("驗證碼", signals.collect(meta(subject="Example Corp verification code 000000"))["protect"])
        self.assertIn("收據／發票", signals.collect(meta(preview="您的電子發票已開立"))["protect"])
        self.assertEqual(signals.collect(meta())["protect"], [])
        self.assertIn("密碼／帳號存取", signals.collect(meta(subject="重設您的密碼"))["protect"])
        # Marketing idioms such as 財富密碼 must not lock a newsletter as protected.
        self.assertEqual(signals.collect(meta(subject="3 檔高股息的財富密碼"))["protect"], [])

    def test_authentication_failures_and_impersonation(self):
        result = signals.collect(meta(
            sender_address="support@evil.example", sender_name="Microsoft Account Team",
            headers=(("Authentication-Results", "spf=fail smtp.mailfrom=evil.example; dkim=none; dmarc=fail"),)))
        self.assertIn("SPF fail", result["risk"])
        self.assertIn("DMARC fail", result["risk"])
        self.assertIn("顯示名稱冒充 microsoft", result["risk"])

    def test_genuine_brand_and_passing_auth_are_not_risky(self):
        result = signals.collect(meta(
            sender_address="account@microsoft.com", sender_name="Microsoft",
            headers=(("Authentication-Results", "spf=pass; dkim=pass; dmarc=pass"),)))
        self.assertEqual(result["risk"], [])

    def test_statement_with_pdf_password_is_a_statement_not_an_account_notice(self):
        result = signals.collect(meta(subject="中國信託銀行電子對帳單", preview="本對帳單已加密，開啟密碼請參考說明"))
        self.assertIn("對帳單", result["protect"])
        self.assertNotIn("密碼／帳號存取", result["protect"])

    def test_marketing_subdomain_counts_as_bulk_but_bare_domain_does_not(self):
        self.assertIn("寄件網域為行銷子網域", signals.collect(meta(sender_address="trip@newsletter.trip.example"))["bulk"])
        self.assertNotIn("寄件網域為行銷子網域", signals.collect(meta(sender_address="info@news.example"))["bulk"])

    def test_login_success_and_new_sign_in_are_security_notices(self):
        for subject in ("範例商店登入成功通知", "新的登入：來自範例裝置", "Username changed on Example"):
            self.assertIn("安全通知", signals.collect(meta(subject=subject))["protect"], subject)

    def test_missing_policy_is_not_a_failure(self):
        result = signals.collect(meta(headers=(("Authentication-Results", "spf=none; dkim=none; dmarc=none"),)))
        self.assertEqual(result["risk"], [])

    def test_display_name_with_foreign_address(self):
        result = signals.collect(meta(sender_address="x@evil.example", sender_name="billing@bank.example"))
        self.assertIn("顯示名稱含其他網域的地址", result["risk"])


if __name__ == "__main__":
    unittest.main()
