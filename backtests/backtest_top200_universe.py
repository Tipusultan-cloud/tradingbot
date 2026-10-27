import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# S&P 500 + TOP-200 MOST-TRADED UNIVERSE BACKTEST
#
# Compares the strict dual-setup strategy on two source
# universes, with the same rules, periods, and execution as
# backtest_holdout_validation.py:
#
#   SP500          today's S&P 500 members (current live default
#                  before 2026-10-27)
#   SP500_TOP200   S&P 500 plus, on each day, the 200 other US
#                  common stocks with the highest 20-day average
#                  dollar volume up to that day (live
#                  UNIVERSE_SOURCE=SP500_TOP200)
#
# The top-200 membership is rebuilt every day from trailing data
# only, as the live universe builder does each morning, so it
# has no look-ahead. The engine then picks its daily 200-stock
# universe from the combined list, as live.
#
# LIMITATIONS:
#   - Only stocks listed today are available, so stocks that were
#     delisted (often after collapsing) are missing. This
#     survivorship bias is larger for the added stocks than for
#     the S&P 500.
#   - Sectors come from Yahoo Finance today, not point in time.
#   - Stocks without a known sector are left out, as live.
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[1]

for folder in (ROOT_DIR, ROOT_DIR / "backtests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import backtest_holdout_validation as hv
from project_paths import BACKTEST_RESULTS_DIR
from us_market_universe import (
    MIN_AVG_DOLLAR_VOLUME_20,
    MIN_HISTORY_BARS,
    MIN_PRICE,
    TOP_TRADED_COUNT,
    load_us_listed_stocks,
    lookup_sectors,
)

PRICE_CACHE = BACKTEST_RESULTS_DIR / "us_stock_prices_cache.pkl"
PRICE_CACHE_MAX_AGE_HOURS = 24

COMPARISON_CSV = BACKTEST_RESULTS_DIR / "top200_universe_comparison.csv"
TRADES_CSV = BACKTEST_RESULTS_DIR / "top200_universe_trades.csv"
MEMBERSHIP_CSV = BACKTEST_RESULTS_DIR / "top200_universe_members.csv"

# Look up sectors for stocks that ever ranked this deep, so enough
# remain for a full 200 after stocks without a sector are dropped.
SECTOR_LOOKUP_DEPTH = int(TOP_TRADED_COUNT * 1.5)

COMPARE_FIELDS = [
    "total_trades",
    "trades_from_added_stocks",
    "win_rate_pct",
    "average_win_r",
    "average_loss_r",
    "expectancy_r",
    "profit_factor",
    "total_return_pct",
    "cagr_pct",
    "max_drawdown_pct",
    "max_consecutive_losses",
    "average_holding_days",
    "market_exposure_pct",
]


def download_prices(tickers):
    """10 years of daily bars per ticker, cached for a day."""
    if PRICE_CACHE.exists():
        age_hours = (time.time() - PRICE_CACHE.stat().st_mtime) / 3600
        if age_hours < PRICE_CACHE_MAX_AGE_HOURS:
            with PRICE_CACHE.open("rb") as file:
                cached = pickle.load(file)
            if set(tickers) <= set(cached["requested"]):
                print(f"Using price cache ({age_hours:.1f} hours old).")
                return cached["frames"]

    frames = hv.download_many(tickers, "US stocks")
    frames = {ticker: frame.astype("float32") for ticker, frame in frames.items()}
    with PRICE_CACHE.open("wb") as file:
        pickle.dump({"requested": list(tickers), "frames": frames}, file)
    return frames


def dollar_volume_ranks(frames, tickers):
    """Daily rank by trailing 20-day dollar volume; NaN when ineligible."""
    close = pd.DataFrame({t: frames[t]["Close"] for t in tickers})
    volume = pd.DataFrame({t: frames[t]["Volume"] for t in tickers})
    close.index = pd.to_datetime(close.index).normalize()
    volume.index = close.index

    dollar_volume = (close * volume).rolling(20).mean()
    eligible = (
        (close >= MIN_PRICE)
        & (dollar_volume >= MIN_AVG_DOLLAR_VOLUME_20)
        & (close.notna().cumsum() >= MIN_HISTORY_BARS)
    )
    return dollar_volume.where(eligible).rank(
        axis=1, ascending=False, method="first"
    )


def build_top_traded_membership(frames, candidates, calendar):
    """(Date, ticker) pairs in each day's top 200, plus their sectors."""
    ranks = dollar_volume_ranks(frames, candidates).reindex(calendar)
    ever_deep = ranks.columns[(ranks <= SECTOR_LOOKUP_DEPTH).any()].tolist()
    print(f"Stocks ever ranked in the top {SECTOR_LOOKUP_DEPTH}: {len(ever_deep)}")

    sectors = lookup_sectors(ever_deep, known={})
    with_sector = [t for t in ever_deep if sectors[t]]
    print(f"With a known sector: {len(with_sector)}")

    # Re-rank among stocks with a sector, as the live builder does.
    ranks = dollar_volume_ranks(frames, with_sector).reindex(calendar)
    in_top = ranks <= TOP_TRADED_COUNT
    members = in_top.stack()
    members = members[members].index.to_frame(index=False)
    members.columns = ["Date", "ticker"]
    return members, {t: sectors[t] for t in with_sector}


def run_period(name, calendar, candidates, stock_dfs, spy_df, sp500_tickers):
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
    summary["trades_from_added_stocks"] = sum(
        1 for trade in result["trades"] if trade["ticker"] not in sp500_tickers
    )
    return summary, result["trades"]


def main():
    print()
    print("================================")
    print("S&P 500 + TOP-200 MOST-TRADED UNIVERSE BACKTEST")
    print("================================")

    sp500 = hv.load_sp500_constituents()
    sp500_tickers = set(sp500["ticker"])
    listed = load_us_listed_stocks()
    others = listed[~listed["ticker"].isin(sp500_tickers)]
    print(f"Other US-listed common stocks: {len(others)}")

    frames = download_prices(sorted(sp500_tickers | set(others["ticker"])))
    market_raw = hv.download_many(
        ["SPY", *hv.SECTOR_ETFS.values()], "SPY + sector ETFs"
    )
    if "SPY" not in market_raw:
        raise RuntimeError("SPY data missing.")
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

    candidates_others = [t for t in others["ticker"] if t in frames]
    members, sectors = build_top_traded_membership(
        frames, candidates_others, pd.DatetimeIndex(full_calendar).normalize()
    )
    members.to_csv(MEMBERSHIP_CSV, index=False)
    daily_counts = members.groupby("Date").size()
    print(
        f"Added stocks per day: median {daily_counts.median():.0f}, "
        f"min {daily_counts.min()}; {members['ticker'].nunique()} different "
        "stocks over the period"
    )

    names = dict(zip(others["ticker"], others["company"]))
    added = pd.DataFrame(
        [
            {"ticker": t, "company": names.get(t, t), "sector": sector}
            for t, sector in sectors.items()
            if t in set(members["ticker"])
        ]
    )
    constituents = pd.concat([sp500, added], ignore_index=True)
    stock_raw = {t: frames[t].astype("float64") for t in constituents["ticker"] if t in frames}
    del frames

    stock_dfs, panel = hv.prepare_stock_data(constituents, stock_raw)
    del stock_raw

    is_sp500 = panel["ticker"].isin(sp500_tickers)
    member_keys = pd.MultiIndex.from_frame(members[["Date", "ticker"]])
    is_member = pd.MultiIndex.from_frame(panel[["Date", "ticker"]]).isin(member_keys)
    panels = {
        "SP500": panel[is_sp500],
        "SP500_TOP200": panel[is_sp500 | is_member],
    }

    summaries = []
    all_trades = []
    for universe, universe_panel in panels.items():
        print()
        print(f"Building daily candidates for {universe}...")
        candidates = hv.build_candidate_stream(
            panel=universe_panel,
            spy_df=spy_df,
            sector_dfs=sector_dfs,
            signal_dates=full_calendar[:-1],
        )
        candidates["signal_date"] = pd.to_datetime(candidates["signal_date"])
        for period, period_calendar in (
            ("DEVELOPMENT", development_calendar),
            ("HOLDOUT", holdout_calendar),
            ("FULL", full_calendar),
        ):
            summary, trades = run_period(
                period, period_calendar, candidates, stock_dfs, spy_df, sp500_tickers
            )
            summary["strategy"] = universe
            summaries.append(summary)
            if period == "FULL":
                for trade in trades:
                    trade["universe"] = universe
                    trade["added_stock"] = trade["ticker"] not in sp500_tickers
                all_trades.extend(trades)

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
    comparison = comparison.sort_values(
        ["period_type", "strategy"], key=lambda s: s.map(
            {"DEVELOPMENT": 0, "HOLDOUT": 1, "FULL": 2}
        ).fillna(s)
    )
    comparison.to_csv(COMPARISON_CSV, index=False)
    pd.DataFrame(all_trades).to_csv(TRADES_CSV, index=False)

    print()
    print("================================")
    print("RESULTS")
    print("================================")
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(comparison.to_string(index=False))

    trades = pd.DataFrame(all_trades)
    if not trades.empty:
        added_trades = trades[(trades["universe"] == "SP500_TOP200") & trades["added_stock"]]
        if not added_trades.empty:
            print()
            print("Trades on added (non-S&P) stocks, full period:")
            print(
                f"  {len(added_trades)} trades, "
                f"win rate {np.mean(added_trades['pnl'] > 0) * 100:.1f}%, "
                f"average {added_trades['r_multiple'].mean():+.2f}R"
            )

    print()
    print("Saved:")
    for path in (COMPARISON_CSV, TRADES_CSV, MEMBERSHIP_CSV):
        print(path)

    print()
    print("IMPORTANT")
    print(
        "Survivorship bias is larger for the added stocks (delisted "
        "losers are missing). Treat a win for SP500_TOP200 as an upper bound."
    )


if __name__ == "__main__":
    main()
