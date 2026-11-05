"""Automatic Alpaca paper trades for the bots' slot picks.

  python auto_trader.py --sync   record fills and exits, raise Bot 3 trailing stops
  python auto_trader.py          sync, buy the slot picks, sync again,
                                 then park idle cash in SPY (cash_parking.py)

--sync runs in the evening scan before slot_selector.py (so closed trades
free their slots); the full run is the 8:45 AM Central morning buy
(scripts/scheduled_morning_buy.ps1), 15 minutes after the open. Does nothing unless
AUTO_TRADING=true with Alpaca paper keys in .env (see broker.py), and
only sends orders while the market is open. Only the bots named in
AUTO_BUY_BOTS (default all) are bought automatically; hand-picked trades
come from the dashboard Buy button.
"""

import sys
import time

from broker import auto_buy_bots, auto_trading_status, get_trading_client, latest_price
from cash_parking import rebalance
from earnings_calendar import check_earnings
from paper_journal import get_pending_broker_orders, save_daily_copy
from trade_lessons import fill_missing_lessons
from shadow_exits import fetch_daily_bars
from project_paths import SLOT_SELECTION_JSON
from risk_guard import trading_pause
from slot_selector import load_fresh
from trade_manager import (
    SOURCE_AUTO,
    TradeError,
    earnings_exits,
    open_trade,
    sync,
    update_trailing_stops,
)

FILL_WAIT_SECONDS = 10
# Market buys usually fill in seconds but can take longer at the open
# (REGN took 18 s on 2026-11-02). Keep syncing until every order filled,
# so the stop / exit orders are placed by this run.
FILL_WAIT_TRIES = 9


def print_sync(result):
    for trade in result["filled"]:
        print(
            f"FILLED {trade['ticker']} ({trade['strategy']}, {trade['entry_source']}): "
            f"{trade['shares']} @ ${trade['entry_price']:.2f}, "
            f"stop ${trade['stop_price']:.2f}, target ${trade['target_price']:.2f}"
            + (
                f" (half), ${trade['target2_price']:.2f} (rest)"
                if trade.get("target2_price") is not None
                else ""
            )
        )
    for trade in result.get("halves", []):
        print(
            f"HALF SOLD {trade['ticker']}: {trade['partial_shares']} @ "
            f"${trade['partial_exit_price']:.2f} (2R); stop of the rest moves "
            f"to ${trade['entry_price']:.2f}"
        )
    for trade in result["closed"]:
        print(
            f"CLOSED {trade['ticker']}: {trade['exit_reason']} @ "
            f"${trade['exit_price']:.2f}, P&L ${trade['realized_pnl']:+.2f}"
        )
    for item in result["canceled"]:
        print(f"Entry order not filled: {item}")
    for error in result["errors"]:
        print(f"Alpaca lookup failed, will retry: {error}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    print()
    print("================================")
    print("AUTO TRADER (ALPACA PAPER)")
    print("================================")

    enabled, reason = auto_trading_status()
    if not enabled:
        print("Skipped:", reason)
        return 0

    client = get_trading_client()
    print_sync(sync(client, latest_price))
    for message in update_trailing_stops(client, fetch_daily_bars):
        print(message)
    write_records()
    if "--sync" in argv:
        return 0

    # Sold before buying, so a freed slot or sector can be refilled.
    for message in earnings_exits(client, latest_price, check_earnings):
        print(message)
    buy_picks(client)
    park_cash(client)
    return 0


def write_records():
    """Lesson notes for newly closed trades, then today's dated copy of the
    trade record (data/trade_history/). Neither stops the run."""
    try:
        for ticker, text in fill_missing_lessons():
            print(f"LESSON {ticker}: {text}")
    except Exception as exc:  # journal or data errors
        print(f"Lesson notes failed, will retry next run: {exc}")
    try:
        print(f"Trade record saved: {save_daily_copy()}")
    except Exception as exc:  # disk errors
        print(f"Daily trade record copy failed: {exc}")


def park_cash(client):
    try:
        state = rebalance(client, latest_price)
    except Exception as exc:  # Alpaca rejections and network errors
        print(f"SPY parking failed, will retry next run: {exc}")
        return
    print(
        f"SPY PARKING ({state['fraction']:.0%} of idle cash): idle "
        f"${state['idle']:,.2f}, target ${state['target']:,.2f}, held "
        f"${state['value']:,.2f} -> {state['action']}"
    )


def buy_picks(client):
    warnings = []
    slots = load_fresh(SLOT_SELECTION_JSON, "Slot selection", warnings)
    for warning in warnings:
        print("WARNING:", warning)
    picks = (slots or {}).get("selections") or []
    allowed = auto_buy_bots()
    for stock in picks:
        if stock.get("source") not in allowed:
            print(
                f"Not bought automatically: {stock['ticker']} ({stock.get('source')}); "
                f"auto-buy is on for {', '.join(allowed) or 'no bot'} "
                "(AUTO_BUY_BOTS). Use the dashboard Buy button."
            )
    picks = [stock for stock in picks if stock.get("source") in allowed]
    if not picks:
        print("No slot picks to buy.")
        return
    pause = trading_pause()
    if pause:
        print("BUYING PAUSED:", pause)
        return

    placed = 0
    for stock in picks:
        try:
            order = open_trade(
                client, stock, stock["source"], SOURCE_AUTO, latest_price
            )
        except TradeError as exc:
            print(f"Skipped {stock['ticker']}: {exc}")
            continue
        placed += 1
        print(
            f"ORDER SENT {order['ticker']} ({order['strategy']}): "
            f"{order['shares']} shares ~${order['price']:.2f}, "
            + (
                f"stop ${order['stop']}, trailing 3 ATR, no target"
                if order["target2"] is None
                else f"stop ${order['stop']}, targets ${order['target']} (half) / "
                f"${order['target2']} (rest)"
            )
        )

    if placed:
        for _ in range(FILL_WAIT_TRIES):
            time.sleep(FILL_WAIT_SECONDS)
            print_sync(sync(client, latest_price))
            if not get_pending_broker_orders():
                break
        else:
            print("Some orders have not filled yet; the next sync records them.")


if __name__ == "__main__":
    raise SystemExit(main())
