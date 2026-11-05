"""Automatic "Lesson" notes for closed trades.

When a trade closes, the bot writes what happened and what it teaches, so
the journal does not depend on writing notes by hand:

  result     won / lost N R ($), sessions held, why it closed
  path       best and worst close while held, in R (how close it came to
             the target, or a gain that was given back)
  market     SPY over the same days next to the stock
  earnings   a report date that fell inside the trade
  takeaway   one line picked from the facts above

Only trades without a lesson are filled in; a note written or edited by
hand is never replaced. Run by auto_trader.py after every sync (morning
buy and evening scan). Prices come from Yahoo Finance; if they cannot be
loaded the trade is tried again on the next run, and after
GIVE_UP_DAYS a lesson is written from the journal alone.

  python trade_lessons.py   fill in missing lessons now
"""

from datetime import date, datetime, timedelta, timezone

from paper_journal import get_closed_trades, set_trade_note

AUTO_PREFIX = "Auto: "
GIVE_UP_DAYS = 5
BENCHMARK = "SPY"

EXIT_TEXT = {
    "STOP": "the stop was hit",
    "TARGET": "the target was hit",
    "TIME_EXIT": "sold after 20 sessions",
    "MANUAL_SELL": "sold by hand",
    "EARNINGS_EXIT": "sold before earnings",
    "TRAIL_3ATR": "the trailing stop was hit",
}


def _day(value):
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment.date()


def _risk_per_share(trade):
    try:
        return float(trade["entry_price"]) - float(trade["stop_price"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def trade_r(trade):
    risk = _risk_per_share(trade) * int(trade.get("shares") or 0)
    try:
        return float(trade["realized_pnl"]) / risk if risk > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def exit_text(reason):
    reason = str(reason or "")
    if reason.startswith("PARTIAL+"):
        rest = EXIT_TEXT.get(reason.split("+", 1)[1], reason.split("+", 1)[1].lower())
        return f"half sold at 2R, then for the rest {rest}"
    return EXIT_TEXT.get(reason, reason.lower().replace("_", " ") or "closed")


def _closes(bars, start, end):
    """Daily closes with start <= date <= end."""
    if bars is None or len(bars) == 0:
        return []
    out = []
    for stamp, close in bars["Close"].items():
        day = stamp.date() if hasattr(stamp, "date") else stamp
        if start <= day <= end:
            out.append((day, float(close)))
    return out


def _change(bars, start, end):
    """% change from the last close before `start` to the close on `end`."""
    if bars is None or len(bars) == 0:
        return None
    before = [c for d, c in _closes(bars, date.min, start - timedelta(days=1))]
    during = _closes(bars, start, end)
    if not before or not during:
        return None
    return (during[-1][1] / before[-1] - 1) * 100


def build_lesson(trade, bars=None, spy_bars=None, earnings_dates=()):
    """The lesson text for one closed trade (pure; data passed in)."""
    entry_day, exit_day = _day(trade.get("entry_time")), _day(trade.get("exit_time"))
    r = trade_r(trade)
    pnl = float(trade.get("realized_pnl") or 0)
    risk = _risk_per_share(trade)
    ticker = trade.get("ticker", "?")
    strategy = trade.get("strategy")

    held = _closes(bars, entry_day, exit_day) if entry_day and exit_day else []
    sessions = len(held)
    sentences = []

    result = "Won" if pnl > 0 else "Lost" if pnl < 0 else "Broke even"
    amount = f" {abs(r):.2f}R" if r is not None and pnl else ""
    span = f" in {sessions} session{'s' if sessions != 1 else ''}" if sessions else ""
    sentences.append(
        f"{result}{amount} (${pnl:+,.2f}){span}: {exit_text(trade.get('exit_reason'))}."
    )

    best_r = worst_r = None
    if held and risk > 0:
        entry = float(trade["entry_price"])
        best_r = (max(c for _, c in held) - entry) / risk
        worst_r = (min(c for _, c in held) - entry) / risk
        sentences.append(f"Best close {best_r:+.1f}R, worst {worst_r:+.1f}R while held.")

    stock_move = spy_move = None
    if entry_day and exit_day:
        stock_move = (float(trade["exit_price"]) / float(trade["entry_price"]) - 1) * 100
        spy_move = _change(spy_bars, entry_day, exit_day)
        if spy_move is not None:
            sentences.append(
                f"{BENCHMARK} {spy_move:+.1f}% over the same days ({ticker} {stock_move:+.1f}%)."
            )

    reported = [
        d for d in earnings_dates
        if entry_day and exit_day and entry_day <= d <= exit_day
    ]
    if reported:
        sentences.append(f"Earnings came out on {reported[-1]:%b %d} during the trade.")

    if r is not None and r < -1.15:
        sentences.append(
            f"It lost more than 1R: the price gapped past the stop."
        )
    sentences.append("Takeaway: " + takeaway(
        r, pnl, trade.get("exit_reason"), strategy, best_r, spy_move, stock_move, bool(reported),
    ))
    return AUTO_PREFIX + " ".join(sentences)


def takeaway(r, pnl, reason, strategy, best_r, spy_move, stock_move, earnings):
    reason = str(reason or "")
    if pnl > 0:
        if "TARGET" in reason:
            return "the plan worked; the setup reached its target."
        if strategy == "MOMENTUM":
            return "the trailing stop locked in a gain; let the next one run too."
        return "a winner within the rules."
    if earnings:
        if strategy == "MOMENTUM":
            return "Bot 3 rides through earnings by design; this loss is that risk."
        return ("holding through earnings cost this trade; the earnings exit "
                "now sells Bot 1 / Bot 2 trades before the report.")
    if best_r is not None and best_r >= 1:
        return (f"it was up {best_r:.1f}R and gave it all back; worth testing a "
                "stop at entry after +1R (backtest before changing).")
    if reason == "TIME_EXIT":
        return "the move never came; the money was tied up for 20 sessions."
    if spy_move is not None and spy_move <= -2:
        return "a market-wide drop took it down; a normal loss within the rules."
    if spy_move is not None and stock_move is not None and spy_move >= 0 > stock_move:
        extra = " The dip kept falling after the buy." if strategy == "PULLBACK" else ""
        return ("the stock fell while the market rose: weakness in the stock "
                "itself, not the market." + extra)
    if reason == "MANUAL_SELL":
        return "sold by hand; check whether the rules would have done better."
    return "a normal loss; the stop kept it to about 1R."


def _default_bars(ticker, entry_day):
    from shadow_exits import fetch_daily_bars

    return fetch_daily_bars(ticker, entry_day)


def _default_earnings(ticker):
    from earnings_calendar import fetch_earnings_dates

    return fetch_earnings_dates(ticker)


def fill_missing_lessons(
    database_path=None, fetch_bars=_default_bars, fetch_earnings=_default_earnings, today=None,
):
    """Write a lesson on each closed trade that has none; [(ticker, text)]."""
    today = today or datetime.now(timezone.utc).date()
    written = []
    spy_cache = {}
    for trade in get_closed_trades(database_path):
        if (trade.get("exit_note") or "").strip():
            continue
        entry_day, exit_day = _day(trade.get("entry_time")), _day(trade.get("exit_time"))
        if not entry_day or not exit_day:
            continue
        try:
            bars = fetch_bars(trade["ticker"], entry_day)
            if entry_day not in spy_cache:
                spy_cache[entry_day] = fetch_bars(BENCHMARK, entry_day)
            spy = spy_cache[entry_day]
            if bars is None or len(bars) == 0:
                raise ValueError("no price data")
        except Exception as exc:  # network and data errors
            if (today - exit_day).days < GIVE_UP_DAYS:
                print(f"Lesson for {trade['ticker']} waits for price data: {exc}")
                continue
            bars = spy = None
        try:
            earnings = fetch_earnings(trade["ticker"])
        except Exception:  # earnings are optional
            earnings = []
        text = build_lesson(trade, bars, spy, earnings)
        set_trade_note(trade["id"], "exit_note", text, database_path)
        written.append((trade["ticker"], text))
    return written


def main():
    written = fill_missing_lessons()
    for ticker, text in written:
        print(f"LESSON {ticker}: {text}")
    if not written:
        print("Every closed trade already has a lesson.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
