"""Earnings-date warning for swing-trade candidates.

A swing trade held 5-15 days can run into an earnings report, and the
overnight gap can jump past the ATR stop. Candidates with earnings inside
the warning window are flagged EARNINGS_SOON so the gap risk is reviewed
before acting. They are NOT rejected: the 2026-10-21 backtest
(backtests/backtest_earnings_blackout.py) showed a hard block cut the
7-year return from 33.9% to 17.7%. A failed lookup is reported as UNKNOWN.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np

EARNINGS_WARNING_TRADING_DAYS = 5
EARNINGS_LOOKUP_LIMIT = 8
MARKET_TIMEZONE = ZoneInfo("America/New_York")

STATUS_CLEAR = "CLEAR"
STATUS_SOON = "EARNINGS_SOON"
STATUS_UNKNOWN = "UNKNOWN"


def market_today():
    return datetime.now(MARKET_TIMEZONE).date()


def trading_days_until(as_of, earnings_date):
    """Weekdays from as_of until earnings_date; 0 means the same day.

    Market holidays are not excluded, so the count can be one day high
    around a holiday.
    """
    return int(np.busday_count(as_of, earnings_date))


def fetch_earnings_dates(ticker, limit=EARNINGS_LOOKUP_LIMIT):
    """Past and scheduled earnings dates (market-local) from Yahoo Finance."""
    import yfinance as yf

    frame = yf.Ticker(ticker).get_earnings_dates(limit=limit)
    if frame is None or frame.empty:
        return []
    return sorted({timestamp.date() for timestamp in frame.index})


def check_earnings(
    ticker,
    as_of=None,
    warning_days=EARNINGS_WARNING_TRADING_DAYS,
    fetch=fetch_earnings_dates,
):
    as_of = as_of or market_today()
    result = {
        "status": STATUS_UNKNOWN,
        "next_earnings_date": None,
        "trading_days_until": None,
        "warning_trading_days": warning_days,
        "detail": "",
    }

    try:
        dates = fetch(ticker)
    except Exception as exc:  # yfinance raises many unrelated error types
        result["detail"] = f"Earnings lookup failed: {exc}"
        return result

    upcoming = [
        earnings_date
        for earnings_date in dates
        if isinstance(earnings_date, date) and earnings_date >= as_of
    ]
    if not upcoming:
        result["detail"] = "No scheduled earnings date found."
        return result

    next_date = min(upcoming)
    days = trading_days_until(as_of, next_date)
    result["next_earnings_date"] = next_date.isoformat()
    result["trading_days_until"] = days

    if days <= warning_days:
        result["status"] = STATUS_SOON
        result["detail"] = (
            f"Earnings on {next_date.isoformat()} in {days} trading day(s). "
            "The trade may hold through the report; review gap risk."
        )
    else:
        result["status"] = STATUS_CLEAR
        result["detail"] = (
            f"Next earnings {next_date.isoformat()} "
            f"in {days} trading day(s)."
        )
    return result
