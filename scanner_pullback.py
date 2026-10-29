"""Leader-pullback scan: strong S&P 500 stocks that just dropped.

A stock qualifies when it is in the top 20% of the S&P 500 by 6-month
return, in an uptrend (Close > SMA200 and EMA20 > EMA50), and closed
more than 5% lower than 5 sessions ago. Strongest 6-month leaders rank
first. Writes data/pullback_setups.json for slot_selector.py.

Backtest: backtests/backtest_pullback_slots.py. The rule is ranked
against the S&P 500 only, as in the backtest, whatever UNIVERSE_SOURCE
is set to.
"""

import json
from datetime import datetime, timezone

import pandas as pd

from project_paths import PULLBACK_SETUPS_JSON
from universe_builder import (
    download_market_data,
    extract_symbol,
    load_sp500_constituents,
)
from us_market_universe import download_daily_bars

LEADER_LOOKBACK_BARS = 126  # about 6 months of sessions
LEADER_MIN_PERCENTILE = 80
PULLBACK_LOOKBACK_BARS = 5
PULLBACK_MAX_RETURN_PCT = -5.0
MIN_HISTORY_BARS = 201
MAX_SETUPS_SAVED = 20


def latest_metrics(ticker, frame):
    """Indicator values on the last completed bar, or None if too short."""
    if len(frame) < MIN_HISTORY_BARS:
        return None

    close = frame["Close"]
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - previous_close).abs(),
            (frame["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    values = {
        "price": close.iloc[-1],
        "ema20": close.ewm(span=20, adjust=False).mean().iloc[-1],
        "ema50": close.ewm(span=50, adjust=False).mean().iloc[-1],
        "sma200": close.rolling(200).mean().iloc[-1],
        "atr": true_range.rolling(14).mean().iloc[-1],
        "return_6m": (
            close.iloc[-1] / close.iloc[-1 - LEADER_LOOKBACK_BARS] - 1
        ) * 100,
        "return_5d": (
            close.iloc[-1] / close.iloc[-1 - PULLBACK_LOOKBACK_BARS] - 1
        ) * 100,
    }
    if any(pd.isna(value) for value in values.values()):
        return None

    return {
        "ticker": ticker,
        "signal_date": pd.Timestamp(frame.index[-1]).date().isoformat(),
        **{key: float(value) for key, value in values.items()},
    }


def find_pullback_setups(metrics):
    """Rank leaders across all stocks, then keep the qualifying pullbacks."""
    if not metrics:
        return []

    frame = pd.DataFrame(metrics)
    frame["leader_percentile"] = frame["return_6m"].rank(pct=True) * 100

    qualified = frame[
        (frame["price"] > frame["sma200"])
        & (frame["ema20"] > frame["ema50"])
        & (frame["leader_percentile"] >= LEADER_MIN_PERCENTILE)
        & (frame["return_5d"] < PULLBACK_MAX_RETURN_PCT)
    ].sort_values("return_6m", ascending=False)

    setups = []
    for rank, row in enumerate(qualified.to_dict(orient="records"), start=1):
        setups.append({
            "ticker": row["ticker"],
            "company": row.get("company"),
            "sector": row.get("sector"),
            "setup_type": "LEADER_PULLBACK",
            "signal_date": row["signal_date"],
            "pullback_rank": rank,
            "price": round(row["price"], 2),
            "atr": round(row["atr"], 4),
            "return_6m": round(row["return_6m"], 2),
            "return_5d": round(row["return_5d"], 2),
            "leader_percentile": round(row["leader_percentile"], 1),
            "ema20": round(row["ema20"], 2),
            "ema50": round(row["ema50"], 2),
            "sma200": round(row["sma200"], 2),
        })
    return setups


def main():
    print()
    print("================================")
    print("LEADER PULLBACK SCAN")
    print("================================")

    constituents = load_sp500_constituents()
    frames = download_daily_bars(
        constituents["ticker"].tolist(),
        download=lambda batch: download_market_data(batch, period="1y"),
        extract=extract_symbol,
    )
    details = constituents.set_index("ticker")[["company", "sector"]].to_dict(
        orient="index"
    )

    metrics = []
    for ticker, frame in frames.items():
        row = latest_metrics(ticker, frame)
        if row is not None:
            row.update(details.get(ticker, {}))
            metrics.append(row)

    if not metrics:
        raise RuntimeError("No usable price history for the pullback scan.")

    setups = find_pullback_setups(metrics)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "LEADER_PULLBACK",
        "rules": {
            "leader_lookback_bars": LEADER_LOOKBACK_BARS,
            "leader_min_percentile": LEADER_MIN_PERCENTILE,
            "pullback_lookback_bars": PULLBACK_LOOKBACK_BARS,
            "pullback_max_return_pct": PULLBACK_MAX_RETURN_PCT,
            "trend": "Close > SMA200 and EMA20 > EMA50",
            "ranking_universe": "S&P 500",
        },
        "stocks_ranked": len(metrics),
        "qualified_count": len(setups),
        "qualified": setups[:MAX_SETUPS_SAVED],
    }
    with PULLBACK_SETUPS_JSON.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)

    print()
    print(f"Stocks ranked: {len(metrics)}")
    print(f"Leader pullbacks found: {len(setups)}")
    for setup in setups[:MAX_SETUPS_SAVED]:
        print(
            f"#{setup['pullback_rank']} {setup['ticker']} "
            f"| {setup['sector']} "
            f"| 6M {setup['return_6m']:+.1f}% "
            f"| 5D {setup['return_5d']:+.1f}%"
        )
    print()
    print("Saved:", PULLBACK_SETUPS_JSON)


if __name__ == "__main__":
    main()
