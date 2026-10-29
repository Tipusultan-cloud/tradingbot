"""Fill free paper-trade slots: the A+ pick first, then leader pullbacks.

Runs after master_selector.py and scanner_pullback.py. Each open paper
trade uses one slot; MAX_OPEN_TRADES in .env sets the number of slots
(default 2, the setting chosen in backtests/backtest_pullback_slots.py).
Writes data/slot_selection.json with the slot picks plus the full list
from each bot (Bot 1 = A+, Bot 2 = leader pullback), so stocks can also
be picked by hand. The webhook approves a pullback BUY for any stock in
the Bot 2 list. Nothing is ordered automatically.

Earnings and news are shown as warnings for pullbacks; neither was part
of the pullback backtest, so neither blocks a pullback.
"""

import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from earnings_calendar import check_earnings
from master_selector import build_risk_preview, parse_timestamp
from news_checker import check_news
from paper_journal import get_paper_trades, get_pending_broker_orders
from project_paths import (
    MASTER_CANDIDATES_JSON,
    PULLBACK_SETUPS_JSON,
    SLOT_SELECTION_JSON,
)

DEFAULT_MAX_OPEN_TRADES = 2
MAX_SOURCE_AGE_HOURS = 36

SOURCE_A_PLUS = "A_PLUS"
SOURCE_PULLBACK = "PULLBACK"


def max_open_trades():
    value = os.getenv("MAX_OPEN_TRADES", str(DEFAULT_MAX_OPEN_TRADES))
    try:
        slots = int(value)
    except ValueError as exc:
        raise RuntimeError("MAX_OPEN_TRADES must be a whole number") from exc
    if slots < 1:
        raise RuntimeError("MAX_OPEN_TRADES must be at least 1")
    return slots


def load_fresh(path, label, warnings, now=None):
    """Payload if the file exists and is recent, else None plus a warning."""
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except FileNotFoundError:
        warnings.append(f"{label} results do not exist.")
        return None
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"{label} results could not be read: {exc}")
        return None

    if not isinstance(payload, dict):
        warnings.append(f"{label} results must contain a JSON object.")
        return None

    try:
        generated = parse_timestamp(payload.get("generated_at"))
    except (AttributeError, ValueError):
        generated = None
    if generated is None:
        warnings.append(f"{label} results have no valid generated_at.")
        return None

    age = ((now or datetime.now(timezone.utc)) - generated).total_seconds() / 3600
    if age > MAX_SOURCE_AGE_HOURS:
        warnings.append(f"{label} results are stale: {age:.1f} hours old.")
        return None
    return payload


def build_slot_selection(master, pullbacks, open_tickers, max_slots):
    """Pick stocks for the free slots, A+ first, skipping stocks held."""
    candidates = []
    best = (master or {}).get("best_current")
    if isinstance(best, dict) and best.get("ticker"):
        candidates.append((SOURCE_A_PLUS, best))
    for setup in (pullbacks or {}).get("qualified", []):
        if isinstance(setup, dict) and setup.get("ticker"):
            candidates.append((SOURCE_PULLBACK, setup))

    held = {str(ticker).upper() for ticker in open_tickers}
    free_slots = max(max_slots - len(held), 0)

    selections = []
    for source, stock in candidates:
        if len(selections) >= free_slots:
            break
        ticker = str(stock["ticker"]).upper()
        if ticker in held:
            continue
        held.add(ticker)
        selections.append({
            **stock,
            "ticker": ticker,
            "source": source,
            "slot": len(open_tickers) + len(selections) + 1,
            "risk_preview": build_risk_preview(
                price=float(stock.get("price") or 0),
                atr=float(stock.get("atr") or 0),
            ),
        })

    return {
        "max_slots": max_slots,
        "open_positions": sorted({str(t).upper() for t in open_tickers}),
        "free_slots": free_slots,
        "selections": selections,
    }


def build_bot_lists(master, pullbacks, open_tickers, selections):
    """Every current stock from each bot, for picking by hand.

    Bot 1 is every A+ approved stock, Bot 2 every leader pullback, each
    in its own rank order with a risk preview. Stocks already held or
    picked for a slot are flagged.
    """
    held = {str(ticker).upper() for ticker in open_tickers}
    picked = {stock["ticker"] for stock in selections}
    sources = {
        SOURCE_A_PLUS: (master or {}).get("approved", []),
        SOURCE_PULLBACK: (pullbacks or {}).get("qualified", []),
    }

    lists = {}
    for source, stocks in sources.items():
        rows = []
        for rank, stock in enumerate(stocks or [], start=1):
            if not isinstance(stock, dict) or not stock.get("ticker"):
                continue
            ticker = str(stock["ticker"]).upper()
            rows.append({
                **stock,
                "ticker": ticker,
                "source": source,
                "rank": rank,
                "held": ticker in held,
                "slot_pick": ticker in picked,
                "risk_preview": stock.get("risk_preview") or build_risk_preview(
                    price=float(stock.get("price") or 0),
                    atr=float(stock.get("atr") or 0),
                ),
            })
        lists[source] = rows
    return lists


def print_bot_list(title, rows):
    print()
    print(f"{title}: {len(rows)} stock(s)")
    if not rows:
        print("  none today")
    for stock in rows:
        risk = stock.get("risk_preview") or {}
        if stock["source"] == SOURCE_PULLBACK:
            detail = (
                f"6M {stock.get('return_6m', 0):+.1f}% "
                f"5D {stock.get('return_5d', 0):+.1f}%"
            )
        else:
            detail = f"setup {stock.get('setup_score', '?')}/100"
        flags = (
            " [HELD]" if stock["held"]
            else " [SLOT PICK]" if stock["slot_pick"]
            else ""
        )
        print(
            f"  #{stock['rank']} {stock['ticker']:6s} {detail} "
            f"| entry ~${risk.get('entry_reference', '?')} "
            f"stop ${risk.get('stop', '?')} "
            f"target ${risk.get('target', '?')} "
            f"shares {risk.get('shares', '?')}{flags}"
        )


def main():
    load_dotenv()

    print()
    print("================================")
    print("SLOT SELECTOR (A+ FIRST, THEN PULLBACK)")
    print("================================")

    warnings = []
    master = load_fresh(MASTER_CANDIDATES_JSON, "Master selection", warnings)
    pullbacks = load_fresh(PULLBACK_SETUPS_JSON, "Pullback scan", warnings)
    # Alpaca orders waiting to fill hold a slot too.
    open_tickers = [
        trade["ticker"] for trade in get_paper_trades(limit=1)["open"]
    ] + [order["ticker"] for order in get_pending_broker_orders()]

    selection = build_slot_selection(
        master=master,
        pullbacks=pullbacks,
        open_tickers=open_tickers,
        max_slots=max_open_trades(),
    )

    # A+ picks already carry these checks from master_selector.py.
    for stock in selection["selections"]:
        if stock["source"] == SOURCE_PULLBACK:
            stock["earnings"] = check_earnings(stock["ticker"])
            stock["news"] = check_news(stock["ticker"])

    if selection["selections"]:
        status = "SLOTS TO FILL"
    elif selection["free_slots"] == 0:
        status = "ALL SLOTS FULL"
    else:
        status = "WAIT"

    bots = build_bot_lists(
        master=master,
        pullbacks=pullbacks,
        open_tickers=open_tickers,
        selections=selection["selections"],
    )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "A_PLUS_THEN_LEADER_PULLBACK",
        "status": status,
        **selection,
        "bots": bots,
        "warnings": warnings,
    }
    with SLOT_SELECTION_JSON.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)

    print()
    print(
        f"Slots: {len(selection['open_positions'])} open of "
        f"{selection['max_slots']} - {selection['free_slots']} free"
    )
    for warning in warnings:
        print("WARNING:", warning)
    print()
    print("STATUS:", status)
    for stock in selection["selections"]:
        risk = stock.get("risk_preview") or {}
        print(
            f"Slot {stock['slot']}: {stock['ticker']} ({stock['source']}) "
            f"| entry ~${risk.get('entry_reference', '—')} "
            f"| stop ${risk.get('stop', '—')} "
            f"| target ${risk.get('target', '—')}"
        )
    print_bot_list("BOT 1 - A+ (buy strength)", bots[SOURCE_A_PLUS])
    print_bot_list("BOT 2 - LEADER PULLBACK (buy the dip)", bots[SOURCE_PULLBACK])
    print()
    print("Any stock in either list can be bought with a BUY alert while a slot is free.")
    print("Saved:", SLOT_SELECTION_JSON)


if __name__ == "__main__":
    main()
