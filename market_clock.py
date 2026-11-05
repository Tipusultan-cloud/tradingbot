"""When scan results count as current.

The full scan runs each weekday evening after the close (6:00 PM
Central) and the buy run the next morning (8:45 AM Central), see
scripts/install_daily_task.ps1. Results are current while they were made
after the close of the weekday before today, so Friday evening's list is
still current on Monday morning and a missed evening scan shows as stale
the next day. Market holidays are not known here; the evening scan runs
on them too, so the rule still holds.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
MARKET_CLOSE = time(15, 0)


def previous_close(now=None):
    """3:00 PM Central on the weekday before today (Central date)."""
    local = (now or datetime.now(timezone.utc)).astimezone(CENTRAL)
    day = local.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, MARKET_CLOSE, tzinfo=CENTRAL)


def is_current(generated, now=None):
    """True when `generated` (aware datetime) is after the previous close."""
    return generated >= previous_close(now)


def stale_message(label, generated, now=None):
    age = ((now or datetime.now(timezone.utc)) - generated).total_seconds() / 3600
    cutoff = previous_close(now)
    return (
        f"{label} results are stale: {age:.1f} hours old, made before the "
        f"{cutoff:%a %b %d} close"
    )


def is_trading_day(day=None, client=None):
    """True when the US market opens on `day` (Central date, default
    today), from the Alpaca calendar. Weekdays count when Alpaca cannot be
    asked (auto trading off, no network)."""
    day = day or datetime.now(timezone.utc).astimezone(CENTRAL).date()
    try:
        if client is None:
            from broker import get_trading_client

            client = get_trading_client()
        from alpaca.trading.requests import GetCalendarRequest

        days = client.get_calendar(GetCalendarRequest(start=day, end=day))
    except Exception:  # auto trading off, network and API errors
        return day.weekday() < 5
    return any(getattr(item, "date", None) == day for item in days)


if __name__ == "__main__":
    # `python market_clock.py trading-day`: exit 0 on a trading day, else 1
    # (used by scripts/keep_awake.ps1).
    import sys

    if sys.argv[1:] == ["trading-day"]:
        raise SystemExit(0 if is_trading_day() else 1)
