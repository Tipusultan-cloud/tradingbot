"""Open and track Alpaca paper trades for the trade slots.

open_trade() sends a market buy. Shares risk 1% of PAPER_ACCOUNT_BALANCE,
with a stop at price - 1.5 ATR. Once the buy fills, two OCO sell orders
(a limit target and a stop, good till canceled) are placed at Alpaca:

  - half the shares: target 2R (REWARD_RISK_RATIO)
  - the rest: target 2.5R (SECOND_REWARD_RISK_RATIO)

When the 2R half fills, the stop of the rest moves up to the entry price.
The 2-slot limit counts open trades and orders not yet filled.

The stop and targets are worked out again from the real fill price (same
1.5 ATR distance), so a fill above the planned price still risks 1% and
the targets stay a true 2R / 2.5R. An automatic buy is skipped when the
price is more than MAX_GAP_ATR (default 0.5) ATR above the scan price:
the stock already ran and the planned entry is gone.

Protection (2026-11-03):
  - earnings_exits() sells a Bot 1 / Bot 2 trade at market the morning
    before its earnings report (report today or on the next trading day,
    EARNINGS_EXIT); an automatic buy that would have to be sold that way
    (earnings within 2 trading days of the scan) is skipped. Bot 3 rides
    through earnings on purpose.
  - At most MAX_TRADES_PER_SECTOR (default 1) open trades or waiting
    orders per sector, across all three bots, for automatic buys.
  - risk_guard.py pauses automatic buys after a bad run.

sync() brings the journal up to date with Alpaca:
  - a filled entry order becomes an open journal trade (fill price) and
    gets its two exit orders (retried on the next sync if that fails)
  - a filled 2R half is recorded and the stop of the rest is moved
  - a filled rest (target or stop) closes the trade (TARGET / STOP, with
    a PARTIAL+ prefix when the half was sold first)
  - a trade held 20 sessions is sold at market (TIME_EXIT), as in the
    backtests

Bot 3 (MOMENTUM) trades ride instead: once the buy fills, one stop order
(good till canceled) is placed 1.5 ATR below the entry, with no target and
no time limit. update_trailing_stops() raises it after each completed
day to 3 ATR below the highest close since the entry (never lowers it).
Bot 3 uses its own MOMENTUM_SLOTS; Bot 1 and Bot 2 share MAX_OPEN_TRADES.

Trades opened before 2026-10-31 hold one bracket order (stop + 2R target
for all shares); they are still tracked through its legs.

sell_trade() is the dashboard Sell button: it cancels the exit orders
and sells at market (MANUAL_SELL).

The Alpaca client and price lookup are passed in so tests can use fakes.
"""

import math
import os
import time
from datetime import date, datetime, timedelta, timezone

from paper_journal import (
    _connection,
    add_broker_order,
    held_sectors,
    close_trade,
    finish_broker_order,
    get_pending_broker_orders,
    initialize_journal,
    record_entry,
    record_partial_exit,
    update_trade_orders,
)
from slot_selector import (
    SOURCE_MOMENTUM,
    busy_by_bot,
    max_open_trades,
    max_trades_per_sector,
    momentum_slots,
)

MAX_HOLD_SESSIONS = 20
# Sell when earnings are today or on the next trading day.
EARNINGS_EXIT_DAYS = 1
TRAIL_ATR_MULTIPLIER = 3.0
SOURCE_AUTO = "AUTO"
SOURCE_MANUAL = "MANUAL"
CANCELED_STATUSES = {"canceled", "expired", "rejected", "suspended", "stopped"}
# Order statuses that will not fill any more.
FINAL_STATUSES = CANCELED_STATUSES | {"filled", "replaced"}


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
        "second_reward_risk": float(os.getenv("SECOND_REWARD_RISK_RATIO", "2.5")),
    }


def max_gap_atr():
    return float(os.getenv("MAX_GAP_ATR", "0.5"))


def entry_reason(stock, strategy, entry_source=None):
    """Why the bot picked the stock, saved as the trade's entry note."""
    def pct(key):
        value = stock.get(key)
        return None if value is None else f"{float(value):+.1f}%"

    if strategy == SOURCE_MOMENTUM:
        parts = ["Bot 3 momentum jump", pct("jump_pct") and f"{pct('jump_pct')} on the day"]
        if stock.get("volume_ratio") is not None:
            parts.append(f"volume {float(stock['volume_ratio']):.1f}x normal")
    elif strategy == "PULLBACK":
        parts = [
            "Bot 2 leader pullback",
            pct("return_6m") and f"6-month {pct('return_6m')}",
            pct("return_5d") and f"5-day {pct('return_5d')}",
        ]
    else:
        parts = [
            "Bot 1 A+ setup",
            stock.get("setup_type"),
            stock.get("setup_score") is not None and f"score {float(stock['setup_score']):g}",
        ]
    if stock.get("sector"):
        parts.append(str(stock["sector"]))
    if entry_source == SOURCE_MANUAL:
        parts.append("bought by hand")
    # The warnings on the list when it was bought (earnings, news), so the
    # lesson can be read against them later.
    labels = [
        str(item.get("label")) for item in stock.get("warnings") or []
        if isinstance(item, dict) and item.get("label")
    ]
    parts.append(f"warnings: {', '.join(labels)}" if labels else "no warnings")
    return " · ".join(str(part) for part in parts if part)


def exit_levels(price, atr, settings):
    """Stop and the two targets for an entry at `price`."""
    if price <= 0 or atr <= 0:
        raise TradeError("Price and ATR must be greater than 0.")
    stop_distance = atr * settings["atr_multiplier"]
    stop = round(price - stop_distance, 2)
    if stop <= 0:
        raise TradeError("Calculated stop is not above $0.")
    return {
        "stop": stop,
        "target": round(price + stop_distance * settings["reward_risk"], 2),
        "target2": round(price + stop_distance * settings["second_reward_risk"], 2),
    }


def plan_order(price, atr, settings):
    """Shares, stop and the two targets for a buy at `price`."""
    levels = exit_levels(price, atr, settings)
    stop_distance = atr * settings["atr_multiplier"]
    shares = min(
        math.floor(settings["account"] * settings["risk_pct"] / 100 / stop_distance),
        math.floor(settings["account"] / price),
    )
    if shares < 1:
        raise TradeError("Position size is below 1 share.")
    return {"shares": shares, **levels}


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


def busy_split(database_path=None):
    """Busy tickers split into (Bot 1 / Bot 2 slots, Bot 3 slots)."""
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        open_trades = [
            dict(row)
            for row in connection.execute(
                "SELECT ticker, strategy FROM paper_trades WHERE status = 'OPEN'"
            )
        ]
    return busy_by_bot(open_trades, get_pending_broker_orders(database_path))


def open_trade(client, stock, strategy, entry_source, price_lookup, database_path=None):
    """Send a market buy for one stock from a bot list."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    ticker = str(stock["ticker"]).upper()
    if not client.get_clock().is_open:
        raise TradeError("The market is closed; orders are only sent during market hours.")

    main_busy, momentum_busy = busy_split(database_path)
    if ticker in main_busy + momentum_busy:
        raise TradeError(f"{ticker} is already held or has an order waiting.")
    if strategy == SOURCE_MOMENTUM:
        slots, busy, label = momentum_slots(), momentum_busy, "Bot 3"
    else:
        slots, busy, label = max_open_trades(), main_busy, "Bot 1 / Bot 2"
    if len(busy) >= slots:
        raise TradeError(f"All {slots} {label} slots are full ({len(busy)} in use).")

    atr = float(stock.get("atr") or 0)
    price = float(price_lookup(ticker))
    planned = float(stock.get("price") or 0)
    if entry_source == SOURCE_AUTO and planned > 0 and atr > 0:
        gap = price - planned
        if gap > max_gap_atr() * atr:
            raise TradeError(
                f"price ${price:.2f} is ${gap:.2f} above the scan price "
                f"${planned:.2f} (more than {max_gap_atr():g} ATR); the "
                "planned entry is gone."
            )
    if entry_source == SOURCE_AUTO:
        check_auto_protection(ticker, stock, strategy, database_path)
    plan = plan_order(price, atr, risk_settings())
    if strategy == SOURCE_MOMENTUM:
        # Rides with a trailing stop; the 2R target is only for reference.
        plan["target2"] = None

    order = client.submit_order(MarketOrderRequest(
        symbol=ticker,
        qty=plan["shares"],
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        client_order_id=f"tb-{ticker}-{int(time.time())}",
    ))
    add_broker_order(
        order_id=order.id,
        ticker=ticker,
        shares=plan["shares"],
        atr=atr,
        stop_price=plan["stop"],
        target_price=plan["target"],
        target2_price=plan["target2"],
        strategy=strategy,
        entry_source=entry_source,
        setup_score=stock.get("setup_score"),
        database_path=database_path,
        sector=stock.get("sector"),
        entry_note=entry_reason(stock, strategy, entry_source),
    )
    return {
        "ticker": ticker,
        "order_id": str(order.id),
        "strategy": strategy,
        "entry_source": entry_source,
        "price": price,
        **plan,
    }


def check_auto_protection(ticker, stock, strategy, database_path):
    """Earnings and sector rules for automatic buys (TradeError if not)."""
    days = (stock.get("earnings") or {}).get("trading_days_until")
    if strategy != SOURCE_MOMENTUM and days is not None and days <= EARNINGS_EXIT_DAYS + 1:
        raise TradeError(
            f"earnings in {days} trading day(s); it would be sold before the "
            "report right away."
        )
    sector = stock.get("sector")
    limit = max_trades_per_sector()
    if sector and limit > 0:
        same = held_sectors(database_path).get(sector, [])
        if len(same) >= limit:
            raise TradeError(
                f"{sector} already has {', '.join(same)} (max {limit} per sector)."
            )


def _submit_oco(client, ticker, shares, target, stop):
    """One sell order pair at Alpaca: limit at `target`, stop at `stop`."""
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    return client.submit_order(LimitOrderRequest(
        symbol=ticker,
        qty=shares,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.OCO,
        limit_price=target,
        take_profit=TakeProfitRequest(limit_price=target),
        stop_loss=StopLossRequest(stop_price=stop),
    ))


def place_exit_orders(client, trade, database_path=None):
    """Send the exit orders a filled split-exit trade is still missing.

    Half the shares sell at the 2R target, the rest at target2. A 1-share
    trade has no half and sells at target2. Each order id is saved as soon
    as it is sent, so a retry never sends one twice.
    """
    half_shares = trade["shares"] // 2
    if half_shares and not trade["half_order_id"]:
        order = _submit_oco(
            client, trade["ticker"], half_shares,
            trade["target_price"], trade["stop_price"],
        )
        trade = update_trade_orders(
            trade["id"], half_order_id=str(order.id), database_path=database_path
        )
    if not trade["rest_order_id"]:
        order = _submit_oco(
            client, trade["ticker"], trade["shares"] - half_shares,
            trade["target2_price"], trade["stop_price"],
        )
        trade = update_trade_orders(
            trade["id"], rest_order_id=str(order.id), database_path=database_path
        )
    return trade


def place_momentum_stop(client, trade, database_path=None):
    """The single stop order of a filled Bot 3 trade (no target)."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import StopOrderRequest

    if trade["rest_order_id"]:
        return trade
    order = client.submit_order(StopOrderRequest(
        symbol=trade["ticker"],
        qty=trade["shares"],
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=trade["stop_price"],
    ))
    return update_trade_orders(
        trade["id"], rest_order_id=str(order.id),
        active_stop_price=trade["stop_price"], database_path=database_path,
    )


def trailing_stop(entry_price, closes, atr, current_stop):
    """3 ATR below the highest close since the entry; never lower than now."""
    highest = max([entry_price, *closes])
    return round(max(current_stop, highest - TRAIL_ATR_MULTIPLIER * atr), 2)


def update_trailing_stops(client, fetch_bars, database_path=None):
    """Raise each Bot 3 stop order after completed daily bars.

    `fetch_bars(ticker, entry_date)` returns completed daily bars with
    Close and ATR (shadow_exits.fetch_daily_bars). Returns messages.
    """
    from alpaca.trading.requests import ReplaceOrderRequest

    from shadow_exits import entry_session_date

    initialize_journal(database_path)
    with _connection(database_path) as connection:
        trades = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM paper_trades WHERE status = 'OPEN' "
                "AND strategy = ? AND rest_order_id IS NOT NULL",
                (SOURCE_MOMENTUM,),
            )
        ]
    messages = []
    for trade in trades:
        try:
            entry_date = entry_session_date(trade["entry_time"])
            bars = fetch_bars(trade["ticker"], entry_date)
            held = bars[bars.index.date >= entry_date]
            if held.empty or held["ATR"].isna().iloc[-1]:
                continue
            current = trade["active_stop_price"] or trade["stop_price"]
            new_stop = trailing_stop(
                trade["entry_price"], held["Close"].tolist(),
                float(held["ATR"].iloc[-1]), current,
            )
            if new_stop <= current:
                continue
            order = client.replace_order_by_id(
                trade["rest_order_id"], ReplaceOrderRequest(stop_price=new_stop)
            )
            update_trade_orders(
                trade["id"], rest_order_id=str(order.id),
                active_stop_price=new_stop, database_path=database_path,
            )
            messages.append(
                f"TRAIL {trade['ticker']}: stop ${current:.2f} -> ${new_stop:.2f}"
            )
        except Exception as exc:  # price lookups and Alpaca errors
            messages.append(f"Trailing stop not updated for {trade['ticker']}: {exc}")
    return messages


def _check_momentum_exit(client, trade, database_path):
    """(closed trade or None, [stop order]) for a Bot 3 trade."""
    if not trade["rest_order_id"]:
        trade = place_momentum_stop(client, trade, database_path)
    order = client.get_order_by_id(trade["rest_order_id"])
    if _value(order.status) == "filled":
        reason = (
            "TRAIL_STOP"
            if (trade["active_stop_price"] or 0) > trade["stop_price"]
            else "STOP"
        )
        closed = close_trade(
            trade["id"], float(order.filled_avg_price), reason,
            _iso(order.filled_at), database_path=database_path,
        )
        return closed, [order]
    return None, [order]


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
    result = {"filled": [], "canceled": [], "halves": [], "closed": [], "errors": []}

    for pending in get_pending_broker_orders(database_path):
        try:
            order = client.get_order_by_id(pending["order_id"])
        except Exception as exc:  # network and API errors
            result["errors"].append(f"{pending['ticker']}: {exc}")
            continue
        status = _value(order.status)
        if status == "filled":
            fill = float(order.filled_avg_price)
            stop, target, target2 = (
                pending["stop_price"], pending["target_price"],
                pending["target2_price"],
            )
            # Split-exit and Bot 3 trades get their sell orders after the
            # fill, so set them from the real price (old brackets already
            # hold theirs at Alpaca).
            if target2 is not None or pending["strategy"] == SOURCE_MOMENTUM:
                try:
                    levels = exit_levels(fill, float(pending["atr"]), risk_settings())
                except (TradeError, TypeError, ValueError):
                    levels = None
                if levels:
                    stop, target = levels["stop"], levels["target"]
                    if target2 is not None:
                        target2 = levels["target2"]
            trade = record_entry(
                pending["ticker"],
                fill,
                pending["atr"],
                int(float(order.filled_qty)),
                stop,
                target,
                setup_score=pending["setup_score"],
                entry_time=_iso(order.filled_at),
                database_path=database_path,
                strategy=pending["strategy"],
                entry_source=pending["entry_source"],
                broker_order_id=pending["order_id"],
                target2_price=target2,
                sector=pending.get("sector"),
                entry_note=pending.get("entry_note"),
            )
            finish_broker_order(
                pending["order_id"], "FILLED", trade_id=trade["id"],
                database_path=database_path,
            )
            result["filled"].append(trade)
            if trade["target2_price"] is not None or trade["strategy"] == SOURCE_MOMENTUM:
                try:
                    if trade["strategy"] == SOURCE_MOMENTUM:
                        place_momentum_stop(client, trade, database_path)
                    else:
                        place_exit_orders(client, trade, database_path)
                except Exception as exc:  # retried by the next sync
                    result["errors"].append(
                        f"{trade['ticker']}: exit orders not placed yet: {exc}"
                    )
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
                GetOrderByIdRequest, result["halves"],
            )
        except Exception as exc:  # network and API errors
            result["errors"].append(f"{trade['ticker']}: {exc}")
            continue
        if closed is not None:
            result["closed"].append(closed)
    return result


def _filled_exit(order):
    """(price, time, reason) when an OCO pair or bracket leg has filled."""
    if _value(order.status) == "filled":
        return float(order.filled_avg_price), _iso(order.filled_at), "TARGET"
    for leg in order.legs or []:
        if _value(leg.status) == "filled":
            reason = "TARGET" if _value(leg.type) == "limit" else "STOP"
            return float(leg.filled_avg_price), _iso(leg.filled_at), reason
    return None


def _move_stop(client, order, stop_price):
    """Move the stop of an OCO pair (its stop leg) to `stop_price`."""
    from alpaca.trading.requests import ReplaceOrderRequest

    for leg in order.legs or []:
        if _value(leg.type) == "stop" and _value(leg.status) not in FINAL_STATUSES:
            client.replace_order_by_id(
                leg.id, ReplaceOrderRequest(stop_price=stop_price)
            )


def _check_split_exits(client, trade, database_path, nested_request, halves=None):
    """Record what filled for a split-exit trade.

    Returns (closed trade or None, the trade's Alpaca exit orders).
    """
    if not trade["rest_order_id"]:
        trade = place_exit_orders(client, trade, database_path)

    def fetch(order_id):
        return client.get_order_by_id(order_id, filter=nested_request(nested=True))

    half = fetch(trade["half_order_id"]) if trade["half_order_id"] else None
    rest = fetch(trade["rest_order_id"])
    orders = [order for order in (half, rest) if order is not None]

    half_fill = _filled_exit(half) if half is not None else None
    if (
        half_fill
        and half_fill[2] == "TARGET"
        and trade["partial_exit_price"] is None
    ):
        recorded = record_partial_exit(
            trade["id"], trade["shares"] // 2, half_fill[0], half_fill[1],
            database_path=database_path,
        )
        if recorded is not None:
            trade = recorded
            if halves is not None:
                halves.append(recorded)

    if trade["partial_exit_price"] is not None and trade["active_stop_price"] is None:
        breakeven = round(trade["entry_price"], 2)
        _move_stop(client, rest, breakeven)
        trade = update_trade_orders(
            trade["id"], active_stop_price=breakeven, database_path=database_path
        )

    rest_fill = _filled_exit(rest)
    if rest_fill is None:
        return None, orders
    price, filled_at, reason = rest_fill
    if trade["partial_exit_price"] is None and half is not None:
        # Both parts share the first stop; wait until the half's stop fills too.
        if half_fill is None:
            return None, orders
        half_shares = trade["shares"] // 2
        price = (
            half_fill[0] * half_shares + price * (trade["shares"] - half_shares)
        ) / trade["shares"]
    closed = close_trade(
        trade["id"], price, reason, filled_at, database_path=database_path
    )
    return closed, orders


def _sync_open_trade(
    client, trade, price_lookup, database_path, today, delay, nested_request,
    halves=None,
):
    if trade["strategy"] == SOURCE_MOMENTUM:
        # Rides until the trailing stop fills: no time exit.
        closed, _ = _check_momentum_exit(client, trade, database_path)
        return closed
    if trade["target2_price"] is not None:
        closed, orders = _check_split_exits(
            client, trade, database_path, nested_request, halves
        )
        if closed is not None:
            return closed
    else:
        order = client.get_order_by_id(
            trade["broker_order_id"], filter=nested_request(nested=True)
        )
        orders = order.legs or []
        for leg in orders:
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
    return _sell_at_market(
        client, trade, orders, "TIME_EXIT", price_lookup, database_path, delay
    )


def _sell_at_market(client, trade, orders, reason, price_lookup, database_path, delay):
    """Cancel the exit orders, sell the position, close the trade."""
    for order in orders:
        if _value(order.status) not in FINAL_STATUSES:
            client.cancel_order_by_id(order.id)
    exit_order = client.close_position(trade["ticker"])
    filled = wait_for_fill(client, exit_order.id, delay=delay)
    price = (
        float(filled.filled_avg_price)
        if filled is not None
        else float(price_lookup(trade["ticker"]))
    )
    return close_trade(
        trade["id"], price, reason,
        _iso(filled.filled_at if filled is not None else datetime.now(timezone.utc)),
        database_path=database_path,
    )


def sell_trade(
    client, ticker, price_lookup, database_path=None, delay=1.0, reason="MANUAL_SELL"
):
    """Sell an open Alpaca paper trade now, at market (dashboard Sell)."""
    from alpaca.trading.requests import GetOrderByIdRequest

    initialize_journal(database_path)
    with _connection(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM paper_trades WHERE ticker = ? AND status = 'OPEN' "
            "ORDER BY id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    if row is None:
        raise TradeError(f"{ticker} has no open paper trade")
    trade = dict(row)
    if not trade.get("broker_order_id"):
        raise TradeError(f"{ticker} is a journal-only trade with no Alpaca position")
    if not client.get_clock().is_open:
        raise TradeError("The market is closed; sell when it opens")

    # A stop or target may have filled since the last sync.
    if trade["strategy"] == SOURCE_MOMENTUM:
        closed, orders = _check_momentum_exit(client, trade, database_path)
        if closed is not None:
            raise TradeError(f"{ticker} already closed at its stop")
    elif trade["target2_price"] is not None:
        closed, orders = _check_split_exits(
            client, trade, database_path, GetOrderByIdRequest
        )
        if closed is not None:
            reason = closed["exit_reason"].removeprefix("PARTIAL+")
            raise TradeError(f"{ticker} already closed at its {reason.lower()}")
    else:
        order = client.get_order_by_id(
            trade["broker_order_id"], filter=GetOrderByIdRequest(nested=True)
        )
        orders = order.legs or []
        for leg in orders:
            if _value(leg.status) == "filled":
                reason = "TARGET" if _value(leg.type) == "limit" else "STOP"
                close_trade(
                    trade["id"], float(leg.filled_avg_price), reason,
                    _iso(leg.filled_at), database_path=database_path,
                )
                raise TradeError(f"{ticker} already closed at its {reason.lower()}")
    return _sell_at_market(
        client, trade, orders, reason, price_lookup, database_path, delay
    )


def earnings_exits(client, price_lookup, check, database_path=None, delay=1.0):
    """Sell Bot 1 / Bot 2 trades whose earnings are today or next session.

    `check(ticker)` is earnings_calendar.check_earnings. Returns messages.
    """
    if not client.get_clock().is_open:
        return []
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        trades = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM paper_trades WHERE status = 'OPEN' "
                "AND broker_order_id IS NOT NULL"
            )
        ]
    messages = []
    for trade in trades:
        if trade["strategy"] == SOURCE_MOMENTUM:
            continue
        earnings = check(trade["ticker"])
        days = earnings.get("trading_days_until")
        if days is None or days > EARNINGS_EXIT_DAYS:
            continue
        try:
            closed = sell_trade(
                client, trade["ticker"], price_lookup, database_path, delay,
                reason="EARNINGS_EXIT",
            )
        except TradeError as exc:
            messages.append(f"Earnings exit {trade['ticker']} skipped: {exc}")
            continue
        except Exception as exc:  # Alpaca errors: retried next run
            messages.append(f"Earnings exit {trade['ticker']} failed, will retry: {exc}")
            continue
        messages.append(
            f"SOLD BEFORE EARNINGS {trade['ticker']} "
            f"(report {earnings.get('next_earnings_date')}): "
            f"{closed['shares']} @ ${closed['exit_price']:.2f}, "
            f"P&L ${closed['realized_pnl']:+.2f}"
        )
    return messages
