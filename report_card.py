"""Weekly report card: how each bot is doing against its backtest.

  python report_card.py            print the report card
  python report_card.py --email    print it and email it (SMTP in .env)
  python report_card.py --friday   email it only on Fridays (the evening
                                   scan runs this every weekday)

For each bot, from the closed paper trades: win rate, average win and
average loss in R, R per trade (expectancy), total R and P&L, for this
week and for all time. R = realized P&L / planned risk, the shares times
(entry - first stop). The all-time numbers are compared with the bot's
10-year backtest once it has MIN_TRADES_TO_COMPARE closed trades; fewer
trades say too little. The dashboard shows the same report.

Off-plan trades (off_plan = 1: hand picks outside the buy plan, and the
trades from before the plan rule) are left out of each bot's numbers and
shown on their own, so a bot is judged only on trades its rules took.
Slippage is the real fill against the scan price the buy was planned at,
in % and in R, over every buy that has a planned price.
"""

import sys
from datetime import datetime, timedelta, timezone

from paper_journal import get_closed_trades, get_paper_trades

WEEK_DAYS = 7
MIN_TRADES_TO_COMPARE = 10
# A bot is "on track" while its win rate is within this many points of the
# backtest and its R per trade is at least half the backtest's.
WIN_RATE_TOLERANCE = 10

BOTS = {
    "A_PLUS": "Bot 1 · A+ Hunter",
    "PULLBACK": "Bot 2 · Dip Buyer",
    "MOMENTUM": "Bot 3 · Momentum Rider",
}
# 10-year backtests (PROJECT_LOG, 2026-10-28 and 2026-10-31). Bot 3's R per
# trade was not measured: its result rests on a few huge winners.
BACKTEST = {
    "A_PLUS": {"win_rate": 49.2, "expectancy_r": 0.34},
    "PULLBACK": {"win_rate": 48.7, "expectancy_r": 0.31},
    "MOMENTUM": {"win_rate": 33.0, "expectancy_r": None},
}


def trade_r(trade):
    """Result of one closed trade in R, or None if the risk is unknown."""
    try:
        risk = (float(trade["entry_price"]) - float(trade["stop_price"])) * int(trade["shares"])
        pnl = float(trade["realized_pnl"])
    except (KeyError, TypeError, ValueError):
        return None
    return pnl / risk if risk > 0 else None


def _exit_time(trade):
    try:
        moment = datetime.fromisoformat(str(trade["exit_time"]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def bot_stats(trades):
    """Win rate, average win / loss, R per trade, totals for closed trades."""
    results = [(trade, trade_r(trade)) for trade in trades]
    rs = [r for _, r in results if r is not None]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    best = max(results, key=lambda item: item[1], default=None) if rs else None
    worst = min(results, key=lambda item: item[1], default=None) if rs else None
    return {
        "closed": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(rs) * 100 if rs else None,
        "avg_win_r": sum(wins) / len(wins) if wins else None,
        "avg_loss_r": sum(losses) / len(losses) if losses else None,
        "expectancy_r": sum(rs) / len(rs) if rs else None,
        "total_r": sum(rs),
        "pnl": sum(float(trade["realized_pnl"] or 0) for trade in trades),
        "best": {"ticker": best[0]["ticker"], "r": best[1]} if best and best[1] is not None else None,
        "worst": {"ticker": worst[0]["ticker"], "r": worst[1]} if worst and worst[1] is not None else None,
    }


def slippage_stats(trades):
    """Average fill vs planned (scan) price, in % and in R."""
    rows = []
    for trade in trades:
        try:
            planned = float(trade.get("planned_price") or 0)
            fill = float(trade["entry_price"])
            risk = fill - float(trade["stop_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if planned > 0 and risk > 0:
            rows.append(((fill / planned - 1) * 100, (fill - planned) / risk))
    if not rows:
        return {"buys": 0, "avg_pct": None, "avg_r": None}
    return {
        "buys": len(rows),
        "avg_pct": sum(pct for pct, _ in rows) / len(rows),
        "avg_r": sum(r for _, r in rows) / len(rows),
    }


def _off_plan(trade):
    return bool(trade.get("off_plan"))


def verdict(stats, backtest):
    """(code, text) comparing a bot's all-time results with its backtest."""
    if stats["closed"] < MIN_TRADES_TO_COMPARE:
        return "EARLY", (
            f"Too early: {stats['closed']} of {MIN_TRADES_TO_COMPARE} closed "
            "trades needed to compare with the backtest."
        )
    reasons = []
    if stats["win_rate"] is not None and stats["win_rate"] < backtest["win_rate"] - WIN_RATE_TOLERANCE:
        reasons.append(
            f"win rate {stats['win_rate']:.0f}% vs {backtest['win_rate']:.0f}% in the backtest"
        )
    target = backtest["expectancy_r"]
    if target is not None and stats["expectancy_r"] is not None and stats["expectancy_r"] < target / 2:
        reasons.append(
            f"{stats['expectancy_r']:+.2f}R per trade vs {target:+.2f}R in the backtest"
        )
    if reasons:
        return "BELOW", "Below the backtest: " + "; ".join(reasons) + "."
    return "ON_TRACK", "On track with the backtest."


def build_report(database_path=None, now=None):
    now = now or datetime.now(timezone.utc)
    week_start = now - timedelta(days=WEEK_DAYS)
    every_closed = get_closed_trades(database_path)
    open_trades = get_paper_trades(limit=1, database_path=database_path)["open"]
    off_plan = [trade for trade in every_closed if _off_plan(trade)]
    closed = [trade for trade in every_closed if not _off_plan(trade)]
    this_week = [trade for trade in closed if (moment := _exit_time(trade)) and moment >= week_start]

    bots = {}
    for code, name in BOTS.items():
        mine = [trade for trade in closed if trade.get("strategy") == code]
        stats = bot_stats(mine)
        status, text = verdict(stats, BACKTEST[code])
        bots[code] = {
            "name": name,
            "all_time": stats,
            "week": bot_stats([trade for trade in this_week if trade.get("strategy") == code]),
            "backtest": BACKTEST[code],
            "open": sum(1 for trade in open_trades
                        if trade.get("strategy") == code and not _off_plan(trade)),
            "verdict": status,
            "verdict_text": text,
            "slippage": slippage_stats(
                [t for t in every_closed + open_trades if t.get("strategy") == code]
            ),
        }
    return {
        "generated_at": now.isoformat(),
        "week_start": week_start.isoformat(),
        "bots": bots,
        "total": {"all_time": bot_stats(closed), "week": bot_stats(this_week)},
        "off_plan": {
            **bot_stats(off_plan),
            "open": sum(1 for trade in open_trades if _off_plan(trade)),
            "tickers": [trade["ticker"] for trade in off_plan],
        },
        "slippage": slippage_stats(every_closed + open_trades),
        "week_trades": [
            {
                "ticker": trade["ticker"],
                "strategy": trade.get("strategy"),
                "exit_time": trade.get("exit_time"),
                "exit_reason": trade.get("exit_reason"),
                "r": trade_r(trade),
                "pnl": trade.get("realized_pnl"),
                "entry_note": trade.get("entry_note"),
                "exit_note": trade.get("exit_note"),
            }
            for trade in this_week
        ],
        "open_trades": len(open_trades),
    }


def _r(value):
    return "-" if value is None else f"{value:+.2f}R"


def _pct(value):
    return "-" if value is None else f"{value:.0f}%"


def format_report(report):
    week = report["total"]["week"]
    lines = [
        "SULTANQUANT WEEKLY REPORT CARD (Alpaca paper)",
        f"Week ending {datetime.fromisoformat(report['generated_at']).astimezone():%a %b %d, %Y}",
        "",
        f"This week: {week['closed']} closed trade(s), {week['wins']} won, "
        f"{_r(week['total_r'] if week['closed'] else None)}, ${week['pnl']:+,.2f}. "
        f"Open now: {report['open_trades']}.",
    ]
    for trade in report["week_trades"]:
        lines.append(
            f"  {trade['ticker']:6s} {BOTS.get(trade['strategy'], trade['strategy'] or '?').replace(' · ', ' - '):24s} "
            f"{trade['exit_reason'] or '':18s} {_r(trade['r']):>8s}  ${float(trade['pnl'] or 0):+,.2f}"
        )
        if trade.get("exit_note"):
            lines.append(f"         Lesson: {trade['exit_note']}")
    for code, bot in report["bots"].items():
        stats, backtest = bot["all_time"], bot["backtest"]
        lines += [
            "",
            f"{bot['name'].replace(' · ', ' - ')} (open: {bot['open']})",
            f"  All time: {stats['closed']} closed | win rate {_pct(stats['win_rate'])} "
            f"(backtest {backtest['win_rate']:.0f}%) | R per trade {_r(stats['expectancy_r'])} "
            f"(backtest {_r(backtest['expectancy_r'])})",
            f"  Average win {_r(stats['avg_win_r'])} | average loss {_r(stats['avg_loss_r'])} | "
            f"total {_r(stats['total_r'] if stats['closed'] else None)} | ${stats['pnl']:+,.2f}",
            f"  {bot['verdict_text']}",
        ]
        slip = bot["slippage"]
        if slip["buys"]:
            lines.append(
                f"  Slippage: {slip['avg_pct']:+.2f}% ({_r(slip['avg_r'])}) a buy, over {slip['buys']} buy(s)"
            )
    off = report["off_plan"]
    if off["closed"] or off["open"]:
        lines += [
            "",
            f"Off-plan trades (not counted above): {off['closed']} closed "
            f"({', '.join(off['tickers']) or 'none'}), {off['open']} open | "
            f"{_r(off['total_r'] if off['closed'] else None)} | ${off['pnl']:+,.2f}",
        ]
    total = report["total"]["all_time"]
    lines += [
        "",
        f"All bots: {total['closed']} closed | win rate {_pct(total['win_rate'])} | "
        f"{_r(total['expectancy_r'])} per trade | ${total['pnl']:+,.2f}",
        "",
        "Paper money only. Backtests do not guarantee future results.",
    ]
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    text = format_report(build_report())
    print(text)
    friday = datetime.now().weekday() == 4
    if "--email" in argv or ("--friday" in argv and friday):
        from scan_notifications import send_email

        send_email("SultanQuant weekly report card", text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
