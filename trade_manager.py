"""Open and track Alpaca paper trades for the trade slots.

open_trade() sends a bracket order: a market buy plus a stop-loss at
price - 1.5 ATR and a take-profit at 2R, both held at Alpaca until one
fills (good till canceled). Shares risk 1% of PAPER_ACCOUNT_BALANCE.
The 2-slot limit counts open trades and orders not yet filled.

sync() brings the journal up to date with Alpaca:
  - a filled entry order becomes an open journal trade (fill price)
  - a filled stop or target leg closes the trade (STOP / TARGET)
  - a trade held 20 sessions is sold at market (TIME_EXIT), as in the
    backtests

The Alpaca client and price lookup are passed in so tests can use fakes.
"""

import math
import os
import time
from datetime import date, datetime, timedelta, timezone

from paper_journal import (
    _connection,
    add_broker_order,
    close_trade,
    finish_broker_order,
    get_pending_broker_orders,
    initialize_journal,
    record_entry,
)
from slot_selector import max_open_trades

MAX_HOLD_SESSIONS = 20
SOURCE_AUTO = "AUTO"
SOURCE_MANUAL = "MANUAL"
CANCELED_STATUSES = {"canceled", "expired", "rejected", "suspended", "stopped"}


class TradeError(Exception):
    """A trade that cannot be placed; the message says why."""


def _value(field):
    return getattr(field, "value", field)


def _iso(moment):
    if moment is None:
        return None
    if isinstance(moment, str):
        return moment
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def risk_settings():
    return {
        "account": float(os.getenv("PAPER_ACCOUNT_BALANCE", "10000")),
        "risk_pct": float(os.getenv("RISK_PER_TRADE_PCT", "1")),
        "atr_multiplier": float(os.getenv("ATR_MULTIPLIER", "1.5")),
        "reward_risk": float(os.getenv("REWARD_RISK_RATIO", "2")),
    }


def plan_order(price, atr, settings):
    """Shares, stop and target for a buy at `price`."""
    if price <= 0 or atr <= 0:
        raise TradeError("Price and ATR must be greater than 0.")
    stop_distance = atr * settings["atr_multiplier"]
    stop = round(price - stop_distance, 2)
    if stop <= 0:
        raise TradeError("Calculated stop is not above $0.")
    target = round(price + stop_distance * settings["reward_risk"], 2)
    shares = min(
        math.floor(settings["account"] * settings["risk_pct"] / 100 / stop_distance),
        math.floor(settings["account"] / price),
    )
    if shares < 1:
        raise TradeError("Position size is below 1 share.")
    return {"shares": shares, "stop": stop, "target": target}


def busy_tickers(database_path=None):
    """Tickers using a slot: open trades plus entry orders not yet filled."""
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        open_tickers = [
            row["ticker"]
            for row in connection.execute(
                "SELECT ticker FROM paper_trades WHERE status = 'OPEN'"
            )
        ]
    pending = [order["ticker"] for order in get_pending_broker_orders(database_path)]
    return open_tickers + pending


def open_trade(client, stock, strategy, entry_source, price_lookup, database_path=None):
    """Send a bracket buy for one stock from a bot list."""
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        MarketOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    ticker = str(stock["ticker"]).upper()
    if not client.get_clock().is_open:
        raise TradeError("The market is closed; orders are only sent during market hours.")

    busy = busy_tickers(database_path)
    if ticker in busy:
        raise TradeError(f"{ticker} is already held or has an order waiting.")
    slots = max_open_trades()
    if len(busy) >= slots:
        raise TradeError(f"All {slots} slots are full ({len(busy)} in use).")

    atr = float(stock.get("atr") or 0)
    price = float(price_lookup(ticker))
    plan = plan_order(price, atr, risk_settings())

    order = client.submit_order(MarketOrderRequest(
        symbol=ticker,
        qty=plan["shares"],
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=plan["target"]),
        stop_loss=StopLossRequest(stop_price=plan["stop"]),
        client_order_id=f"tb-{ticker}-{int(time.time())}",
    ))
    add_broker_order(
        order_id=order.id,
        ticker=ticker,
        shares=plan["shares"],
        atr=atr,
        stop_price=plan["stop"],
        target_price=plan["target"],
        strategy=strategy,
        entry_source=entry_source,
        setup_score=stock.get("setup_score"),
        database_path=database_path,
    )
    return {
        "ticker": ticker,
        "order_id": str(order.id),
        "strategy": strategy,
        "entry_source": entry_source,
        "price": price,
        **plan,
    }


def sessions_held(client, entry_time, today):
    """Completed market sessions from the entry day up to yesterday."""
    from alpaca.trading.requests import GetCalendarRequest

    entry_day = datetime.fromisoformat(
        str(entry_time).replace("Z", "+00:00")
    ).date()
    if entry_day >= today:
        return 0
    days = client.get_calendar(GetCalendarRequest(
        start=entry_day, end=today - timedelta(days=1)
    ))
    return len(days)


def wait_for_fill(client, order_id, tries=10, delay=1.0):
    for _ in range(tries):
        order = client.get_order_by_id(order_id)
        if _value(order.status) == "filled":
            return order
        time.sleep(delay)
    return None


def sync(client, price_lookup, database_path=None, today=None, delay=1.0):
    """Record fills, stop / target exits and 20-session exits."""
    from alpaca.trading.requests import GetOrderByIdRequest

    today = today or date.today()
    result = {"filled": [], "canceled": [], "closed": [], "errors": []}

    for pending in get_pending_broker_orders(database_path):
        try:
            order = client.get_order_by_id(pending["order_id"])
        except Exception as exc:  # network and API errors
            result["errors"].append(f"{pending['ticker']}: {exc}")
            continue
        status = _value(order.status)
        if status == "filled":
            trade = record_entry(
                pending["ticker"],
                float(order.filled_avg_price),
                pending["atr"],
                int(float(order.filled_qty)),
                pending["stop_price"],
                pending["target_price"],
                setup_score=pending["setup_score"],
                entry_time=_iso(order.filled_at),
                database_path=database_path,
                strategy=pending["strategy"],
                entry_source=pending["entry_source"],
                broker_order_id=pending["order_id"],
            )
            finish_broker_order(
                pending["order_id"], "FILLED", trade_id=trade["id"],
                database_path=database_path,
            )
            result["filled"].append(trade)
        elif status in CANCELED_STATUSES:
            finish_broker_order(
                pending["order_id"], "CANCELED", detail=status,
                database_path=database_path,
            )
            result["canceled"].append(f"{pending['ticker']} ({status})")

    with _connection(database_path) as connection:
        open_trades = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM paper_trades "
                "WHERE status = 'OPEN' AND broker_order_id IS NOT NULL"
            )
        ]

    for trade in open_trades:
        try:
            closed = _sync_open_trade(
                client, trade, price_lookup, database_path, today, delay,
                GetOrderByIdRequest,
            )
        except Exception as exc:  # network and API errors
            result["errors"].append(f"{trade['ticker']}: {exc}")
            continue
        if closed is not None:
            result["closed"].append(closed)
    return result


def _sync_open_trade(client, trade, price_lookup, database_path, today, delay, nested_request):
    order = client.get_order_by_id(
        trade["broker_order_id"], filter=nested_request(nested=True)
    )
    legs = order.legs or []
    for leg in legs:
        if _value(leg.status) == "filled":
            reason = "TARGET" if _value(leg.type) == "limit" else "STOP"
            return close_trade(
                trade["id"], float(leg.filled_avg_price), reason,
                _iso(leg.filled_at), database_path=database_path,
            )

    if sessions_held(client, trade["entry_time"], today) < MAX_HOLD_SESSIONS:
        return None
    if not client.get_clock().is_open:
        return None

    for leg in legs:
        if _value(leg.status) not in CANCELED_STATUSES | {"filled"}:
            client.cancel_order_by_id(leg.id)
    exit_order = client.close_position(trade["ticker"])
    filled = wait_for_fill(client, exit_order.id, delay=delay)
    price = (
        float(filled.filled_avg_price)
        if filled is not None
        else float(price_lookup(trade["ticker"]))
    )
    return close_trade(
        trade["id"], price, "TIME_EXIT",
        _iso(filled.filled_at if filled is not None else datetime.now(timezone.utc)),
        database_path=database_path,
    )
