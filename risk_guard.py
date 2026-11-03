"""Account loss limit: pause new automatic buys after a bad run.

Two rules, checked before the morning buys (auto_trader.py):

  - Monthly loss: realized P&L of trades closed in the last 30 days is a
    loss of MAX_MONTHLY_LOSS_PCT (default 6) % of PAPER_ACCOUNT_BALANCE or
    more. The pause ends by itself as those losses get older than 30 days.
  - Losing streak: the last LOSING_STREAK_LIMIT (default 5) closed trades
    were all losses. The pause lasts STREAK_PAUSE_DAYS (7) calendar days
    after the last of them; one win ends the streak.

Open trades keep their stops and targets; only new automatic buys wait.
Dashboard Buy clicks are not blocked. 0 turns a rule off.
"""

import os
from datetime import datetime, timedelta, timezone

from paper_journal import get_closed_trades

MONTH_DAYS = 30
STREAK_PAUSE_DAYS = 7


def _settings():
    return {
        "balance": float(os.getenv("PAPER_ACCOUNT_BALANCE", "10000")),
        "month_loss_pct": float(os.getenv("MAX_MONTHLY_LOSS_PCT", "6")),
        "streak": int(os.getenv("LOSING_STREAK_LIMIT", "5")),
    }


def _exit_time(trade):
    try:
        moment = datetime.fromisoformat(str(trade["exit_time"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def trading_pause(database_path=None, now=None):
    """Why new automatic buys are paused, or None."""
    settings = _settings()
    now = now or datetime.now(timezone.utc)
    closed = [
        (moment, float(trade["realized_pnl"] or 0))
        for trade in get_closed_trades(database_path)
        if (moment := _exit_time(trade)) is not None
    ]
    closed.sort(key=lambda item: item[0], reverse=True)

    if settings["month_loss_pct"] > 0 and settings["balance"] > 0:
        month = sum(pnl for moment, pnl in closed if moment >= now - timedelta(days=MONTH_DAYS))
        limit = settings["balance"] * settings["month_loss_pct"] / 100
        if month <= -limit:
            return (
                f"Lost ${-month:,.2f} in the last {MONTH_DAYS} days, more than "
                f"the {settings['month_loss_pct']:g}% limit (${limit:,.2f}). "
                "New buys wait until older losses drop out of the window."
            )

    streak = settings["streak"]
    if streak > 0 and len(closed) >= streak:
        last = closed[:streak]
        if all(pnl < 0 for _, pnl in last):
            until = last[0][0] + timedelta(days=STREAK_PAUSE_DAYS)
            if now < until:
                return (
                    f"The last {streak} closed trades were all losses. New buys "
                    f"pause until {until.astimezone():%b %d}."
                )
    return None
