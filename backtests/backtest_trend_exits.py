import math
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# ============================================================
# TREND EXIT BACKTEST
#
# Same entries as the strict dual-setup strategy, different
# exits. The current exit (2R target or 20 days) caps every
# winner near 2R; the trend exits below let a winner run until
# the trend breaks.
#
#   CURRENT_2R_20D       2R target, time exit after 20 days
#   ATR_TRAIL_3X         stop trails the highest close by 3 ATR
#   EMA20_CLOSE          exit next open after a close below EMA20
#   HALF_2R_THEN_TRAIL   sell half at 2R, stop to breakeven,
#                        trail the rest by 3 ATR
#   2R_HOLD_ABOVE_EMA20  2R target; after 20 days stay in while
#                        the close is above EMA20, up to 60 days
#
# Every variant keeps the 1.5 ATR starting stop and 1% risk.
# Stops only move up, after the close, so they apply from the
# next session (no look-ahead).
#
# Same periods, data, and execution as
# backtest_holdout_validation.py (development + 2-year holdout).
#
# LIMITATIONS:
#   - Today's S&P 500 membership is used historically, so
#     survivorship bias remains. It flatters trend exits more
#     than fixed targets, because survivors trended.
#   - When a bar touches both the stop and the target, the stop
#     is assumed to fill first (same as the base backtest).
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[1]

for folder in (ROOT_DIR, ROOT_DIR / "backtests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import backtest_holdout_validation as hv
from project_paths import BACKTEST_RESULTS_DIR

COMPARISON_CSV = BACKTEST_RESULTS_DIR / "trend_exits_comparison.csv"
TRADES_CSV = BACKTEST_RESULTS_DIR / "trend_exits_trades.csv"

COMPARE_FIELDS = [
    "total_trades",
    "win_rate_pct",
    "average_win_r",
    "average_loss_r",
    "largest_win_r",
    "trades_5r_plus",
    "expectancy_r",
    "profit_factor",
    "total_return_pct",
    "cagr_pct",
    "max_drawdown_pct",
    "max_consecutive_losses",
    "average_holding_days",
    "market_exposure_pct",
]


@dataclass(frozen=True)
class ExitRule:
    name: str
    target_r: float | None = None
    partial_r: float | None = None
    trail_atr: float | None = None
    ema20_exit: bool = False
    max_hold_days: int | None = None
    # Past max_hold_days, keep holding while close >= EMA20, up to this.
    extended_hold_days: int | None = None


CURRENT_EXIT = ExitRule(
    "CURRENT_2R_20D",
    target_r=hv.REWARD_RISK_RATIO,
    max_hold_days=hv.MAX_HOLD_DAYS,
)

EXIT_RULES = [
    CURRENT_EXIT,
    ExitRule("ATR_TRAIL_3X", trail_atr=3.0),
    ExitRule("EMA20_CLOSE", ema20_exit=True),
    ExitRule("HALF_2R_THEN_TRAIL", partial_r=2.0, trail_atr=3.0),
    ExitRule(
        "2R_HOLD_ABOVE_EMA20",
        target_r=hv.REWARD_RISK_RATIO,
        max_hold_days=hv.MAX_HOLD_DAYS,
        extended_hold_days=60,
    ),
]


def run_with_exit(rule, candidate_df, calendar, stock_dfs):
    """hv.run_strategy (CURRENT_CONTINUOUS) with a pluggable exit."""
    slip = hv.SLIPPAGE_PCT
    candidate_lookup = {}
    if not candidate_df.empty:
        candidate_lookup = {
            date: rows.iloc[0].to_dict()
            for date, rows in candidate_df.groupby("signal_date")
        }

    calendar_index = {date: index for index, date in enumerate(calendar)}
    cash = hv.STARTING_EQUITY
    position = None
    pending = None
    trades = []
    equity_rows = []
    skipped = []

    def sell(shares, price):
        nonlocal cash
        proceeds = shares * price
        cash += proceeds
        position["proceeds"] += proceeds
        position["shares_open"] -= shares

    def close_trade(date_value, price, reason):
        nonlocal position
        sell(position["shares_open"], price)
        pnl = position["proceeds"] - position["entry_cost"]
        risk = position["planned_risk"]
        if position["partial_price"] is not None:
            reason = f"PARTIAL+{reason}"
        trades.append({
            **position["signal"],
            "exit_rule": rule.name,
            "entry_date": position["entry_date"],
            "exit_date": date_value,
            "entry_price": position["entry_price"],
            "partial_exit_price": position["partial_price"],
            "exit_price": price,
            "stop": position["initial_stop"],
            "final_stop": position["stop"],
            "target": position["target"],
            "shares": position["shares"],
            "planned_risk": risk,
            "pnl": pnl,
            "r_multiple": pnl / risk if risk > 0 else 0.0,
            "holding_days": (
                calendar_index[date_value] - position["entry_index"] + 1
            ),
            "exit_reason": reason,
        })
        position = None

    def stop_reason():
        return (
            "TRAIL_STOP"
            if position["stop"] > position["initial_stop"]
            else "STOP"
        )

    def take_partial(price):
        sell(position["shares"] // 2, price)
        position["partial_price"] = price
        position["stop"] = max(position["stop"], position["entry_price"])

    for i, date_value in enumerate(calendar):
        # 1. Exits at the open: flagged EMA20 exit, gaps through levels.
        if position is not None:
            bar = hv.get_bar(stock_dfs, position["ticker"], date_value)
            if bar is not None:
                open_price = float(bar["Open"])
                if position["exit_at_open"]:
                    close_trade(date_value, open_price * (1 - slip), "EMA20_EXIT")
                elif open_price <= position["stop"]:
                    close_trade(
                        date_value, open_price * (1 - slip), f"GAP_{stop_reason()}"
                    )
                elif position["target"] and open_price >= position["target"]:
                    close_trade(date_value, open_price * (1 - slip), "GAP_TARGET")
                elif (
                    position["partial_level"]
                    and position["partial_price"] is None
                    and open_price >= position["partial_level"]
                ):
                    take_partial(open_price * (1 - slip))

        # 2. Enter the pending signal at today's open.
        if pending is not None and position is None:
            signal = pending
            pending = None
            bar = hv.get_bar(stock_dfs, signal["ticker"], date_value)
            if bar is None:
                skipped.append({**signal, "skip_reason": "NO_ENTRY_BAR"})
            else:
                entry_price = float(bar["Open"]) * (1 + slip)
                stop_distance = signal["atr"] * hv.ATR_MULTIPLIER
                max_risk = cash * (hv.RISK_PER_TRADE_PCT / 100)
                shares = min(
                    math.floor(max_risk / stop_distance),
                    math.floor(cash / entry_price),
                )
                if shares <= 0:
                    skipped.append({**signal, "skip_reason": "INSUFFICIENT_CASH"})
                else:
                    entry_cost = shares * entry_price
                    cash -= entry_cost
                    position = {
                        "ticker": signal["ticker"],
                        "signal": signal,
                        "entry_date": date_value,
                        "entry_index": calendar_index[date_value],
                        "entry_price": entry_price,
                        "entry_cost": entry_cost,
                        "shares": shares,
                        "shares_open": shares,
                        "proceeds": 0.0,
                        "planned_risk": shares * stop_distance,
                        "initial_stop": entry_price - stop_distance,
                        "stop": entry_price - stop_distance,
                        "target": (
                            entry_price + stop_distance * rule.target_r
                            if rule.target_r
                            else None
                        ),
                        "partial_level": (
                            entry_price + stop_distance * rule.partial_r
                            if rule.partial_r
                            else None
                        ),
                        "partial_price": None,
                        "highest_close": entry_price,
                        "exit_at_open": False,
                    }

        # 3. Intraday exits (stop first), then time exit at the close.
        if position is not None:
            bar = hv.get_bar(stock_dfs, position["ticker"], date_value)
            if bar is not None:
                low = float(bar["Low"])
                high = float(bar["High"])
                close = float(bar["Close"])
                hold_days = calendar_index[date_value] - position["entry_index"] + 1

                if low <= position["stop"]:
                    close_trade(
                        date_value, position["stop"] * (1 - slip), stop_reason()
                    )
                elif position["target"] and high >= position["target"]:
                    close_trade(
                        date_value, position["target"] * (1 - slip), "TARGET"
                    )
                else:
                    if (
                        position["partial_level"]
                        and position["partial_price"] is None
                        and high >= position["partial_level"]
                    ):
                        take_partial(position["partial_level"] * (1 - slip))

                    if rule.max_hold_days and hold_days >= rule.max_hold_days:
                        hold_on = (
                            rule.extended_hold_days
                            and hold_days < rule.extended_hold_days
                            and close >= float(bar["EMA20"])
                        )
                        if not hold_on:
                            close_trade(date_value, close * (1 - slip), "TIME_EXIT")

                # 4. After the close: move the stop up / flag EMA20 exit.
                if position is not None:
                    position["highest_close"] = max(position["highest_close"], close)
                    if rule.trail_atr:
                        trail = (
                            position["highest_close"]
                            - rule.trail_atr * float(bar["ATR"])
                        )
                        position["stop"] = max(position["stop"], trail)
                    if rule.ema20_exit and close < float(bar["EMA20"]):
                        position["exit_at_open"] = True

        # 5. Mark equity.
        equity = cash
        if position is not None:
            bar = hv.get_bar(stock_dfs, position["ticker"], date_value)
            mark = float(bar["Close"]) if bar is not None else position["entry_price"]
            equity += position["shares_open"] * mark
        equity_rows.append({
            "date": date_value,
            "equity": equity,
            "cash": cash,
            "open_position": position["ticker"] if position is not None else None,
        })

        # 6. After the close: queue the next entry while flat.
        if position is not None or pending is not None or i >= len(calendar) - 1:
            continue
        candidate = candidate_lookup.get(date_value)
        if candidate is not None:
            pending = candidate.copy()

    final_date = calendar[-1]
    if position is not None:
        bar = hv.get_bar(stock_dfs, position["ticker"], final_date)
        if bar is not None:
            close_trade(final_date, float(bar["Close"]) * (1 - slip), "END_OF_TEST")
            equity_rows[-1].update(equity=cash, cash=cash, open_position=None)

    return {
        "mode": rule.name,
        "trades": trades,
        "equity": equity_rows,
        "skipped": skipped,
        "ending_cash": cash,
    }


def check_matches_base_engine(candidates, calendar, stock_dfs):
    """The current exit must reproduce hv.run_strategy exactly."""
    expected = hv.run_strategy(
        mode="CURRENT_CONTINUOUS",
        candidate_df=candidates,
        calendar=calendar,
        stock_dfs=stock_dfs,
    )
    actual = run_with_exit(CURRENT_EXIT, candidates, calendar, stock_dfs)
    pairs = list(zip(expected["trades"], actual["trades"]))
    if len(expected["trades"]) != len(actual["trades"]) or any(
        a["ticker"] != b["ticker"]
        or a["exit_reason"] != b["exit_reason"]
        or not math.isclose(a["pnl"], b["pnl"], abs_tol=1e-6)
        for a, b in pairs
    ):
        raise RuntimeError(
            "Trend-exit engine does not reproduce the base backtest; "
            "results would not be comparable."
        )
    if not math.isclose(expected["ending_cash"], actual["ending_cash"], abs_tol=1e-6):
        raise RuntimeError("Trend-exit engine ending cash differs from base.")


def summarize(result, calendar, spy_df):
    summary, _ = hv.summarize(result, calendar[0], calendar[-1], spy_df)
    r_values = [trade["r_multiple"] for trade in result["trades"]]
    summary["largest_win_r"] = round(max(r_values, default=0.0), 2)
    summary["trades_5r_plus"] = sum(1 for r in r_values if r >= 5)
    return summary


def main():
    print()
    print("================================")
    print("TREND EXIT BACKTEST")
    print("================================")

    constituents = hv.load_sp500_constituents()
    tickers = constituents["ticker"].tolist()

    stock_raw = hv.download_many(tickers, "S&P 500 stocks")
    market_raw = hv.download_many(
        ["SPY", *hv.SECTOR_ETFS.values()], "SPY + sector ETFs"
    )
    if "SPY" not in market_raw:
        raise RuntimeError("SPY data missing.")

    stock_dfs, panel = hv.prepare_stock_data(constituents, stock_raw)
    spy_df = hv.add_indicators(market_raw["SPY"])
    sector_dfs = {
        etf: hv.add_indicators(market_raw[etf])
        for etf in hv.SECTOR_ETFS.values()
        if etf in market_raw
    }

    # Same period split as backtest_holdout_validation.py.
    calendar = spy_df.index
    final_date = calendar.max()
    full_calendar = calendar[
        calendar >= final_date - pd.DateOffset(years=hv.BACKTEST_YEARS)
    ]
    holdout_boundary = final_date - pd.DateOffset(years=hv.HOLDOUT_YEARS)
    holdout_start = full_calendar[full_calendar >= holdout_boundary][0]
    development_calendar = full_calendar[full_calendar < holdout_start]
    holdout_calendar = full_calendar[full_calendar >= holdout_start]

    candidates = hv.build_candidate_stream(
        panel=panel,
        spy_df=spy_df,
        sector_dfs=sector_dfs,
        signal_dates=full_calendar[:-1],
    )
    candidates["signal_date"] = pd.to_datetime(candidates["signal_date"])

    check_matches_base_engine(candidates, full_calendar, stock_dfs)
    print()
    print("Check passed: current exit reproduces the base backtest.")

    summaries = []
    all_trades = []
    for period, period_calendar in (
        ("DEVELOPMENT", development_calendar),
        ("HOLDOUT", holdout_calendar),
        ("FULL", full_calendar),
    ):
        signal_dates = set(period_calendar[:-1])
        period_candidates = candidates[candidates["signal_date"].isin(signal_dates)]
        for rule in EXIT_RULES:
            result = run_with_exit(rule, period_candidates, period_calendar, stock_dfs)
            summary = summarize(result, period_calendar, spy_df)
            summary["period_type"] = period
            summaries.append(summary)
            if period == "FULL":
                all_trades.extend(result["trades"])

    comparison = pd.DataFrame(summaries)
    columns = [
        "period_type",
        "strategy",
        "start_date",
        "end_date",
        *COMPARE_FIELDS,
        "spy_total_return_pct",
    ]
    comparison = comparison[[c for c in columns if c in comparison.columns]]
    comparison.to_csv(COMPARISON_CSV, index=False)
    pd.DataFrame(all_trades).to_csv(TRADES_CSV, index=False)

    print()
    print("================================")
    print("RESULTS")
    print("================================")
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(comparison.to_string(index=False))

    print()
    print("Saved:")
    print(COMPARISON_CSV)
    print(TRADES_CSV)

    print()
    print("IMPORTANT")
    print(
        "The holdout has already been inspected for the base strategy. "
        "Prefer an exit that wins in BOTH development and holdout, "
        "not one that only wins on the full period."
    )


if __name__ == "__main__":
    main()
