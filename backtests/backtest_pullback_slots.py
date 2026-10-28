import math
import sys
from pathlib import Path

import pandas as pd

# ============================================================
# LEADER-PULLBACK + TRADE-SLOTS BACKTEST
#
# Compares the current A+ bot with the leader-pullback entry
# (scanner_pullback.py) and with both combined in 1-3 trade
# slots (slot_selector.py):
#
#   Leader pullback: top 20% of the S&P 500 by 6-month return,
#   Close > SMA200, EMA20 > EMA50, and down more than 5% over
#   the last 5 sessions. Strongest 6-month leader first.
#
#   Slots: each day after the close, free slots are filled with
#   the A+ pick first, then pullbacks, skipping stocks held.
#
# Same execution as backtest_holdout_validation.py: entry at the
# next open, 1.5 ATR stop, 2R target, 20-day maximum hold, stop
# checked before target, 0.05% slippage each side. Each trade
# risks 1% of total equity (cash + open positions at the open),
# capped by available cash. Starts with $7,000; profits compound.
#
# LIMITATIONS:
#   - Today's S&P 500 membership is used historically. Stocks
#     that fell out of the index are missing, which flatters
#     dip-buying more than most rules.
#   - The pullback rule was chosen after looking at 2019-2026
#     data, so the later periods are not an untouched holdout.
#   - No bad-news block (no news history without Alpaca keys).
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[1]

for folder in (ROOT_DIR, ROOT_DIR / "backtests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import backtest_holdout_validation as hv
from project_paths import BACKTEST_RESULTS_DIR

BACKTEST_YEARS = 10
# 10 years of signals plus a year of indicator warm-up. yfinance has
# no 12-year period, so downloads use a fixed start date.
DOWNLOAD_START = "2015-01-01"
STARTING_EQUITY = 7000.0

LEADER_LOOKBACK_BARS = 126
LEADER_MIN_PERCENTILE = 0.80
PULLBACK_LOOKBACK_BARS = 5
PULLBACK_MAX_RETURN = -0.05
PULLBACKS_PER_DAY = 5

COMPARISON_CSV = BACKTEST_RESULTS_DIR / "pullback_slots_comparison.csv"
TRADES_CSV = BACKTEST_RESULTS_DIR / "pullback_slots_trades.csv"
YEARLY_CSV = BACKTEST_RESULTS_DIR / "pullback_slots_yearly.csv"


def download_from_start(*args, period=None, **kwargs):
    return original_download(*args, start=DOWNLOAD_START, **kwargs)


original_download = hv.yf.download
hv.yf.download = download_from_start


def build_pullback_candidates(panel, signal_dates):
    panel = panel.sort_values(["ticker", "Date"]).copy()
    close = panel.groupby("ticker")["Close"]
    panel["RETURN_6M"] = close.transform(
        lambda c: c / c.shift(LEADER_LOOKBACK_BARS) - 1
    )
    panel["RETURN_5D"] = close.transform(
        lambda c: c / c.shift(PULLBACK_LOOKBACK_BARS) - 1
    )
    panel["LEADER_PCT"] = panel.groupby("Date")["RETURN_6M"].rank(pct=True)

    setups = panel[
        panel["Date"].isin(signal_dates)
        & (panel["Close"] > panel["SMA200"])
        & (panel["EMA20"] > panel["EMA50"])
        & (panel["LEADER_PCT"] >= LEADER_MIN_PERCENTILE)
        & (panel["RETURN_5D"] < PULLBACK_MAX_RETURN)
        & panel["ATR"].notna()
    ].sort_values(["Date", "RETURN_6M"], ascending=[True, False])
    setups = setups.groupby("Date").head(PULLBACKS_PER_DAY)

    return pd.DataFrame({
        "signal_date": setups["Date"].values,
        "ticker": setups["ticker"].values,
        "sector": setups["sector"].values,
        "atr": setups["ATR"].values,
        "source": "PULLBACK",
    })


def build_a_plus_candidates(panel, spy_df, sector_dfs, signal_dates):
    panel_by_date = dict(
        tuple(panel[panel["Date"].isin(set(signal_dates))].groupby("Date"))
    )
    rows = []
    print()
    print("Building daily A+ candidates...")
    for date_value in signal_dates:
        daily = panel_by_date.get(date_value)
        if daily is None:
            continue
        state = hv.build_sector_state(
            date_value=date_value, spy_df=spy_df, sector_dfs=sector_dfs
        )
        best = hv.get_daily_best_candidate(
            date_value=date_value, daily_stocks=daily, sector_state=state
        )
        if best is not None:
            rows.append({**best, "source": "A_PLUS"})
    frame = pd.DataFrame(rows)
    frame["signal_date"] = pd.to_datetime(frame["signal_date"])
    return frame


def ranked(*frames):
    """Per signal date, candidates in priority order (earlier frames first)."""
    lookup = {}
    for frame in frames:
        for row in frame.to_dict(orient="records"):
            lookup.setdefault(row["signal_date"], []).append(row)
    return lookup


def simulate(lookup, calendar, stock_dfs, slots):
    cash = STARTING_EQUITY
    positions = []
    pending = []
    trades = []
    equity = []
    calendar_index = {date: i for i, date in enumerate(calendar)}

    def bar(ticker, date_value):
        return hv.get_bar(stock_dfs, ticker, date_value)

    def close(position, date_value, price, reason):
        nonlocal cash
        proceeds = position["shares"] * price
        cash += proceeds
        pnl = proceeds - position["entry_cost"]
        trades.append({
            "ticker": position["ticker"],
            "source": position["source"],
            "entry_date": position["entry_date"],
            "exit_date": date_value,
            "entry_price": position["entry_price"],
            "exit_price": price,
            "shares": position["shares"],
            "pnl": pnl,
            "r_multiple": pnl / position["planned_risk"],
            "holding_days": (
                calendar_index[date_value] - position["entry_index"] + 1
            ),
            "exit_reason": reason,
        })

    for i, date_value in enumerate(calendar):
        # 1. Gap exits at the open.
        for position in positions[:]:
            today = bar(position["ticker"], date_value)
            if today is None:
                continue
            open_price = float(today["Open"])
            if open_price <= position["stop"]:
                reason = "GAP_STOP"
            elif open_price >= position["target"]:
                reason = "GAP_TARGET"
            else:
                continue
            close(position, date_value, open_price * (1 - hv.SLIPPAGE_PCT), reason)
            positions.remove(position)

        # 2. Entries at the open, sized on total equity.
        if pending:
            equity_now = cash
            for position in positions:
                today = bar(position["ticker"], date_value)
                mark = float(today["Open"]) if today is not None else position["entry_price"]
                equity_now += position["shares"] * mark
            for signal in pending:
                if len(positions) >= slots:
                    break
                today = bar(signal["ticker"], date_value)
                if today is None:
                    continue
                entry_price = float(today["Open"]) * (1 + hv.SLIPPAGE_PCT)
                stop_distance = signal["atr"] * hv.ATR_MULTIPLIER
                shares = min(
                    math.floor(
                        equity_now * hv.RISK_PER_TRADE_PCT / 100 / stop_distance
                    ),
                    math.floor(cash / entry_price),
                )
                if shares <= 0:
                    continue
                cash -= shares * entry_price
                positions.append({
                    "ticker": signal["ticker"],
                    "source": signal["source"],
                    "entry_date": date_value,
                    "entry_index": i,
                    "entry_price": entry_price,
                    "entry_cost": shares * entry_price,
                    "shares": shares,
                    "stop": entry_price - stop_distance,
                    "target": entry_price + stop_distance * hv.REWARD_RISK_RATIO,
                    "planned_risk": shares * stop_distance,
                })
            pending = []

        # 3. Intraday stop / target / time exits; stop first.
        for position in positions[:]:
            today = bar(position["ticker"], date_value)
            if today is None:
                continue
            held = i - position["entry_index"] + 1
            if float(today["Low"]) <= position["stop"]:
                price, reason = position["stop"], "STOP"
            elif float(today["High"]) >= position["target"]:
                price, reason = position["target"], "TARGET"
            elif held >= hv.MAX_HOLD_DAYS:
                price, reason = float(today["Close"]), "TIME_EXIT"
            else:
                continue
            close(position, date_value, price * (1 - hv.SLIPPAGE_PCT), reason)
            positions.remove(position)

        # 4. Mark equity at the close.
        value = cash
        for position in positions:
            today = bar(position["ticker"], date_value)
            mark = float(today["Close"]) if today is not None else position["entry_price"]
            value += position["shares"] * mark
        equity.append(value)

        # 5. After the close, queue next-day entries for free slots.
        if i < len(calendar) - 1:
            held_tickers = {position["ticker"] for position in positions}
            free = slots - len(positions)
            for candidate in lookup.get(date_value, []):
                if free <= 0:
                    break
                if candidate["ticker"] in held_tickers:
                    continue
                pending.append(candidate)
                held_tickers.add(candidate["ticker"])
                free -= 1

    for position in positions:
        today = bar(position["ticker"], calendar[-1])
        close(
            position,
            calendar[-1],
            float(today["Close"]) * (1 - hv.SLIPPAGE_PCT),
            "END_OF_TEST",
        )
    equity[-1] = cash
    return pd.DataFrame(trades), pd.Series(equity, index=calendar)


def summarize(trades, equity):
    wins = trades.loc[trades["pnl"] > 0, "pnl"].sum()
    losses = -trades.loc[trades["pnl"] <= 0, "pnl"].sum()
    drawdown = (equity / equity.cummax() - 1).min() * -100
    return {
        "trades": len(trades),
        "win_rate_pct": round((trades["pnl"] > 0).mean() * 100, 1),
        "expectancy_r": round(trades["r_multiple"].mean(), 3),
        "profit_factor": round(wins / losses, 2) if losses else None,
        "pnl_usd": round(equity.iloc[-1] - STARTING_EQUITY, 2),
        "total_return_pct": round(
            (equity.iloc[-1] / STARTING_EQUITY - 1) * 100, 1
        ),
        "max_drawdown_pct": round(drawdown, 1),
    }


def main():
    print()
    print("================================")
    print("LEADER PULLBACK + TRADE SLOTS BACKTEST")
    print("================================")

    constituents = hv.load_sp500_constituents()
    stock_raw = hv.download_many(
        constituents["ticker"].tolist(), "S&P 500 stocks"
    )
    market_raw = hv.download_many(
        ["SPY", *hv.SECTOR_ETFS.values()], "SPY + sector ETFs"
    )
    stock_dfs, panel = hv.prepare_stock_data(constituents, stock_raw)
    # Some old bars have zero volume, which breaks the volume ratio.
    panel = panel[panel["AVG_VOLUME20"] > 0]
    spy_df = hv.add_indicators(market_raw["SPY"])
    sector_dfs = {
        etf: hv.add_indicators(market_raw[etf])
        for etf in hv.SECTOR_ETFS.values()
        if etf in market_raw
    }

    calendar = spy_df.index
    final_date = calendar.max()
    full = calendar[calendar >= final_date - pd.DateOffset(years=BACKTEST_YEARS)]
    signal_dates = full[:-1]

    a_plus = build_a_plus_candidates(panel, spy_df, sector_dfs, signal_dates)
    pullback = build_pullback_candidates(panel, set(signal_dates))

    variants = {
        "1 slot A+ only (current bot)": (ranked(a_plus), 1),
        "1 slot pullback only": (
            ranked(pullback.groupby("signal_date").head(1)), 1
        ),
        "2 slots A+ then pullback": (ranked(a_plus, pullback), 2),
        "3 slots A+ then pullback": (ranked(a_plus, pullback), 3),
        "3 slots pullback only": (ranked(pullback), 3),
    }
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
    for variant, (lookup, slots) in variants.items():
        for period, period_calendar in periods.items():
            trades, equity = simulate(lookup, period_calendar, stock_dfs, slots)
            rows.append({
                "period": period,
                "variant": variant,
                "start_date": period_calendar[0].date(),
                "end_date": period_calendar[-1].date(),
                **summarize(trades, equity),
            })
            if period == "FULL":
                all_trades.append(trades.assign(variant=variant))
                year_end = equity.groupby(equity.index.year).last()
                yearly[variant] = year_end.diff().fillna(
                    year_end.iloc[0] - STARTING_EQUITY
                ).round(2)

    comparison = pd.DataFrame(rows)
    comparison.to_csv(COMPARISON_CSV, index=False)
    pd.concat(all_trades).to_csv(TRADES_CSV, index=False)
    pd.DataFrame(yearly).to_csv(YEARLY_CSV, index_label="year")

    print()
    print("================================")
    print("RESULTS (start $7,000, 1% risk per trade)")
    print("================================")
    with pd.option_context("display.width", 220, "display.max_columns", 20):
        for period in periods:
            print()
            print(f"--- {period} ---")
            print(
                comparison[comparison["period"] == period]
                .drop(columns=["period"])
                .to_string(index=False)
            )
        print()
        print("P&L by year ($, full period):")
        print(pd.DataFrame(yearly).to_string())

    print()
    print("Saved:")
    for path in (COMPARISON_CSV, TRADES_CSV, YEARLY_CSV):
        print(path)


if __name__ == "__main__":
    main()
