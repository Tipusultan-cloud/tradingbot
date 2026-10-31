"""Automatic paper exits: half at 2R, the rest at 2.5R, stop or 20 days.

Runs in the daily scan before slot_selector.py, so a slot freed by an
exit can be refilled the same morning. Each open trade is replayed on
completed daily bars with the backtested rule (shadow_exits.py
HALF_2R_THEN_2_5R):

  - stop: the recorded stop price (entry - 1.5 ATR)
  - first target: half the shares are sold at 2R above entry, and the
    stop of the rest moves up to the entry price
  - second target: the rest are sold at 2.5R above entry
  - time exit: at the close of the 20th session held
  - the stop is checked before the targets; a gap through a level fills
    at the open; the entry day's high and low are skipped

The half sale and the exit are recorded in the paper journal at about
4 PM New York time on their day. The exit reason (STOP, TARGET,
GAP_STOP, GAP_TARGET or TIME_EXIT) gets a PARTIAL+ prefix when the half
was sold first. A TradingView SELL that arrives first still closes the
trade as before. Nothing is sold at a broker.

Bot 3 (MOMENTUM) journal-only trades use shadow_exits.py TRAIL_3ATR
instead: the stop trails 3 ATR below the highest close, with no target
and no time limit.
"""

import sqlite3
import sys
from datetime import date, datetime, time as clock_time, timezone

from paper_journal import (
    _connection,
    close_trade,
    initialize_journal,
    record_partial_exit,
)
from shadow_exits import NEW_YORK, entry_session_date, fetch_daily_bars, simulate

EXIT_RULE = "HALF_2R_THEN_2_5R"
# Bot 3 (momentum) rides: stop trails 3 ATR below the highest close.
MOMENTUM_EXIT_RULE = "TRAIL_3ATR"


def market_close_utc(exit_date):
    """ISO UTC timestamp for 4 PM New York on an exit date."""
    day = date.fromisoformat(exit_date)
    close = datetime.combine(day, clock_time(16, 0), tzinfo=NEW_YORK)
    return close.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def close_paper_trades(database_path=None, fetch=fetch_daily_bars):
    """Close every open paper trade whose exit rule has fired."""
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        # Alpaca trades have real stop / target orders; auto_trader.py syncs them.
        trades = connection.execute(
            "SELECT * FROM paper_trades "
            "WHERE status = 'OPEN' AND broker_order_id IS NULL ORDER BY id"
        ).fetchall()

    closed = []
    halves = []
    still_open = []
    failures = []
    for trade in trades:
        entry_date = entry_session_date(trade["entry_time"])
        try:
            bars = fetch(trade["ticker"], entry_date)
        except Exception as exc:  # yfinance raises many error types
            failures.append(f"{trade['ticker']}: {exc}")
            continue
        if bars.empty:
            failures.append(f"{trade['ticker']}: no price data")
            continue

        state = simulate(
            MOMENTUM_EXIT_RULE if trade["strategy"] == "MOMENTUM" else EXIT_RULE,
            entry_date,
            trade["entry_price"],
            trade["stop_price"],
            trade["shares"],
            bars,
        )
        if state["partial_shares"] and trade["partial_exit_price"] is None:
            half = record_partial_exit(
                trade["id"],
                state["partial_shares"],
                state["partial_price"],
                market_close_utc(state["partial_date"]),
                active_stop_price=trade["entry_price"],
                database_path=database_path,
            )
            if half is not None:
                halves.append(half)
        if state["status"] != "CLOSED":
            still_open.append(trade["ticker"])
            continue

        result = close_trade(
            trade["id"],
            exit_price=state["exit_price"],
            exit_reason=state["exit_reason"],
            exit_time=market_close_utc(state["exit_date"]),
            database_path=database_path,
        )
        if result is not None:
            closed.append(result)

    return {
        "closed": closed,
        "halves": halves,
        "still_open": still_open,
        "failures": failures,
    }


def main():
    print()
    print("================================")
    print("PAPER EXITS (STOP / HALF 2R + REST 2.5R / 20 DAYS)")
    print("================================")
    try:
        result = close_paper_trades()
    except (OSError, sqlite3.Error) as exc:
        print(f"Paper exit check failed: {exc}", file=sys.stderr)
        return 1

    for trade in result["halves"]:
        print(
            f"HALF SOLD {trade['ticker']}: {trade['partial_shares']} shares at "
            f"${trade['partial_exit_price']:.2f} (2R); stop moved to "
            f"${trade['active_stop_price']:.2f}"
        )
    for trade in result["closed"]:
        print(
            f"CLOSED {trade['ticker']} ({trade['strategy'] or 'unknown'}): "
            f"{trade['exit_reason']} at ${trade['exit_price']:.2f} on "
            f"{trade['exit_time'][:10]}, P&L ${trade['realized_pnl']:+.2f}"
        )
    if result["still_open"]:
        print("Still open:", ", ".join(result["still_open"]))
    if not result["closed"] and not result["still_open"]:
        print("No open paper trades.")
    for failure in result["failures"]:
        print(f"Price lookup failed, will retry next run: {failure}")
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
