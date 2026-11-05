"""The buy plan: what the bot buys by itself at the next open, and the best
stocks left for the user to buy by hand. Emailed after the evening scan
(scan_notifications.send_scan_email).

  Automatic   the slot picks of the bots in AUTO_BUY_BOTS (now Bot 1), with
              the reason when the 8:45 AM run will skip one (earnings within
              2 trading days, sector already held, buying paused)
  By hand     Bot 2 / Bot 3: the one plan pick per bot (review_list: the
              first stock with no warning, not held, sector not held), the
              others with why they are not in the plan, and the free
              slots each bot has
  Open        each open trade with its stop and target

  python buy_plan.py   print the plan (no email)
"""

from datetime import datetime, timezone

from market_clock import CENTRAL

BOT_NAMES = {
    "A_PLUS": "Bot 1 · A+ Hunter",
    "PULLBACK": "Bot 2 · Dip Buyer",
    "MOMENTUM": "Bot 3 · Momentum Rider",
}
WARNED_SHOWN = 6
DASHBOARD_URL = "http://127.0.0.1:8001/#bots"


def _money(value):
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _labels(stock):
    return [
        str(item.get("label")) for item in stock.get("warnings") or []
        if isinstance(item, dict) and item.get("label")
    ]


def stock_line(stock):
    """KMB (Consumer Staples) · 20 shares near $108.79 · stop ... · target ..."""
    risk = stock.get("risk_preview") or {}
    parts = [
        f"{stock.get('ticker')} ({stock.get('sector') or 'sector ?'})",
        f"{risk.get('shares', '?')} shares near {_money(risk.get('entry_reference'))}",
        f"stop {_money(risk.get('stop'))}",
    ]
    if stock.get("source") == "MOMENTUM":
        parts.append("trailing stop, no target")
    else:
        parts.append(f"target {_money(risk.get('target'))} (2R)")
    if stock.get("source") == "PULLBACK" and stock.get("return_6m") is not None:
        parts.append(f"6M {float(stock['return_6m']):+.0f}%, 5D {float(stock.get('return_5d') or 0):+.1f}%")
    if stock.get("source") == "MOMENTUM" and stock.get("jump_pct") is not None:
        parts.append(f"+{float(stock['jump_pct']):.1f}% on {float(stock.get('volume_ratio') or 0):.1f}x volume")
    return " · ".join(parts)


def _held_tickers(open_trades):
    return {trade.get("ticker") for trade in open_trades}


def review_list(stocks, held_tickers=(), held_sectors=None):
    """[(stock, reasons)] in rank order. The plan pick is the first stock
    with no reasons: no warning, not held, sector not held. Every other
    stock gets the reasons it is not in the plan.

    Used by the email, the dashboard and the hand-buy check, so all three
    always agree.
    """
    held_sectors = held_sectors or {}
    reviewed = []
    pick = None
    for stock in stocks:
        if not isinstance(stock, dict):
            continue
        reasons = _labels(stock)
        sector = stock.get("sector")
        if stock.get("ticker") in held_tickers:
            reasons.append("already held")
        elif sector and sector in held_sectors:
            reasons.append(f"{sector} already held ({', '.join(held_sectors[sector])})")
        if not reasons:
            if pick is None:
                pick = stock.get("ticker")
            else:
                reasons.append(f"the plan pick is {pick}")
        reviewed.append((stock, reasons))
    return reviewed


def plan_pick(stocks, held_tickers=(), held_sectors=None):
    """The one stock of a bot list to buy by hand, or None."""
    for stock, reasons in review_list(stocks, held_tickers, held_sectors):
        if not reasons:
            return stock
    return None


def build_buy_plan(slots, open_trades, auto_bots, held_sectors=None,
                   pause=None, auto_check=None, now=None):
    """(subject, body). auto_check(stock) returns why an automatic buy
    would be skipped, or None."""
    now = (now or datetime.now(timezone.utc)).astimezone(CENTRAL)
    held_sectors = held_sectors or {}
    held = _held_tickers(open_trades)
    bots = (slots or {}).get("bots") or {}
    picks = (slots or {}).get("selections") or []
    lines = []

    if not slots:
        body = ("No buy plan: the slot selection did not run or could not be read. "
                "Check the System page of the dashboard.")
        return "SultanQuant buy plan: scan results missing", body

    generated = slots.get("generated_at")
    try:
        when = datetime.fromisoformat(generated).astimezone(CENTRAL)
        scan_text = f"{when:%a %b %d, %I:%M %p} Central".replace(" 0", " ")
    except (TypeError, ValueError):
        scan_text = "unknown time"
    lines += [f"SULTANQUANT BUY PLAN · scan of {scan_text}", ""]

    # 1. Automatic
    lines.append("1) BOUGHT AUTOMATICALLY AT 8:45 AM CENTRAL")
    auto_buys = []
    auto_picks = [p for p in picks if p.get("source") in auto_bots and p.get("ticker") not in held]
    if pause:
        lines.append(f"   Buying is paused: {pause}")
    if not auto_picks:
        names = ", ".join(BOT_NAMES.get(b, b) for b in auto_bots) or "no bot"
        lines.append(f"   Nothing. Auto-buy is on for {names}, which has no pick today.")
    for stock in auto_picks:
        lines.append(f"   {BOT_NAMES.get(stock['source'], stock['source'])}: {stock_line(stock)}")
        skip = pause or (auto_check(stock) if auto_check else None)
        if skip:
            lines.append(f"   -> WILL BE SKIPPED: {skip}")
        else:
            auto_buys.append(stock["ticker"])
            lines.append("   -> will be bought unless the price opens more than 0.5 ATR higher.")
    lines.append("")

    # 2. By hand
    main_free = int(slots.get("free_slots") or 0) - len(
        [t for t in auto_buys if any(p["ticker"] == t and p["source"] != "MOMENTUM" for p in auto_picks)]
    )
    momentum_free = int(slots.get("momentum_free") or 0)
    lines.append("2) FOR YOU TO BUY BY HAND (dashboard Buy button, after 8:30 AM)")
    clean_total = 0
    for source in ("A_PLUS", "PULLBACK", "MOMENTUM"):
        if source in auto_bots:
            continue
        free = momentum_free if source == "MOMENTUM" else max(main_free, 0)
        stocks = [s for s in bots.get(source) or [] if s.get("ticker") not in held]
        lines.append(f"   {BOT_NAMES[source]} · {len(stocks)} listed · "
                     f"{free} free slot{'s' if free != 1 else ''}")
        if not stocks:
            lines.append("     No stock today.")
            continue
        if free <= 0:
            lines.append("     No free slot: a trade has to close first.")
            continue
        reviewed = review_list(stocks, held, held_sectors)
        pick = next((stock for stock, reasons in reviewed if not reasons), None)
        others = [(stock, reasons) for stock, reasons in reviewed if reasons]
        if pick:
            clean_total += 1
            lines.append(f"     PLAN PICK 👑 {stock_line(pick)}")
        else:
            lines.append("     No plan pick: every stock has a warning or its sector is held.")
        if others:
            shown = "; ".join(f"{s['ticker']} ({', '.join(r)})" for s, r in others[:WARNED_SHOWN])
            more = f"; +{len(others) - WARNED_SHOWN} more" if len(others) > WARNED_SHOWN else ""
            lines.append(f"     Not in the plan: {shown}{more}")
    lines.append("")

    # 3. Open trades
    lines.append(f"3) YOUR OPEN TRADES ({len(open_trades)})")
    if not open_trades:
        lines.append("   None.")
    for trade in open_trades:
        stop = trade.get("active_stop_price") or trade.get("stop_price")
        target = ("trailing stop, no target" if trade.get("strategy") == "MOMENTUM"
                  else f"target {_money(trade.get('target_price'))}")
        lines.append(
            f"   {trade.get('ticker')} · {BOT_NAMES.get(trade.get('strategy'), trade.get('strategy'))} · "
            f"{trade.get('shares')} shares at {_money(trade.get('entry_price'))} · "
            f"stop {_money(stop)} · {target}"
        )
    lines += ["", f"Dashboard: {DASHBOARD_URL}",
              "Paper trading. The bot's rules, not financial advice."]

    if auto_buys:
        headline = f"Bot 1 buys {', '.join(auto_buys)}"
    elif auto_picks:
        headline = f"no automatic buy ({', '.join(p['ticker'] for p in auto_picks)} skipped)"
    else:
        headline = "no automatic buy"
    headline += f" · {clean_total} plan pick{'s' if clean_total != 1 else ''} to buy by hand"
    return f"SultanQuant buy plan: {headline}", "\n".join(lines)


def current_buy_plan():
    """The plan from the latest slot selection, journal and settings."""
    import json

    from broker import auto_buy_bots
    from paper_journal import get_paper_trades, held_sectors
    from project_paths import SLOT_SELECTION_JSON
    from risk_guard import trading_pause
    from trade_manager import SOURCE_AUTO, TradeError, check_auto_protection

    try:
        slots = json.loads(SLOT_SELECTION_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        slots = None

    def auto_check(stock):
        try:
            check_auto_protection(stock["ticker"], stock, stock["source"], None)
        except TradeError as exc:
            return str(exc)
        return None

    return build_buy_plan(
        slots,
        get_paper_trades(limit=1)["open"],
        auto_buy_bots(),
        held_sectors(),
        trading_pause(),
        auto_check,
    )


if __name__ == "__main__":
    subject, body = current_buy_plan()
    print(subject)
    print()
    print(body)
