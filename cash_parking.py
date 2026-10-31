"""Park part of the bot's idle cash in SPY on the Alpaca paper account.

The bot's money is PAPER_ACCOUNT_BALANCE (the same reference trades are
sized on). Cash not in an open trade or a waiting entry order is idle;
SPY_PARK_FRACTION of it (default 0.5) is kept in SPY. rebalance() buys or
sells SPY to match, only while the market is open, and skips changes
smaller than 2% of the balance so it does not trade back and forth.
SPY_PARK_FRACTION=0 sells any parked SPY and stops parking.

Backtest (10 years, 2 slots): 13.8% a year with idle cash unused, 22.3%
with half of it in SPY, 24.2% with all of it; worst drop 12% / 23% / 34%
(SPY alone 15.3% a year, 34%). See PROJECT_LOG.md, 2026-10-31.

SPY is not a journal trade, so it never uses a trade slot. Any SPY in the
Alpaca paper account is treated as parked cash.

The Alpaca client and price lookup are passed in so tests can use fakes.
"""

import os

from paper_journal import _connection, get_pending_broker_orders, initialize_journal

PARK_SYMBOL = "SPY"
MIN_CHANGE_PCT = 2.0


def park_fraction():
    value = float(os.getenv("SPY_PARK_FRACTION", "0.5"))
    return min(max(value, 0.0), 1.0)


def bot_capital():
    return float(os.getenv("PAPER_ACCOUNT_BALANCE", "10000"))


def money_in_trades(price_lookup, database_path=None):
    """Cost of open trades (remaining shares) plus waiting entry orders."""
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        rows = connection.execute(
            "SELECT entry_price, shares, partial_shares FROM paper_trades "
            "WHERE status = 'OPEN'"
        ).fetchall()
    total = sum(
        row["entry_price"] * (row["shares"] - (row["partial_shares"] or 0))
        for row in rows
    )
    for order in get_pending_broker_orders(database_path):
        total += order["shares"] * float(price_lookup(order["ticker"]))
    return total


def parked_position(client):
    """(shares, market value, unrealized P/L) of SPY held at Alpaca."""
    for position in client.get_all_positions():
        if position.symbol == PARK_SYMBOL:
            return (
                float(position.qty),
                float(position.market_value),
                float(position.unrealized_pl),
            )
    return 0.0, 0.0, 0.0


def plan(client, price_lookup, database_path=None):
    """What rebalance() would do: target, current value and the change."""
    capital = bot_capital()
    in_trades = money_in_trades(price_lookup, database_path)
    idle = max(capital - in_trades, 0.0)
    target = park_fraction() * idle
    shares, value, unrealized = parked_position(client)
    return {
        "fraction": park_fraction(),
        "capital": capital,
        "in_trades": round(in_trades, 2),
        "idle": round(idle, 2),
        "target": round(target, 2),
        "shares": shares,
        "value": round(value, 2),
        "unrealized_pl": round(unrealized, 2),
        "change": round(target - value, 2),
    }


def rebalance(client, price_lookup, database_path=None):
    """Buy or sell SPY so it matches the target; returns the plan + action."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    state = plan(client, price_lookup, database_path)
    state["action"] = "none"
    if not client.get_clock().is_open:
        state["action"] = "market closed"
        return state

    change = state["change"]
    if state["target"] == 0 and state["shares"] > 0:
        client.close_position(PARK_SYMBOL)
        state["action"] = f"sold all {state['shares']:g} {PARK_SYMBOL}"
        return state
    if abs(change) < state["capital"] * MIN_CHANGE_PCT / 100:
        return state

    if change > 0:
        client.submit_order(MarketOrderRequest(
            symbol=PARK_SYMBOL,
            notional=round(change, 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        state["action"] = f"bought ${change:,.2f} of {PARK_SYMBOL}"
    else:
        price = float(price_lookup(PARK_SYMBOL))
        qty = min(round(-change / price, 6), state["shares"])
        if qty <= 0:
            return state
        client.submit_order(MarketOrderRequest(
            symbol=PARK_SYMBOL,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        state["action"] = f"sold {qty:g} {PARK_SYMBOL} (~${-change:,.2f})"
    return state
