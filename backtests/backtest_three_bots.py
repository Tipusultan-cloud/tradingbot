import bisect
import math
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# THREE BOTS, HEAD TO HEAD
#
# Bot 1 (A+), Bot 2 (leader pullback) and Bot 3 (momentum), each
# alone with the same money and sizing, using the live exits:
#
#   Bot 1 / Bot 2: 1.5 ATR stop; half at 2R, then the stop moves
#     to the entry; the rest at 2.5R or after 20 sessions. Sold at
#     the open when earnings are today or next session; a stock
#     with earnings within 2 sessions of the scan is not bought.
#   Bot 3: 1.5 ATR stop, then 3 ATR (ATR at entry) below the highest
#     close; no target, no time limit, rides through earnings.
#
# Bot 1's final target became 3R after this test (2026-11-06), on the
# result of backtest_bot1_levers.py; TARGET_R here stays 2.5R so the
# saved three_bots_*.csv results can be reproduced.
#
# Same execution as backtest_pullback_slots.py: entry at the next
# open, stop checked first, 0.05% slippage each side, 1% of total
# equity risked per trade, profits compound, start $7,000.
#
# LIMITATIONS:
#   - Only stocks listed today are available and today's S&P 500
#     membership is used historically. Stocks that collapsed and
#     were delisted are missing, which flatters all three bots and
#     Bot 3's added stocks most.
#   - Bot 3's daily top 200 is ranked without the live rule that
#     drops stocks with no known sector (no sector history).
#   - No bad-news block and no sector limit (no history for them).
#   - Earnings dates have no before / after market time, so the
#     earnings rules run by date only.
#   - SPY parking of idle cash is left out, so idle cash earns 0.
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[1]

for folder in (ROOT_DIR, ROOT_DIR / "backtests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import backtest_pullback_slots as bp
import backtest_top200_universe as top200
from project_paths import BACKTEST_RESULTS_DIR

hv = bp.hv

STARTING_EQUITY = bp.STARTING_EQUITY
PARTIAL_R = 2.0
TARGET_R = 2.5
# Per-bot override of TARGET_R, for tests of the mixed live setup.
TARGET_BY_SOURCE = {}
# Move the stop to the entry once the high reaches this R (None = only
# after the half sells). Bots 1 and 2; it applies from the next session.
BREAKEVEN_R = None
TRAIL_ATR = 3.0
EARNINGS_EXIT_DAYS = 1
EARNINGS_SKIP_DAYS = 2
HIGH_SLIPPAGE_PCT = 0.25 / 100

MIN_JUMP_PCT = 5.0
MIN_VOLUME_RATIO = 3.0
MIN_CLOSE_POSITION = 0.75
MOMENTUM_PER_DAY = 5

SOURCE_MOMENTUM = "MOMENTUM"

PRICE_CACHE = BACKTEST_RESULTS_DIR / "three_bots_prices.pkl"
PRICE_CACHE_MAX_AGE_HOURS = 24
EARNINGS_CSV = BACKTEST_RESULTS_DIR / "earnings_dates_cache.csv"
COMPARISON_CSV = BACKTEST_RESULTS_DIR / "three_bots_comparison.csv"
TRADES_CSV = BACKTEST_RESULTS_DIR / "three_bots_trades.csv"
YEARLY_CSV = BACKTEST_RESULTS_DIR / "three_bots_yearly.csv"


class EarningsCalendar:
    def __init__(self, dates_by_ticker):
        self.dates = {t: sorted(d) for t, d in dates_by_ticker.items()}

    def days_until(self, ticker, as_of):
        """Weekdays until the next report on or after as_of, or None."""
        dates = self.dates.get(ticker)
        if not dates:
            return None
        day = pd.Timestamp(as_of).date()
        index = bisect.bisect_left(dates, day)
        if index == len(dates):
            return None
        return int(np.busday_count(day, dates[index]))


def load_earnings():
    frame = pd.read_csv(EARNINGS_CSV, parse_dates=["earnings_date"])
    frame["earnings_date"] = frame["earnings_date"].dt.date
    return EarningsCalendar(frame.groupby("ticker")["earnings_date"].apply(list).to_dict())


def load_prices(tickers):
    """Daily bars from bp.DOWNLOAD_START, cached for a day."""
    if PRICE_CACHE.exists():
        age_hours = (time.time() - PRICE_CACHE.stat().st_mtime) / 3600
        if age_hours < PRICE_CACHE_MAX_AGE_HOURS:
            with PRICE_CACHE.open("rb") as file:
                print(f"Using price cache ({age_hours:.1f} hours old).")
                return pickle.load(file)
    frames = hv.download_many(tickers, "US stocks")
    frames = {t: frame.astype("float32") for t, frame in frames.items()}
    with PRICE_CACHE.open("wb") as file:
        pickle.dump(frames, file)
    return frames


def listed_tickers():
    """The US stock list saved with the earlier price cache."""
    with top200.PRICE_CACHE.open("rb") as file:
        return pickle.load(file)["requested"]


def momentum_signals(ticker, frame):
    """Rows of `frame` that pass the Bot 3 scan (scanner_momentum.py)."""
    frame = frame.dropna(subset=["Close"]).astype("float64")
    close, high, low, volume = (frame[c] for c in ("Close", "High", "Low", "Volume"))
    previous = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1)
    average_volume = volume.shift(1).rolling(20).mean()
    day_range = high - low
    table = pd.DataFrame({
        "jump_pct": (close / previous - 1) * 100,
        "volume_ratio": volume / average_volume.where(average_volume > 0),
        "close_position": (close - low) / day_range.where(day_range > 0),
        "above_sma200": close > close.rolling(200).mean(),
        "atr": true_range.rolling(14).mean(),
    })
    hits = table[
        (table["jump_pct"] >= MIN_JUMP_PCT)
        & (table["volume_ratio"] >= MIN_VOLUME_RATIO)
        & (table["close_position"] >= MIN_CLOSE_POSITION)
        & table["above_sma200"]
        & table["atr"].notna()
    ]
    return hits.assign(ticker=ticker)


def build_momentum_candidates(frames, sp500, in_top, signal_dates):
    pieces = [momentum_signals(ticker, frame) for ticker, frame in frames.items()]
    hits = pd.concat([p for p in pieces if not p.empty])
    hits.index = pd.to_datetime(hits.index).normalize()
    hits = hits[hits.index.isin(signal_dates)]
    hits = hits.rename_axis("signal_date").reset_index()

    members = in_top.stack()
    members = set(members[members].index)
    keep = [
        row.ticker in sp500 or (row.signal_date, row.ticker) in members
        for row in hits.itertuples()
    ]
    hits = hits[keep].sort_values(["signal_date", "volume_ratio"], ascending=[True, False])
    hits = hits.groupby("signal_date").head(MOMENTUM_PER_DAY)
    return pd.DataFrame({
        "signal_date": hits["signal_date"].values,
        "ticker": hits["ticker"].values,
        "atr": hits["atr"].values,
        "source": SOURCE_MOMENTUM,
    })


def simulate(lookup, calendar, stock_dfs, slots, earnings=None, slippage=hv.SLIPPAGE_PCT):
    """Returns (trades, equity, share of days with a position open)."""
    cash = STARTING_EQUITY
    positions = []
    pending = []
    trades = []
    equity = []
    days_invested = 0
    calendar_index = {date: i for i, date in enumerate(calendar)}

    def bar(ticker, date_value):
        return hv.get_bar(stock_dfs, ticker, date_value)

    def sell(position, shares, price):
        nonlocal cash
        cash += shares * price
        position["proceeds"] += shares * price
        position["shares_open"] -= shares

    def close(position, date_value, price, reason):
        sell(position, position["shares_open"], price)
        positions.remove(position)
        pnl = position["proceeds"] - position["entry_cost"]
        if position["partial_price"] is not None:
            reason = f"PARTIAL+{reason}"
        trades.append({
            "ticker": position["ticker"],
            "source": position["source"],
            "entry_date": position["entry_date"],
            "exit_date": date_value,
            "entry_price": position["entry_price"],
            "partial_price": position["partial_price"],
            "exit_price": price,
            "shares": position["shares"],
            "pnl": pnl,
            "r_multiple": pnl / position["planned_risk"],
            "holding_days": calendar_index[date_value] - position["entry_index"] + 1,
            "exit_reason": reason,
        })

    def take_partial(position, price):
        sell(position, position["shares"] // 2, price)
        position["partial_price"] = price
        position["stop"] = max(position["stop"], position["entry_price"])

    def stop_name(position):
        return "TRAIL_STOP" if position["stop"] > position["initial_stop"] else "STOP"

    for i, date_value in enumerate(calendar):
        # 1. Exits at the open: gaps through a level, earnings.
        for position in positions[:]:
            today = bar(position["ticker"], date_value)
            if today is None:
                continue
            open_price = float(today["Open"])
            sold_at_open = open_price * (1 - slippage)
            if open_price <= position["stop"]:
                close(position, date_value, sold_at_open, f"GAP_{stop_name(position)}")
                continue
            if earnings and not position["trail"]:
                days = earnings.days_until(position["ticker"], date_value)
                if days is not None and days <= EARNINGS_EXIT_DAYS:
                    close(position, date_value, sold_at_open, "EARNINGS_EXIT")
                    continue
            if position["partial_level"] and position["partial_price"] is None \
                    and open_price >= position["partial_level"]:
                take_partial(position, sold_at_open)
            if position["target"] and open_price >= position["target"]:
                close(position, date_value, sold_at_open, "GAP_TARGET")

        # 2. Entries at the open, sized on total equity.
        if pending:
            equity_now = cash
            for position in positions:
                today = bar(position["ticker"], date_value)
                mark = float(today["Open"]) if today is not None else position["last_close"]
                equity_now += position["shares_open"] * mark
            for signal in pending:
                if len(positions) >= slots:
                    break
                today = bar(signal["ticker"], date_value)
                if today is None:
                    continue
                entry_price = float(today["Open"]) * (1 + slippage)
                stop_distance = signal["atr"] * hv.ATR_MULTIPLIER
                shares = min(
                    math.floor(equity_now * hv.RISK_PER_TRADE_PCT / 100 / stop_distance),
                    math.floor(cash / entry_price),
                )
                if shares <= 0:
                    continue
                cash -= shares * entry_price
                trail = signal["source"] == SOURCE_MOMENTUM
                positions.append({
                    "ticker": signal["ticker"],
                    "source": signal["source"],
                    "trail": trail,
                    "atr": signal["atr"],
                    "entry_date": date_value,
                    "entry_index": i,
                    "entry_price": entry_price,
                    "entry_cost": shares * entry_price,
                    "shares": shares,
                    "shares_open": shares,
                    "proceeds": 0.0,
                    "planned_risk": shares * stop_distance,
                    "initial_stop": entry_price - stop_distance,
                    "stop": entry_price - stop_distance,
                    "target": None if trail else entry_price + stop_distance * TARGET_BY_SOURCE.get(signal["source"], TARGET_R),
                    "partial_level": None if trail else entry_price + stop_distance * PARTIAL_R,
                    "partial_price": None,
                    "highest_close": entry_price,
                    "last_close": entry_price,
                })
            pending = []

        # 3. Intraday exits (stop first), time exit at the close, then
        #    raise the trailing stop for the next session.
        for position in positions[:]:
            today = bar(position["ticker"], date_value)
            if today is None:
                continue
            high, low, last = (float(today[c]) for c in ("High", "Low", "Close"))
            position["last_close"] = last
            if low <= position["stop"]:
                close(position, date_value, position["stop"] * (1 - slippage), stop_name(position))
                continue
            if position["partial_level"] and position["partial_price"] is None \
                    and high >= position["partial_level"]:
                take_partial(position, position["partial_level"] * (1 - slippage))
            if position["target"] and high >= position["target"]:
                close(position, date_value, position["target"] * (1 - slippage), "TARGET")
                continue
            risk = position["entry_price"] - position["initial_stop"]
            if BREAKEVEN_R is not None and not position["trail"]                     and high >= position["entry_price"] + BREAKEVEN_R * risk:
                position["stop"] = max(position["stop"], position["entry_price"])
            if position["trail"]:
                position["highest_close"] = max(position["highest_close"], last)
                position["stop"] = max(
                    position["stop"], position["highest_close"] - TRAIL_ATR * position["atr"]
                )
            elif i - position["entry_index"] + 1 >= hv.MAX_HOLD_DAYS:
                close(position, date_value, last * (1 - slippage), "TIME_EXIT")

        # 4. Mark equity at the close.
        days_invested += bool(positions)
        equity.append(cash + sum(p["shares_open"] * p["last_close"] for p in positions))

        # 5. After the close, queue next-day entries for free slots.
        if i < len(calendar) - 1:
            held = {p["ticker"] for p in positions}
            free = slots - len(positions)
            for candidate in lookup.get(date_value, []):
                if free <= 0:
                    break
                if candidate["ticker"] in held:
                    continue
                if earnings and candidate["source"] != SOURCE_MOMENTUM:
                    days = earnings.days_until(candidate["ticker"], date_value)
                    if days is not None and days <= EARNINGS_SKIP_DAYS:
                        continue
                pending.append(candidate)
                held.add(candidate["ticker"])
                free -= 1

    for position in positions[:]:
        close(position, calendar[-1], position["last_close"] * (1 - slippage), "END_OF_TEST")
    equity[-1] = cash
    return pd.DataFrame(trades), pd.Series(equity, index=calendar), days_invested / len(calendar)


def summarize(trades, equity, exposure):
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    final = equity.iloc[-1]
    drawdown = (equity / equity.cummax() - 1).min() * -100
    cagr = ((final / STARTING_EQUITY) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    row = {
        "trades": len(trades),
        "total_return_pct": round((final / STARTING_EQUITY - 1) * 100, 1),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(drawdown, 1),
        "return_over_drawdown": round(cagr / drawdown, 2) if drawdown else None,
        "exposure_pct": round(exposure * 100, 1),
    }
    if trades.empty:
        return row
    wins = trades.loc[trades["pnl"] > 0, "pnl"].sum()
    losses = -trades.loc[trades["pnl"] <= 0, "pnl"].sum()
    top3 = trades["pnl"].nlargest(3).sum()
    return {
        **row,
        "win_rate_pct": round((trades["pnl"] > 0).mean() * 100, 1),
        "expectancy_r": round(trades["r_multiple"].mean(), 3),
        "profit_factor": round(wins / losses, 2) if losses else None,
        "best_r": round(trades["r_multiple"].max(), 1),
        "worst_r": round(trades["r_multiple"].min(), 1),
        "avg_hold_days": round(trades["holding_days"].mean(), 1),
        "pnl_usd": round(final - STARTING_EQUITY, 2),
        "pnl_without_top3_usd": round(final - STARTING_EQUITY - top3, 2),
    }


def buy_and_hold(spy_df, calendar):
    closes = spy_df["Close"].reindex(calendar).ffill()
    equity = closes / closes.iloc[0] * STARTING_EQUITY
    return summarize(pd.DataFrame(), equity, 1.0), equity


def main():
    print()
    print("================================")
    print("THREE BOTS, HEAD TO HEAD")
    print("================================")

    constituents = hv.load_sp500_constituents()
    sp500 = set(constituents["ticker"])
    market_raw = hv.download_many(["SPY", *hv.SECTOR_ETFS.values()], "SPY + sector ETFs")
    frames = load_prices(sorted(sp500 | set(listed_tickers())))
    print(f"Stocks with prices: {len(frames)} (S&P 500: {len(sp500 & set(frames))})")

    spy_df = hv.add_indicators(market_raw["SPY"])
    sector_dfs = {
        etf: hv.add_indicators(market_raw[etf])
        for etf in hv.SECTOR_ETFS.values() if etf in market_raw
    }
    calendar = spy_df.index
    final_date = calendar.max()
    full = calendar[calendar >= final_date - pd.DateOffset(years=bp.BACKTEST_YEARS)]
    signal_dates = full[:-1]
    print(f"Test window: {full[0].date()} to {full[-1].date()}")

    sp500_raw = {t: frames[t].dropna(subset=["Close"]).astype("float64")
                 for t in sp500 if t in frames}
    stock_dfs, panel = hv.prepare_stock_data(constituents, sp500_raw)
    panel = panel[panel["AVG_VOLUME20"] > 0]

    a_plus = bp.build_a_plus_candidates(panel, spy_df, sector_dfs, signal_dates)
    pullback = bp.build_pullback_candidates(panel, set(signal_dates))
    del panel

    others = [t for t in frames if t not in sp500]
    ranks = top200.dollar_volume_ranks(frames, others).reindex(
        pd.DatetimeIndex(full).normalize()
    )
    momentum = build_momentum_candidates(
        frames, sp500, ranks <= top200.TOP_TRADED_COUNT, set(signal_dates)
    )
    del ranks
    for ticker in set(momentum["ticker"]) - set(stock_dfs):
        stock_dfs[ticker] = frames[ticker].dropna(subset=["Close"]).astype("float64")
    del frames
    print(f"Signals: Bot 1 {len(a_plus)}, Bot 2 {len(pullback)}, Bot 3 {len(momentum)}")

    earnings = load_earnings()
    bots = {
        "Bot 1 A+ Hunter": a_plus,
        "Bot 2 Dip Buyer": pullback,
        "Bot 3 Momentum Rider": momentum,
    }
    momentum_sp500 = momentum[momentum["ticker"].isin(sp500)]
    variants = {name: (bp.ranked(frame), 1, earnings, hv.SLIPPAGE_PCT)
                for name, frame in bots.items()}
    for name, frame in bots.items():
        short = name[:5]
        variants[f"{short} 2 slots"] = (bp.ranked(frame), 2, earnings, hv.SLIPPAGE_PCT)
        variants[f"{short} slippage 0.25%"] = (bp.ranked(frame), 1, earnings, HIGH_SLIPPAGE_PCT)
    for name in list(bots)[:2]:
        variants[f"{name[:5]} no earnings rules"] = (
            bp.ranked(bots[name]), 1, None, hv.SLIPPAGE_PCT
        )
    variants["Bot 3 S&P 500 only"] = (bp.ranked(momentum_sp500), 1, earnings, hv.SLIPPAGE_PCT)

    periods = {
        "FULL": full,
        "FIRST 5 YEARS": full[full < final_date - pd.DateOffset(years=5)],
        "LAST 5 YEARS": full[full >= final_date - pd.DateOffset(years=5)],
        "LAST 2 YEARS": full[full >= final_date - pd.DateOffset(years=2)],
        "LAST 12 MONTHS": full[full >= final_date - pd.DateOffset(months=12)],
    }

    rows = []
    all_trades = []
    yearly = {}
    for period, period_calendar in periods.items():
        spy_summary, spy_equity = buy_and_hold(spy_df, period_calendar)
        rows.append({"period": period, "variant": "SPY buy and hold", **spy_summary})
        if period == "FULL":
            year_end = spy_equity.groupby(spy_equity.index.year).last()
            starts = year_end.shift(1).fillna(STARTING_EQUITY)
            yearly["SPY buy and hold"] = ((year_end / starts - 1) * 100).round(1)
        for variant, (lookup, slots, calendar_rules, slippage) in variants.items():
            trades, equity, exposure = simulate(
                lookup, period_calendar, stock_dfs, slots, calendar_rules, slippage
            )
            rows.append({"period": period, "variant": variant,
                         **summarize(trades, equity, exposure)})
            if period == "FULL" and variant in bots:
                all_trades.append(trades.assign(variant=variant))
                year_end = equity.groupby(equity.index.year).last()
                starts = year_end.shift(1).fillna(STARTING_EQUITY)
                yearly[variant] = ((year_end / starts - 1) * 100).round(1)

    comparison = pd.DataFrame(rows)
    comparison.to_csv(COMPARISON_CSV, index=False)
    pd.concat(all_trades).to_csv(TRADES_CSV, index=False)
    pd.DataFrame(yearly).to_csv(YEARLY_CSV, index_label="year")

    with pd.option_context("display.width", 250, "display.max_columns", 30):
        for period in periods:
            print()
            print(f"--- {period} ---")
            print(comparison[comparison["period"] == period].drop(columns=["period"])
                  .to_string(index=False))
        print()
        print("Return by year (%):")
        print(pd.DataFrame(yearly).to_string())
    print()
    print("Saved:", COMPARISON_CSV, TRADES_CSV, YEARLY_CSV, sep="\n")


if __name__ == "__main__":
    main()
