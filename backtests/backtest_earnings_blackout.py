import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# EARNINGS BLACKOUT BACKTEST
#
# Compares the strict dual-setup strategy with and without the
# live earnings rule: skip any A+ stock whose next earnings
# report is within EARNINGS_BLACKOUT_TRADING_DAYS of entry. When
# the best stock is blocked, the next-best A+ stock is used,
# matching the live master selector.
#
# Same periods, data, and execution as
# backtest_holdout_validation.py (development + 2-year holdout).
#
# LIMITATIONS:
#   - Earnings dates come from Yahoo Finance; tickers with no
#     history are never blocked (same as live UNKNOWN).
#   - Today's S&P 500 membership is used historically, so
#     survivorship bias remains.
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[1]

for folder in (ROOT_DIR, ROOT_DIR / "backtests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import backtest_holdout_validation as hv
from earnings_calendar import (
    EARNINGS_WARNING_TRADING_DAYS,
    fetch_earnings_dates,
)
from project_paths import BACKTEST_RESULTS_DIR

# The live scanner only warns inside this window; this backtest tests
# what a hard block over the same window would do.
EARNINGS_BLACKOUT_TRADING_DAYS = EARNINGS_WARNING_TRADING_DAYS

EARNINGS_HISTORY_LIMIT = 50

EARNINGS_CACHE_CSV = BACKTEST_RESULTS_DIR / "earnings_dates_cache.csv"
COMPARISON_CSV = BACKTEST_RESULTS_DIR / "earnings_blackout_comparison.csv"
BLACKOUT_TRADES_CSV = BACKTEST_RESULTS_DIR / "earnings_blackout_trades.csv"
BLOCKED_SIGNALS_CSV = BACKTEST_RESULTS_DIR / "earnings_blocked_signals.csv"

COMPARE_FIELDS = [
    "total_trades",
    "win_rate_pct",
    "expectancy_r",
    "profit_factor",
    "total_return_pct",
    "max_drawdown_pct",
    "max_consecutive_losses",
    "average_holding_days",
]


def load_earnings_dates(tickers):
    """Earnings dates per ticker, cached so reruns skip the download."""
    cached = {}
    if EARNINGS_CACHE_CSV.exists():
        frame = pd.read_csv(EARNINGS_CACHE_CSV, dtype=str)
        for ticker, group in frame.groupby("ticker"):
            cached[ticker] = sorted(
                pd.to_datetime(group["earnings_date"].dropna()).dt.date
            )

    missing = [ticker for ticker in tickers if ticker not in cached]
    print()
    print(
        f"Earnings dates: {len(tickers) - len(missing)} cached, "
        f"{len(missing)} to download."
    )

    for index, ticker in enumerate(missing, start=1):
        if index % 50 == 0:
            print(f"Earnings dates: {index} / {len(missing)}")
        try:
            cached[ticker] = fetch_earnings_dates(
                ticker, limit=EARNINGS_HISTORY_LIMIT
            )
        except Exception as exc:  # yfinance raises many error types
            print(f"Earnings lookup failed for {ticker}: {exc}")
            cached[ticker] = []

    rows = [
        {"ticker": ticker, "earnings_date": earnings_date.isoformat()}
        for ticker, dates in cached.items()
        for earnings_date in dates
    ]
    # Keep tickers with no dates so they are not downloaded again.
    rows += [
        {"ticker": ticker, "earnings_date": None}
        for ticker, dates in cached.items()
        if not dates
    ]
    pd.DataFrame(rows).to_csv(EARNINGS_CACHE_CSV, index=False)
    return cached


def build_blocked_lookup(earnings_by_ticker, calendar):
    """Map each signal date to the tickers blocked for next-day entry.

    Live rule: blocked when earnings fall 0..N trading days after the
    entry day. In the backtest the entry day is the next calendar
    session after the signal close.
    """
    sessions = pd.DatetimeIndex(calendar)
    next_session = dict(zip(sessions[:-1], sessions[1:]))
    entry_days = np.array(
        [day.date() for day in next_session.values()],
        dtype="datetime64[D]",
    )
    signal_days = list(next_session.keys())

    blocked = {}
    for ticker, dates in earnings_by_ticker.items():
        if not dates:
            continue
        earnings = np.array(dates, dtype="datetime64[D]")
        position = np.searchsorted(earnings, entry_days)
        has_next = position < len(earnings)
        next_dates = earnings[np.minimum(position, len(earnings) - 1)]
        days = np.busday_count(entry_days, next_dates)
        hits = has_next & (days <= EARNINGS_BLACKOUT_TRADING_DAYS)
        for index in np.flatnonzero(hits):
            blocked.setdefault(signal_days[index], set()).add(ticker)
    return blocked


def build_candidate_stream(panel, spy_df, sector_dfs, signal_dates, blocked):
    """Daily best and best-after-blackout candidates in one pass."""
    panel_subset = panel[panel["Date"].isin(signal_dates)]
    panel_by_date = dict(tuple(panel_subset.groupby("Date")))

    baseline_rows = []
    blackout_rows = []
    blocked_rows = []

    print()
    print("Building daily candidates with and without earnings blackout...")

    for index, date_value in enumerate(signal_dates, start=1):
        if index % 250 == 0:
            print(f"Processed {index} / {len(signal_dates)} dates")

        daily = panel_by_date.get(date_value)
        if daily is None or daily.empty:
            continue

        sector_state = hv.build_sector_state(
            date_value=date_value, spy_df=spy_df, sector_dfs=sector_dfs
        )
        best = hv.get_daily_best_candidate(
            date_value=date_value,
            daily_stocks=daily,
            sector_state=sector_state,
        )
        if best is None:
            continue
        baseline_rows.append(best)

        blocked_today = blocked.get(date_value, set())
        if best["ticker"] not in blocked_today:
            blackout_rows.append(best)
            continue

        blocked_rows.append(best)
        replacement = hv.get_daily_best_candidate(
            date_value=date_value,
            daily_stocks=daily,
            sector_state=sector_state,
            exclude_tickers=blocked_today,
        )
        if replacement is not None:
            blackout_rows.append(replacement)

    def to_frame(rows):
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame["signal_date"] = pd.to_datetime(frame["signal_date"])
        return frame

    return to_frame(baseline_rows), to_frame(blackout_rows), to_frame(blocked_rows)


def run_period(name, calendar, candidates, stock_dfs, spy_df):
    signal_dates = set(calendar[:-1])
    period_candidates = candidates[candidates["signal_date"].isin(signal_dates)]
    result = hv.run_strategy(
        mode="CURRENT_CONTINUOUS",
        candidate_df=period_candidates,
        calendar=calendar,
        stock_dfs=stock_dfs,
    )
    summary, _ = hv.summarize(result, calendar[0], calendar[-1], spy_df)
    summary["period_type"] = name
    return summary, result["trades"]


def earnings_trades_held(trades, earnings_by_ticker):
    """Trades whose holding window contained an earnings date."""
    count = 0
    for trade in trades:
        entry = pd.Timestamp(trade["entry_date"]).date()
        exit_ = pd.Timestamp(trade["exit_date"]).date()
        if any(
            entry <= day <= exit_
            for day in earnings_by_ticker.get(trade["ticker"], [])
        ):
            count += 1
    return count


def main():
    print()
    print("================================")
    print("EARNINGS BLACKOUT BACKTEST")
    print("================================")
    print(
        f"Rule: skip A+ stocks with earnings within "
        f"{EARNINGS_BLACKOUT_TRADING_DAYS} trading days of entry."
    )

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

    earnings_by_ticker = load_earnings_dates(sorted(stock_dfs))
    with_dates = sum(1 for dates in earnings_by_ticker.values() if dates)
    print(f"Tickers with earnings history: {with_dates} / {len(stock_dfs)}")

    blocked = build_blocked_lookup(earnings_by_ticker, full_calendar)

    baseline, blackout, blocked_signals = build_candidate_stream(
        panel=panel,
        spy_df=spy_df,
        sector_dfs=sector_dfs,
        signal_dates=full_calendar[:-1],
        blocked=blocked,
    )
    blocked_signals.to_csv(BLOCKED_SIGNALS_CSV, index=False)

    summaries = []
    blackout_trades = []
    for period, period_calendar in (
        ("DEVELOPMENT", development_calendar),
        ("HOLDOUT", holdout_calendar),
        ("FULL", full_calendar),
    ):
        for strategy, candidates in (
            ("BASELINE", baseline),
            ("EARNINGS_BLACKOUT", blackout),
        ):
            summary, trades = run_period(
                period, period_calendar, candidates, stock_dfs, spy_df
            )
            summary["strategy"] = strategy
            summary["trades_holding_through_earnings"] = earnings_trades_held(
                trades, earnings_by_ticker
            )
            summaries.append(summary)
            if strategy == "EARNINGS_BLACKOUT" and period == "FULL":
                blackout_trades = trades

    comparison = pd.DataFrame(summaries)
    columns = [
        "period_type",
        "strategy",
        "start_date",
        "end_date",
        *COMPARE_FIELDS,
        "trades_holding_through_earnings",
    ]
    comparison = comparison[[c for c in columns if c in comparison.columns]]
    comparison.to_csv(COMPARISON_CSV, index=False)
    pd.DataFrame(blackout_trades).to_csv(BLACKOUT_TRADES_CSV, index=False)

    print()
    print("================================")
    print("RESULTS")
    print("================================")
    print(f"Baseline signal days blocked by earnings: {len(blocked_signals)}")
    print()
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(comparison.to_string(index=False))

    print()
    print("Saved:")
    for path in (
        COMPARISON_CSV,
        BLACKOUT_TRADES_CSV,
        BLOCKED_SIGNALS_CSV,
        EARNINGS_CACHE_CSV,
    ):
        print(path)

    print()
    print("IMPORTANT")
    print(
        "The holdout has already been inspected for the base strategy. "
        "Judge this rule on all periods together, not the holdout alone."
    )


if __name__ == "__main__":
    main()
