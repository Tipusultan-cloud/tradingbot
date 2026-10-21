import os
import unittest
from unittest.mock import patch

from scan_notifications import _email_settings, send_scan_email


class EmailNotificationTests(unittest.TestCase):
    def test_email_settings_are_optional(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_email_settings())

    def test_recipient_alone_does_not_enable_sending(self):
        with patch.dict(
            "os.environ",
            {"TRADINGBOT_ALERT_EMAIL": "alerts@example.com"},
            clear=True,
        ):
            self.assertIsNone(_email_settings())

    def test_partial_smtp_settings_fail_explicitly(self):
        with patch.dict(
            "os.environ",
            {"SMTP_HOST": "smtp.gmail.com"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                _email_settings()

    def test_port_must_be_valid(self):
        settings = {
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "70000",
            "SMTP_USERNAME": "sender@example.com",
            "SMTP_PASSWORD": "app-password",
            "TRADINGBOT_ALERT_EMAIL": "alerts@example.com",
        }
        with patch.dict(os.environ, settings, clear=True):
            with self.assertRaisesRegex(RuntimeError, "between 1 and 65535"):
                _email_settings()

    def test_sends_paper_only_scan_email(self):
        settings = {
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "465",
            "SMTP_USERNAME": "sender@example.com",
            "SMTP_PASSWORD": "app-password",
            "TRADINGBOT_ALERT_EMAIL": "alerts@example.com",
        }
        with (
            patch.dict(os.environ, settings, clear=True),
            patch("scan_notifications.build_scan_message", return_value="NO TRADE"),
            patch("scan_notifications.smtplib.SMTP_SSL") as smtp_ssl,
        ):
            self.assertTrue(send_scan_email())

        smtp_ssl.assert_called_once()
        smtp_ssl.return_value.__enter__.return_value.login.assert_called_once_with(
            "sender@example.com",
            "app-password",
        )
        sent_message = (
            smtp_ssl.return_value.__enter__.return_value.send_message.call_args[0][0]
        )
        self.assertEqual(sent_message["To"], "alerts@example.com")
        self.assertIn("NO TRADE", sent_message.get_content())
        self.assertIn("not a trade order", sent_message.get_content())


if __name__ == "__main__":
    unittest.main()
