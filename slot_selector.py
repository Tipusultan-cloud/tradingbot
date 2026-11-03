"""Fill free paper-trade slots: the A+ pick first, then leader pullbacks;
Bot 3 (momentum) fills its own slot.

Runs after master_selector.py, scanner_pullback.py and scanner_momentum.py.
Each open paper trade uses one slot. Bot 1 and Bot 2 share MAX_OPEN_TRADES
slots (default 2, the setting chosen in backtests/backtest_pullback_slots.py);
Bot 3 has MOMENTUM_SLOTS of its own (default 1), so it never takes their
place. Writes data/slot_selection.json with the slot picks plus the full
list from each bot (Bot 1 = A+, Bot 2 = leader pullback, Bot 3 = momentum),
so stocks can also be picked by hand. The webhook approves a pullback BUY
for any stock in the Bot 2 list. auto_trader.py buys the slot picks.

Earnings and news are shown as warnings for pullbacks; neither was part
of the pullback backtest, so neither blocks a pullback. Every listed
stock gets warnings (earnings within 2 weeks, bad news or bad earnings,
a drop right after earnings); the dashboard crowns the top stock with
none.
"""

import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from earnings_calendar import check_earnings
from master_selector import build_risk_preview, parse_timestamp
from news_checker import check_news
from paper_journal import get_paper_trades, get_pending_broker_orders
from market_clock import is_current, stale_message
from project_paths import (
    MASTER_CANDIDATES_JSON,
    MOMENTUM_SETUPS_JSON,
    PULLBACK_SETUPS_JSON,
    SLOT_SELECTION_JSON,
)

DEFAULT_MAX_OPEN_TRADES = 2
DEFAULT_MOMENTUM_SLOTS = 1

SOURCE_A_PLUS = "A_PLUS"
SOURCE_PULLBACK = "PULLBACK"
SOURCE_MOMENTUM = "MOMENTUM"

# A pullback is a >5% drop over 5 sessions; earnings inside that window
# probably caused it.
RECENT_EARNINGS_TRADING_DAYS = 5
BAD_EARNINGS_CATEGORIES = {"earnings_miss", "guidance_cut"}


def max_open_trades():
    value = os.getenv("MAX_OPEN_TRADES", str(DEFAULT_MAX_OPEN_TRADES))
    try:
        slots = int(value)
    except ValueError as exc:
        raise RuntimeError("MAX_OPEN_TRADES must be a whole number") from exc
    if slots < 1:
        raise RuntimeError("MAX_OPEN_TRADES must be at least 1")
    return slots


def momentum_slots():
    """Bot 3's own slots (0 turns Bot 3 buying off)."""
    value = os.getenv("MOMENTUM_SLOTS", str(DEFAULT_MOMENTUM_SLOTS))
    try:
        slots = int(value)
    except ValueError as exc:
        raise RuntimeError("MOMENTUM_SLOTS must be a whole number") from exc
    return max(slots, 0)


def busy_by_bot(open_trades, pending_orders):
    """Tickers using a slot, split into (Bot 1 / Bot 2 slots, Bot 3 slots)."""
    main, momentum = [], []
    for item in [*open_trades, *pending_orders]:
        ticker = str(item["ticker"]).upper()
        (momentum if item.get("strategy") == SOURCE_MOMENTUM else main).append(ticker)
    return main, momentum


def build_momentum_selection(momentum, held_tickers, busy_count, slots):
    """Picks for Bot 3's free slots, biggest volume surge first."""
    held = {str(ticker).upper() for ticker in held_tickers}
    free = max(slots - busy_count, 0)
    picks = []
    for setup in (momentum or {}).get("qualified", []):
        if len(picks) >= free:
            break
        if not isinstance(setup, dict) or not setup.get("ticker"):
            continue
        ticker = str(setup["ticker"]).upper()
        if ticker in held:
            continue
        held.add(ticker)
        picks.append({
            **setup,
            "ticker": ticker,
            "source": SOURCE_MOMENTUM,
            "slot": f"Bot 3 #{busy_count + len(picks) + 1}",
            "risk_preview": build_risk_preview(
                price=float(setup.get("price") or 0),
                atr=float(setup.get("atr") or 0),
            ),
        })
    return {"momentum_slots": slots, "momentum_free": free, "momentum_selections": picks}

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

    if not is_current(generated, now):
        warnings.append(stale_message(label, generated, now) + ".")
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


def build_bot_lists(master, pullbacks, open_tickers, selections, momentum=None):
    """Every current stock from each bot, for picking by hand.

    Bot 1 is every A+ approved stock, Bot 2 every leader pullback, Bot 3
    every momentum jump, each in its own rank order with a risk preview.
    Stocks already held or picked for a slot are flagged.
    """
    held = {str(ticker).upper() for ticker in open_tickers}
    picked = {stock["ticker"] for stock in selections}
    sources = {
        SOURCE_A_PLUS: (master or {}).get("approved", []),
        SOURCE_PULLBACK: (pullbacks or {}).get("qualified", []),
        SOURCE_MOMENTUM: (momentum or {}).get("qualified", []),
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


def stock_warnings(stock):
    """Warnings shown next to a listed stock; none means it is clean.

    Warnings only: none of them was part of the pullback backtest, so
    they never stop a stock being picked or bought.
    """
    warnings = []
    earnings = stock.get("earnings") or {}
    news = stock.get("news") or {}

    if earnings.get("status") == "EARNINGS_SOON":
        warnings.append({
            "code": "EARNINGS_SOON",
            "label": f"Earnings in {earnings.get('trading_days_until')}d",
            "detail": earnings.get("detail", ""),
        })
    elif earnings.get("status") != "CLEAR":
        warnings.append({
            "code": "EARNINGS_UNKNOWN",
            "label": "Earnings not checked",
            "detail": earnings.get("detail") or "Earnings lookup unavailable.",
        })

    days_since = earnings.get("trading_days_since")
    if (
        stock.get("source") == SOURCE_PULLBACK
        and days_since is not None
        and days_since <= RECENT_EARNINGS_TRADING_DAYS
    ):
        warnings.append({
            "code": "FELL_AFTER_EARNINGS",
            "label": "Fell after earnings",
            "detail": (
                f"Reported earnings on {earnings.get('last_earnings_date')}; "
                "this week's drop may be the market's reaction."
            ),
        })

    if news.get("status") == "BAD_NEWS":
        categories = {
            item.get("category") for item in news.get("bad_headlines") or []
        }
        bad_earnings = bool(categories & BAD_EARNINGS_CATEGORIES)
        warnings.append({
            "code": "BAD_EARNINGS" if bad_earnings else "BAD_NEWS",
            "label": "Bad earnings" if bad_earnings else "Bad news",
            "detail": news.get("detail", ""),
        })
    elif news.get("status") != "CLEAR":
        warnings.append({
            "code": "NEWS_UNKNOWN",
            "label": "News not checked",
            "detail": news.get("detail") or "News lookup unavailable.",
        })
    return warnings


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
        elif stock["source"] == SOURCE_MOMENTUM:
            detail = (
                f"+{stock.get('jump_pct', 0):.1f}% today "
                f"volume {stock.get('volume_ratio', 0):.1f}x"
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
        for warning in stock.get("warnings") or []:
            print(f"      ! {warning['label']}: {warning['detail']}")


def main():
    load_dotenv()

    print()
    print("================================")
    print("SLOT SELECTOR (A+ FIRST, THEN PULLBACK; BOT 3 OWN SLOT)")
    print("================================")

    warnings = []
    master = load_fresh(MASTER_CANDIDATES_JSON, "Master selection", warnings)
    pullbacks = load_fresh(PULLBACK_SETUPS_JSON, "Pullback scan", warnings)
    momentum = load_fresh(MOMENTUM_SETUPS_JSON, "Momentum scan", warnings)
    # Alpaca orders waiting to fill hold a slot too.
    main_busy, momentum_busy = busy_by_bot(
        get_paper_trades(limit=1)["open"], get_pending_broker_orders()
    )
    open_tickers = main_busy + momentum_busy

    selection = build_slot_selection(
        master=master,
        pullbacks=pullbacks,
        open_tickers=main_busy,
        max_slots=max_open_trades(),
    )
    momentum_selection = build_momentum_selection(
        momentum,
        held_tickers=open_tickers + [s["ticker"] for s in selection["selections"]],
        busy_count=len(momentum_busy),
        slots=momentum_slots(),
    )
    selection["open_positions"] = sorted(set(open_tickers))
    selection["selections"] += momentum_selection["momentum_selections"]

    bots = build_bot_lists(
        master=master,
        pullbacks=pullbacks,
        open_tickers=open_tickers,
        selections=selection["selections"],
        momentum=momentum,
    )

    # A+ stocks already carry these checks from master_selector.py.
    # One lookup per ticker, shared by the slot picks and the lists.
    checks = {}
    for stock in [*selection["selections"], *bots[SOURCE_A_PLUS], *bots[SOURCE_PULLBACK], *bots[SOURCE_MOMENTUM]]:
        ticker = stock["ticker"]
        if "earnings" in stock and "news" in stock:
            checks.setdefault(ticker, (stock["earnings"], stock["news"]))
        if ticker not in checks:
            checks[ticker] = (check_earnings(ticker), check_news(ticker))
        stock["earnings"], stock["news"] = checks[ticker]
        stock["warnings"] = stock_warnings(stock)

    if selection["selections"]:
        status = "SLOTS TO FILL"
    elif selection["free_slots"] == 0:
        status = "ALL SLOTS FULL"
    else:
        status = "WAIT"

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "A_PLUS_THEN_LEADER_PULLBACK_PLUS_MOMENTUM",
        "status": status,
        **selection,
        "momentum_slots": momentum_selection["momentum_slots"],
        "momentum_free": momentum_selection["momentum_free"],
        "momentum_open": sorted(set(momentum_busy)),
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
            + (
                "| trailing stop, no target"
                if stock["source"] == SOURCE_MOMENTUM
                else f"| target ${risk.get('target', '—')}"
            )
        )
    print_bot_list("BOT 1 - A+ (buy strength)", bots[SOURCE_A_PLUS])
    print_bot_list("BOT 2 - LEADER PULLBACK (buy the dip)", bots[SOURCE_PULLBACK])
    print_bot_list("BOT 3 - MOMENTUM (ride the jump)", bots[SOURCE_MOMENTUM])
    print()
    print(
        f"Bot 3 slots: {len(momentum_busy)} in use of "
        f"{momentum_selection['momentum_slots']}"
    )
    print("Any stock in the lists can be bought from the dashboard while its bot has a free slot.")
    print("Saved:", SLOT_SELECTION_JSON)


if __name__ == "__main__":
    main()
