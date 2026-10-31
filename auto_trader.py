"""Automatic Alpaca paper trades for the bots' slot picks.

  python auto_trader.py --sync   record fills and exits only
  python auto_trader.py          sync, buy the slot picks, sync again,
                                 then park idle cash in SPY (cash_parking.py)

Runs in the daily scan: --sync before slot_selector.py (so closed trades
free their slots), then the full run after it. Does nothing unless
AUTO_TRADING=true with Alpaca paper keys in .env (see broker.py), and
only sends orders while the market is open. Hand-picked trades come
from the dashboard Buy button.
"""

import sys
import time

from broker import auto_trading_status, get_trading_client, latest_price
from cash_parking import rebalance
from project_paths import SLOT_SELECTION_JSON
from slot_selector import load_fresh
from trade_manager import SOURCE_AUTO, TradeError, open_trade, sync

FILL_WAIT_SECONDS = 10


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
    if "--sync" in argv:
        return 0

    buy_picks(client)
    park_cash(client)
    return 0


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
    if not picks:
        print("No slot picks to buy.")
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
            f"stop ${order['stop']}, targets ${order['target']} (half) / "
            f"${order['target2']} (rest)"
        )

    if placed:
        time.sleep(FILL_WAIT_SECONDS)
        print_sync(sync(client, latest_price))


if __name__ == "__main__":
    raise SystemExit(main())
