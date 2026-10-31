"""Momentum scan (Bot 3): stocks that jumped on huge volume.

A stock qualifies when, on the last completed day, it closed at least 5%
higher than the day before, on at least 3 times its average daily volume
of the previous 20 sessions, in the top 25% of the day's range (buyers in
control into the close), and above its 200-day average. The biggest
volume surge ranks first. Writes data/momentum_setups.json for
slot_selector.py.

The stocks come from the S&P 500 plus the 200 most-traded other US
stocks (us_market_universe.build_sp500_top_traded_constituents), the
universe the backtest used. Downloading every US stock to rank them
takes several minutes.

Bot 3 trades are exited by a trailing stop (trade_manager.py): 1.5 ATR
below the entry, then 3 ATR below the highest close, no target and no
time limit. Backtest, 10 years, Bot 3 alone in one slot: 4.2% a year,
33% of trades won, best trade 33.6R, worst drop 15% (PROJECT_LOG.md,
2026-10-31).
"""

import json
from datetime import datetime, timezone

import pandas as pd

from project_paths import MOMENTUM_SETUPS_JSON
from universe_builder import (
    download_market_data,
    extract_symbol,
    load_sp500_constituents,
)
from us_market_universe import build_sp500_top_traded_constituents

MIN_JUMP_PCT = 5.0
MIN_VOLUME_RATIO = 3.0
MIN_CLOSE_POSITION = 0.75
MIN_HISTORY_BARS = 201
MAX_SETUPS_SAVED = 20


def latest_metrics(ticker, frame):
    """Values for the last completed bar, or None if too short."""
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
    high, low = frame["High"].iloc[-1], frame["Low"].iloc[-1]
    average_volume = frame["Volume"].iloc[-21:-1].mean()

    values = {
        "price": close.iloc[-1],
        "sma200": close.rolling(200).mean().iloc[-1],
        "atr": true_range.rolling(14).mean().iloc[-1],
        "jump_pct": (close.iloc[-1] / close.iloc[-2] - 1) * 100,
        "volume_ratio": (
            frame["Volume"].iloc[-1] / average_volume if average_volume > 0 else None
        ),
        "close_position": (close.iloc[-1] - low) / (high - low) if high > low else None,
    }
    if any(value is None or pd.isna(value) for value in values.values()):
        return None
    return {
        "ticker": ticker,
        "signal_date": pd.Timestamp(frame.index[-1]).date().isoformat(),
        **{key: float(value) for key, value in values.items()},
    }


def find_momentum_setups(metrics):
    """Qualifying jumps, biggest volume surge first."""
    if not metrics:
        return []
    frame = pd.DataFrame(metrics)
    qualified = frame[
        (frame["jump_pct"] >= MIN_JUMP_PCT)
        & (frame["volume_ratio"] >= MIN_VOLUME_RATIO)
        & (frame["close_position"] >= MIN_CLOSE_POSITION)
        & (frame["price"] > frame["sma200"])
    ].sort_values("volume_ratio", ascending=False)

    setups = []
    for rank, row in enumerate(qualified.to_dict(orient="records"), start=1):
        setups.append({
            "ticker": row["ticker"],
            "company": row.get("company"),
            "sector": row.get("sector"),
            "setup_type": "MOMENTUM_BREAKOUT",
            "signal_date": row["signal_date"],
            "momentum_rank": rank,
            "price": round(row["price"], 2),
            "atr": round(row["atr"], 4),
            "jump_pct": round(row["jump_pct"], 2),
            "volume_ratio": round(row["volume_ratio"], 2),
            "close_position": round(row["close_position"], 2),
            "sma200": round(row["sma200"], 2),
            "in_sp500": bool(row.get("in_sp500")),
        })
    return setups


def main():
    print()
    print("================================")
    print("MOMENTUM SCAN (BOT 3)")
    print("================================")

    sp500 = load_sp500_constituents()
    constituents, frames = build_sp500_top_traded_constituents(
        sp500,
        download=lambda batch: download_market_data(batch, period="1y"),
        extract=extract_symbol,
    )
    details = constituents.set_index("ticker")[["company", "sector"]].to_dict(
        orient="index"
    )
    sp500_tickers = set(sp500["ticker"])

    metrics = []
    for ticker, frame in frames.items():
        row = latest_metrics(ticker, frame)
        if row is not None:
            row.update(details.get(ticker, {}))
            row["in_sp500"] = ticker in sp500_tickers
            metrics.append(row)
    if not metrics:
        raise RuntimeError("No usable price history for the momentum scan.")

    setups = find_momentum_setups(metrics)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "MOMENTUM_BREAKOUT",
        "rules": {
            "min_jump_pct": MIN_JUMP_PCT,
            "min_volume_ratio": MIN_VOLUME_RATIO,
            "min_close_position": MIN_CLOSE_POSITION,
            "trend": "Close > SMA200",
            "universe": "S&P 500 + 200 most-traded other US stocks",
            "exit": "trailing stop: 1.5 ATR, then 3 ATR below the highest close",
        },
        "stocks_scanned": len(metrics),
        "qualified_count": len(setups),
        "qualified": setups[:MAX_SETUPS_SAVED],
    }
    with MOMENTUM_SETUPS_JSON.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)

    print()
    print(f"Stocks scanned: {len(metrics)}")
    print(f"Momentum jumps found: {len(setups)}")
    for setup in setups[:MAX_SETUPS_SAVED]:
        print(
            f"#{setup['momentum_rank']} {setup['ticker']} "
            f"| {setup['sector']} "
            f"| +{setup['jump_pct']:.1f}% today "
            f"| volume {setup['volume_ratio']:.1f}x"
        )
    print()
    print("Saved:", MOMENTUM_SETUPS_JSON)


if __name__ == "__main__":
    main()
