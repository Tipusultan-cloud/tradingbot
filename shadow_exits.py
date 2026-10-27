"""Shadow exits: replay alternative exit rules on real paper trades.

Each paper trade is replayed on daily bars with two rules and stored
next to the TradingView exit actually taken, so the rules can be
compared on forward data:

  RULE_2R_20D    sell all at 2R, or at the close after 20 days
  HALF_2R_TRAIL  sell half at 2R, move the stop to breakeven, trail
                 the rest 3 ATR below the highest close

Nothing here places or changes orders; it only records what each rule
would have done. Results come from the backtest
backtests/backtest_trend_exits.py (2026-10-27).

Replay details:
  - Risk per share is the recorded entry minus the recorded stop.
  - The entry day's high and low are skipped (the entry time within
    the day is unknown); its close still moves the trailing stop.
  - When a bar touches both the stop and a target, the stop is
    assumed to fill first. No slippage is applied.
"""

import sqlite3
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from paper_journal import _connection, initialize_journal

NEW_YORK = ZoneInfo("America/New_York")

RULES = {
    "RULE_2R_20D": {"target_r": 2.0, "max_hold_days": 20},
    "HALF_2R_TRAIL": {"partial_r": 2.0, "trail_atr": 3.0},
}

# Enough history before entry for a settled 14-day ATR.
HISTORY_DAYS_BEFORE_ENTRY = 60


def initialize_shadow_table(database_path=None):
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_exits (
                trade_id INTEGER NOT NULL REFERENCES paper_trades(id),
                rule TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
                stop_price REAL,
                partial_price REAL,
                exit_date TEXT,
                exit_price REAL,
                exit_reason TEXT,
                pnl REAL,
                r_multiple REAL,
                last_bar_date TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_id, rule)
            )
            """
        )


def entry_session_date(entry_time):
    """New York trading date of an ISO entry timestamp."""
    moment = datetime.fromisoformat(str(entry_time).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(NEW_YORK).date()


def simulate(rule, entry_date, entry_price, stop_price, shares, bars):
    """Replay one exit rule on daily bars (Open/High/Low/Close/ATR).

    `bars` may start before the entry date; earlier rows are ignored.
    Returns the rule's state after the last bar.
    """
    settings = RULES[rule]
    risk_per_share = entry_price - stop_price
    planned_risk = risk_per_share * shares
    target_r = settings.get("target_r")
    partial_r = settings.get("partial_r")
    target = entry_price + risk_per_share * target_r if target_r else None
    partial_level = entry_price + risk_per_share * partial_r if partial_r else None

    stop = stop_price
    highest_close = entry_price
    shares_open = shares
    realized = 0.0
    partial_price = None
    last_close = entry_price
    last_bar_date = None

    def result(status, exit_date=None, exit_price=None, reason=None):
        mark = exit_price if exit_price is not None else last_close
        pnl = realized + shares_open * mark - entry_price * shares
        if partial_price is not None and reason:
            reason = f"PARTIAL+{reason}"
        return {
            "status": status,
            "stop_price": stop,
            "partial_price": partial_price,
            "exit_date": exit_date,
            "exit_price": exit_price,
            "exit_reason": reason,
            "pnl": pnl,
            "r_multiple": pnl / planned_risk if planned_risk > 0 else 0.0,
            "last_bar_date": last_bar_date,
        }

    def stop_reason():
        return "TRAIL_STOP" if stop > stop_price else "STOP"

    def take_partial(price):
        nonlocal shares_open, realized, partial_price, stop
        sold = shares // 2
        shares_open -= sold
        realized += sold * price
        partial_price = price
        stop = max(stop, entry_price)

    held = bars[bars.index.date >= entry_date]
    for hold_days, (day, bar) in enumerate(held.iterrows(), start=1):
        day = day.date().isoformat()
        last_bar_date = day
        open_, high, low, close = (
            float(bar["Open"]), float(bar["High"]),
            float(bar["Low"]), float(bar["Close"]),
        )

        if hold_days > 1:
            if open_ <= stop:
                return result("CLOSED", day, open_, f"GAP_{stop_reason()}")
            if target and open_ >= target:
                return result("CLOSED", day, open_, "GAP_TARGET")
            if partial_level and partial_price is None and open_ >= partial_level:
                take_partial(open_)

            if low <= stop:
                return result("CLOSED", day, stop, stop_reason())
            if target and high >= target:
                return result("CLOSED", day, target, "TARGET")
            if partial_level and partial_price is None and high >= partial_level:
                take_partial(partial_level)

        last_close = close
        max_hold = settings.get("max_hold_days")
        if max_hold and hold_days >= max_hold:
            return result("CLOSED", day, close, "TIME_EXIT")

        highest_close = max(highest_close, close)
        if settings.get("trail_atr") and pd.notna(bar["ATR"]):
            trail = highest_close - settings["trail_atr"] * float(bar["ATR"])
            stop = max(stop, trail)

    return result("OPEN")


def fetch_daily_bars(ticker, entry_date):
    """Completed daily bars with ATR, from before entry until today."""
    import yfinance as yf

    from scanner import (
        calculate_indicators,
        extract_ticker_frame,
        remove_incomplete_daily_bar,
    )

    data = yf.download(
        tickers=[ticker],
        start=entry_date - timedelta(days=HISTORY_DAYS_BEFORE_ENTRY),
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        threads=False,
        progress=False,
    )
    frame = remove_incomplete_daily_bar(extract_ticker_frame(data, ticker))
    if frame.empty:
        return frame
    return calculate_indicators(frame)


def update_shadow_exits(database_path=None, fetch=fetch_daily_bars):
    """Replay every rule still OPEN for each paper trade."""
    initialize_shadow_table(database_path)
    with _connection(database_path) as connection:
        trades = connection.execute("SELECT * FROM paper_trades ORDER BY id").fetchall()
        closed = {
            (row["trade_id"], row["rule"])
            for row in connection.execute(
                "SELECT trade_id, rule FROM shadow_exits WHERE status = 'CLOSED'"
            )
        }

    updated = 0
    failures = []
    for trade in trades:
        pending = [rule for rule in RULES if (trade["id"], rule) not in closed]
        if not pending:
            continue
        entry_date = entry_session_date(trade["entry_time"])
        try:
            bars = fetch(trade["ticker"], entry_date)
        except Exception as exc:  # yfinance raises many error types
            failures.append(f"{trade['ticker']}: {exc}")
            continue

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with _connection(database_path) as connection:
            for rule in pending:
                state = simulate(
                    rule,
                    entry_date,
                    trade["entry_price"],
                    trade["stop_price"],
                    trade["shares"],
                    bars,
                )
                connection.execute(
                    """
                    INSERT INTO shadow_exits (
                        trade_id, rule, status, stop_price, partial_price,
                        exit_date, exit_price, exit_reason, pnl, r_multiple,
                        last_bar_date, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_id, rule) DO UPDATE SET
                        status = excluded.status,
                        stop_price = excluded.stop_price,
                        partial_price = excluded.partial_price,
                        exit_date = excluded.exit_date,
                        exit_price = excluded.exit_price,
                        exit_reason = excluded.exit_reason,
                        pnl = excluded.pnl,
                        r_multiple = excluded.r_multiple,
                        last_bar_date = excluded.last_bar_date,
                        updated_at = excluded.updated_at
                    """,
                    (
                        trade["id"], rule, state["status"], state["stop_price"],
                        state["partial_price"], state["exit_date"],
                        state["exit_price"], state["exit_reason"], state["pnl"],
                        state["r_multiple"], state["last_bar_date"], now,
                    ),
                )
                updated += 1
    return {"updated": updated, "failures": failures}


def get_shadow_comparison(database_path=None):
    """Per-trade R for the actual exit and each shadow rule, plus totals."""
    initialize_shadow_table(database_path)
    with _connection(database_path) as connection:
        trades = connection.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC"
        ).fetchall()
        shadows = connection.execute("SELECT * FROM shadow_exits").fetchall()

    by_trade = {}
    for row in shadows:
        by_trade.setdefault(row["trade_id"], {})[row["rule"]] = dict(row)

    rows = []
    totals = {
        "ACTUAL": {"closed": 0, "total_r": 0.0, "pnl": 0.0},
        **{rule: {"closed": 0, "total_r": 0.0, "pnl": 0.0} for rule in RULES},
    }
    for trade in trades:
        planned_risk = (trade["entry_price"] - trade["stop_price"]) * trade["shares"]
        row = {
            "trade_id": trade["id"],
            "ticker": trade["ticker"],
            "entry_time": trade["entry_time"],
            "entry_price": trade["entry_price"],
            "actual_status": trade["status"],
            "actual_r": (
                trade["realized_pnl"] / planned_risk
                if trade["status"] == "CLOSED" and planned_risk > 0
                else None
            ),
        }
        if row["actual_r"] is not None:
            totals["ACTUAL"]["closed"] += 1
            totals["ACTUAL"]["total_r"] += row["actual_r"]
            totals["ACTUAL"]["pnl"] += trade["realized_pnl"]
        for rule in RULES:
            shadow = by_trade.get(trade["id"], {}).get(rule)
            row[f"{rule}_status"] = shadow["status"] if shadow else None
            row[f"{rule}_r"] = shadow["r_multiple"] if shadow else None
            row[f"{rule}_reason"] = shadow["exit_reason"] if shadow else None
            if shadow and shadow["status"] == "CLOSED":
                totals[rule]["closed"] += 1
                totals[rule]["total_r"] += shadow["r_multiple"]
                totals[rule]["pnl"] += shadow["pnl"]
        rows.append(row)
    return {"trades": rows, "totals": totals}


def main():
    try:
        result = update_shadow_exits()
    except (OSError, sqlite3.Error) as exc:
        print(f"Shadow exit update failed: {exc}", file=sys.stderr)
        return 1

    print(f"Shadow exits updated: {result['updated']} rule result(s).")
    for failure in result["failures"]:
        print(f"Price lookup failed, will retry next run: {failure}")

    totals = get_shadow_comparison()["totals"]
    print()
    print("Closed trades, total R by exit:")
    for name, total in totals.items():
        print(f"  {name:14s} {total['closed']:3d} closed  {total['total_r']:+.2f}R")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
