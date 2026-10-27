import sys
import time
from datetime import datetime, time as clock_time
from pathlib import Path

import pandas as pd

# ============================================================
# BAD-NEWS BLOCK BACKTEST
#
# Compares the strict dual-setup strategy with and without the
# live bad-news rule (news_checker.py): skip any A+ stock with a
# negative headline in the NEWS_LOOKBACK_TRADING_DAYS before the
# entry-day open. When the best stock is blocked, the next-best
# A+ stock is checked, matching the live master selector.
#
# Same periods, data, and execution as
# backtest_holdout_validation.py (development + 2-year holdout).
#
# Requires real Alpaca API keys in .env (free account). News is
# only requested for daily best candidates, and results are
# cached so reruns do not call the API again.
#
# LIMITATIONS:
#   - Keyword matching on headlines, same as live.
#   - Today's S&P 500 membership is used historically, so
#     survivorship bias remains.
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[1]

for folder in (ROOT_DIR, ROOT_DIR / "backtests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import backtest_holdout_validation as hv
from backtest_earnings_blackout import COMPARE_FIELDS, run_period
from news_checker import (
    MARKET_TIMEZONE,
    NEWS_LOOKBACK_TRADING_DAYS,
    STATUS_BAD_NEWS,
    STATUS_UNKNOWN,
    _alpaca_keys,
    check_news,
)
from project_paths import BACKTEST_RESULTS_DIR

MAX_REPLACEMENT_TRIES = 5
# Free Alpaca data plan allows 200 requests per minute.
SECONDS_BETWEEN_REQUESTS = 0.35

NEWS_CACHE_CSV = BACKTEST_RESULTS_DIR / "news_checks_cache.csv"
COMPARISON_CSV = BACKTEST_RESULTS_DIR / "bad_news_block_comparison.csv"
NEWS_BLOCK_TRADES_CSV = BACKTEST_RESULTS_DIR / "bad_news_block_trades.csv"
BLOCKED_SIGNALS_CSV = BACKTEST_RESULTS_DIR / "bad_news_blocked_signals.csv"

CACHE_FIELDS = [
    "ticker",
    "signal_date",
    "status",
    "category",
    "headline",
    "articles_checked",
]


class NewsCache:
    """News check per (ticker, signal date), persisted between runs."""

    def __init__(self, next_session):
        self.next_session = next_session
        self.rows = {}
        self.api_calls = 0
        self.unknown = 0
        if NEWS_CACHE_CSV.exists():
            frame = pd.read_csv(NEWS_CACHE_CSV, dtype=str).fillna("")
            for row in frame.to_dict(orient="records"):
                self.rows[(row["ticker"], row["signal_date"])] = row

    def status(self, ticker, signal_date):
        key = (ticker, pd.Timestamp(signal_date).date().isoformat())
        if key in self.rows:
            return self.rows[key]

        # Live scan runs before the open of the entry day.
        entry_day = self.next_session[pd.Timestamp(signal_date)].date()
        as_of = datetime.combine(
            entry_day, clock_time(9, 30), tzinfo=MARKET_TIMEZONE
        )
        time.sleep(SECONDS_BETWEEN_REQUESTS)
        self.api_calls += 1
        if self.api_calls % 100 == 0:
            print(f"News API calls: {self.api_calls}")

        result = check_news(ticker, as_of=as_of)
        first = (result["bad_headlines"] or [{}])[0]
        row = {
            "ticker": key[0],
            "signal_date": key[1],
            "status": result["status"],
            "category": first.get("category", ""),
            "headline": first.get("headline", ""),
            "articles_checked": str(result["articles_checked"]),
        }
        if result["status"] == STATUS_UNKNOWN:
            # Not cached so a rerun retries it; treated as not blocked.
            self.unknown += 1
            print(f"News lookup failed for {ticker} {key[1]}: {result['detail']}")
        else:
            self.rows[key] = row
        return row

    def save(self):
        pd.DataFrame(
            list(self.rows.values()), columns=CACHE_FIELDS
        ).to_csv(NEWS_CACHE_CSV, index=False)


def build_candidate_stream(panel, spy_df, sector_dfs, signal_dates, news):
    """Daily best and best-without-bad-news candidates in one pass."""
    panel_subset = panel[panel["Date"].isin(signal_dates)]
    panel_by_date = dict(tuple(panel_subset.groupby("Date")))

    baseline_rows = []
    filtered_rows = []
    blocked_rows = []

    print()
    print("Building daily candidates with and without the bad-news block...")

    for index, date_value in enumerate(signal_dates, start=1):
        if index % 250 == 0:
            print(f"Processed {index} / {len(signal_dates)} dates")
            news.save()

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

        check = news.status(best["ticker"], date_value)
        best["news_status"] = check["status"]
        best["news_category"] = check["category"]
        best["news_headline"] = check["headline"]
        baseline_rows.append(best)

        candidate = best
        excluded = set()
        tries = 0
        while (
            candidate is not None
            and candidate.get("news_status") == STATUS_BAD_NEWS
        ):
            blocked_rows.append(candidate)
            excluded.add(candidate["ticker"])
            tries += 1
            if tries > MAX_REPLACEMENT_TRIES:
                candidate = None
                break
            candidate = hv.get_daily_best_candidate(
                date_value=date_value,
                daily_stocks=daily,
                sector_state=sector_state,
                exclude_tickers=excluded,
            )
            if candidate is not None:
                check = news.status(candidate["ticker"], date_value)
                candidate["news_status"] = check["status"]
                candidate["news_category"] = check["category"]
                candidate["news_headline"] = check["headline"]

        if candidate is not None:
            filtered_rows.append(candidate)

    news.save()

    def to_frame(rows):
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame["signal_date"] = pd.to_datetime(frame["signal_date"])
        return frame

    return to_frame(baseline_rows), to_frame(filtered_rows), to_frame(blocked_rows)


def bad_news_trade_outcomes(trades):
    """Average R of baseline trades entered despite bad news."""
    frame = pd.DataFrame(trades)
    if frame.empty or "news_status" not in frame:
        return {}
    bad = frame[frame["news_status"] == STATUS_BAD_NEWS]["r_multiple"]
    rest = frame[frame["news_status"] != STATUS_BAD_NEWS]["r_multiple"]
    return {
        "bad_news_trades": len(bad),
        "bad_news_avg_r": round(bad.mean(), 3) if len(bad) else None,
        "other_trades_avg_r": round(rest.mean(), 3) if len(rest) else None,
    }


def main():
    print()
    print("================================")
    print("BAD-NEWS BLOCK BACKTEST")
    print("================================")
    print(
        f"Rule: skip A+ stocks with a negative headline in the last "
        f"{NEWS_LOOKBACK_TRADING_DAYS} trading days before entry."
    )

    # Fail fast before the long price download.
    _alpaca_keys()

    constituents = hv.load_sp500_constituents()
    stock_raw = hv.download_many(
        constituents["ticker"].tolist(), "S&P 500 stocks"
    )
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

    next_session = dict(zip(full_calendar[:-1], full_calendar[1:]))
    news = NewsCache(next_session)

    baseline, filtered, blocked_signals = build_candidate_stream(
        panel=panel,
        spy_df=spy_df,
        sector_dfs=sector_dfs,
        signal_dates=full_calendar[:-1],
        news=news,
    )
    blocked_signals.to_csv(BLOCKED_SIGNALS_CSV, index=False)

    summaries = []
    filtered_trades = []
    for period, period_calendar in (
        ("DEVELOPMENT", development_calendar),
        ("HOLDOUT", holdout_calendar),
        ("FULL", full_calendar),
    ):
        for strategy, candidates in (
            ("BASELINE", baseline),
            ("BAD_NEWS_BLOCK", filtered),
        ):
            summary, trades = run_period(
                period, period_calendar, candidates, stock_dfs, spy_df
            )
            summary["strategy"] = strategy
            if strategy == "BASELINE":
                summary.update(bad_news_trade_outcomes(trades))
            summaries.append(summary)
            if strategy == "BAD_NEWS_BLOCK" and period == "FULL":
                filtered_trades = trades

    comparison = pd.DataFrame(summaries)
    columns = [
        "period_type",
        "strategy",
        "start_date",
        "end_date",
        *COMPARE_FIELDS,
        "bad_news_trades",
        "bad_news_avg_r",
        "other_trades_avg_r",
    ]
    comparison = comparison[[c for c in columns if c in comparison.columns]]
    comparison.to_csv(COMPARISON_CSV, index=False)
    pd.DataFrame(filtered_trades).to_csv(NEWS_BLOCK_TRADES_CSV, index=False)

    print()
    print("================================")
    print("RESULTS")
    print("================================")
    print(f"Candidate days blocked by bad news: {len(blocked_signals)}")
    print(f"News API calls this run: {news.api_calls}")
    print(f"News lookups that failed (treated as not blocked): {news.unknown}")
    if not blocked_signals.empty:
        print()
        print("Blocked categories:")
        print(blocked_signals["news_category"].value_counts().to_string())
    print()
    with pd.option_context("display.width", 220, "display.max_columns", 20):
        print(comparison.to_string(index=False))

    print()
    print("Saved:")
    for path in (
        COMPARISON_CSV,
        NEWS_BLOCK_TRADES_CSV,
        BLOCKED_SIGNALS_CSV,
        NEWS_CACHE_CSV,
    ):
        print(path)

    print()
    print("IMPORTANT")
    print(
        "The holdout has already been inspected for the base strategy. "
        "Judge this rule on all periods together, not the holdout alone. "
        "Review the blocked headlines for false positives."
    )


if __name__ == "__main__":
    main()
