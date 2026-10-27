import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

from dotenv import load_dotenv

from project_paths import BEST_CURRENT_CANDIDATE_JSON, MASTER_CANDIDATES_JSON


def _email_settings():
    names = (
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "TRADINGBOT_ALERT_EMAIL",
    )
    values = {name: os.getenv(name, "").strip() for name in names}
    smtp_names = names[:-1]
    if not any(values[name] for name in smtp_names):
        return None
    if not all(values.values()):
        raise RuntimeError(
            "Email settings are incomplete. Set SMTP_HOST, SMTP_PORT, "
            "SMTP_USERNAME, SMTP_PASSWORD, and TRADINGBOT_ALERT_EMAIL in .env."
        )
    try:
        port = int(values["SMTP_PORT"])
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be a number") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("SMTP_PORT must be between 1 and 65535")
    if "@" not in values["SMTP_USERNAME"] or "@" not in values["TRADINGBOT_ALERT_EMAIL"]:
        raise RuntimeError("SMTP_USERNAME and TRADINGBOT_ALERT_EMAIL must be email addresses")
    values["SMTP_PORT"] = port
    return values


def _read_json(path):
    import json

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def build_scan_message():
    master = _read_json(MASTER_CANDIDATES_JSON)
    decision = _read_json(BEST_CURRENT_CANDIDATE_JSON)
    candidate = master.get("best_current") or decision.get("selected")

    if not isinstance(candidate, dict):
        market_state = (
            "bullish"
            if master.get("market_bullish") is True
            else "not bullish"
        )
        return (
            f"TradingBot scan: NO TRADE. Market is {market_state}. "
            "No candidate passed all master rules."
        )

    ticker = candidate.get("ticker", "Unknown")
    sector = candidate.get("sector", "Unknown sector")
    score = candidate.get("setup_score", "n/a")
    price = candidate.get("price")
    price_text = f" Entry reference ${float(price):.2f}." if price else ""
    earnings = candidate.get("earnings")
    earnings_text = (
        f" Earnings check {earnings.get('status', 'UNKNOWN')}: "
        f"{earnings.get('detail', '')}"
        if isinstance(earnings, dict)
        else ""
    )
    news = candidate.get("news")
    news_text = (
        f" News check {news.get('status', 'UNKNOWN')}: "
        f"{news.get('detail', '')}"
        if isinstance(news, dict)
        else ""
    )
    return (
        f"TradingBot scan: candidate {ticker} ({sector}), setup score "
        f"{score}/100.{price_text}{earnings_text}{news_text} "
        "Review dashboard; this is not an order."
    )


def send_scan_email():
    settings = _email_settings()
    if settings is None:
        print("Email alert skipped: SMTP is not configured.")
        return False

    message = EmailMessage()
    message["Subject"] = "TradingBot daily scan summary"
    message["From"] = settings["SMTP_USERNAME"]
    message["To"] = settings["TRADINGBOT_ALERT_EMAIL"]
    message.set_content(
        f"{build_scan_message()}\n\n"
        "This is a paper-trading research alert, not a trade order."
    )
    try:
        with smtplib.SMTP_SSL(
            settings["SMTP_HOST"],
            settings["SMTP_PORT"],
            context=ssl.create_default_context(),
            timeout=15,
        ) as server:
            server.login(settings["SMTP_USERNAME"], settings["SMTP_PASSWORD"])
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise RuntimeError("Could not send the scan email through SMTP") from exc

    print("Email alert sent successfully.")
    return True


def main():
    load_dotenv()
    try:
        send_scan_email()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Email alert failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
