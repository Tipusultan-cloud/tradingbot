import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yfinance as yf


# ============================================================
# BACKTESTER V2
# Institutional-style validation layer for the current strategy
# ============================================================

BACKTEST_PERIOD = "7y"

# -----------------------------
# STRATEGY RULES — FROZEN
# -----------------------------
ATR_MULTIPLIER = 1.5
REWARD_RISK_RATIO = 2.0
MAX_HOLD_DAYS = 20

REQUIRE_BASIC_SCORE = 4

# Preferred high-quality group from V1.
# V2 reports both ALL 4/4 trades and 90+ trades separately.
PREFERRED_SETUP_SCORE = 90

# -----------------------------
# REALISM / EXECUTION
# -----------------------------
# 5 basis points (0.05%) adverse slippage on entry and exit.
SLIPPAGE_PCT = 0.0005

# Flat commission per side. Most U.S. stock brokers are often $0,
# but leave this configurable.
COMMISSION_PER_SIDE = 0.00

# If target and stop are both touched in the same daily candle,
# daily OHLC data cannot tell which came first.
# True = assume stop first (conservative).
CONSERVATIVE_SAME_DAY = True

# -----------------------------
# PORTFOLIO RISK
# -----------------------------
STARTING_CAPITAL = 10000.0
RISK_PER_TRADE_PCT = 0.01

MAX_OPEN_POSITIONS = 4
MAX_OPEN_RISK_PCT = 0.03
MAX_POSITIONS_PER_SECTOR = 2

# -----------------------------
# HOLDOUT-STYLE PERIODS
# -----------------------------
# Important: because V1 results were already viewed across the full
# historical sample, this is not a pristine untouched OOS test.
# It is still useful as a regime/robustness check.
TRAIN_END = pd.Timestamp("2023-12-31")
OOS_START = pd.Timestamp("2024-01-01")


# ============================================================
# SAME STOCK UNIVERSE AS scanner.py
# ============================================================

SECTORS = {
    "Technology": [
        "MSFT", "AAPL", "NVDA", "AVGO", "AMD",
        "ORCL", "CRM", "CSCO", "KLAC", "LRCX"
    ],
    "Communication Services": [
        "META", "GOOGL", "NFLX", "TMUS", "TTWO",
        "EA", "DIS", "CMCSA", "T", "VZ"
    ],
    "Consumer Discretionary": [
        "AMZN", "HD", "MCD", "TJX", "BKNG",
        "LOW", "ROST", "MAR", "ORLY", "AZO"
    ],
    "Consumer Staples": [
        "WMT", "COST", "PG", "KO", "PEP",
        "CL", "MDLZ", "KMB", "KR", "GIS"
    ],
    "Energy": [
        "XOM", "CVX", "COP", "EOG", "MPC",
        "VLO", "PSX", "WMB", "FANG", "BKR"
    ],
    "Financials": [
        "BRK-B", "JPM", "V", "MA", "GS",
        "MS", "AXP", "SCHW", "PGR", "CB"
    ],
    "Health Care": [
        "LLY", "JNJ", "ABBV", "MRK", "TMO",
        "ABT", "AMGN", "GILD", "ISRG", "SYK"
    ],
    "Industrials": [
        "CAT", "GE", "RTX", "ETN", "PH",
        "DE", "UNP", "HON", "WM", "RSG"
    ],
    "Materials": [
        "LIN", "SHW", "ECL", "VMC", "MLM",
        "NUE", "STLD", "APD", "FCX", "CTVA"
    ],
    "Real Estate": [
        "PLD", "EQIX", "AMT", "WELL", "SPG",
        "PSA", "DLR", "O", "CBRE", "VICI"
    ],
    "Utilities": [
        "NEE", "SO", "DUK", "CEG", "AEP",
        "SRE", "ETR", "XEL", "VST", "D"
    ]
}

TICKER_TO_SECTOR = {
    ticker: sector
    for sector, stocks in SECTORS.items()
    for ticker in stocks
}

TICKERS = list(TICKER_TO_SECTOR.keys())


# ============================================================
# INDICATORS — SAME LOGIC AS CURRENT scanner.py
# ============================================================

def calculate_indicators(df):
    df = df.copy()

    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["SMA200"] = df["Close"].rolling(window=200).mean()

    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()

    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))

    df["AVG_VOLUME20"] = df["Volume"].rolling(window=20).mean()

    previous_close = df["Close"].shift(1)
    tr1 = df["High"] - df["Low"]
    tr2 = (df["High"] - previous_close).abs()
    tr3 = (df["Low"] - previous_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    df["ATR"] = true_range.rolling(window=14).mean()

    df["RETURN20"] = (
        (df["Close"] / df["Close"].shift(20)) - 1
    ) * 100

    return df


def calculate_spy_indicators(df):
    df = df.copy()

    df["EMA50"] = df["Close"].ewm(
        span=50,
        adjust=False
    ).mean()

    df["SPY_RETURN20"] = (
        (df["Close"] / df["Close"].shift(20)) - 1
    ) * 100

    df["MARKET_OK"] = (
        df["Close"] > df["EMA50"]
    )

    return df


# ============================================================
# SAME 100-POINT SETUP SCORE
# ============================================================

def calculate_setup_score(
    latest,
    entry_ok,
    stock_return_20,
    spy_return_20,
    market_ok
):
    score = 0

    # Trend = 25
    if latest["Close"] > latest["SMA200"]:
        score += 10

    if latest["EMA20"] > latest["EMA50"]:
        score += 10

    if latest["Close"] > latest["EMA20"]:
        score += 5

    # Entry quality = 20
    if entry_ok:
        score += 20
    else:
        distance_from_ema20 = abs(
            latest["Close"] - latest["EMA20"]
        ) / latest["EMA20"]

        if distance_from_ema20 <= 0.02:
            score += 10

    # RSI = 10
    rsi = latest["RSI"]

    if 50 <= rsi <= 65:
        score += 10
    elif 65 < rsi <= 70:
        score += 7
    elif 45 <= rsi < 50:
        score += 5

    # Volume = 15
    volume_ratio = (
        latest["Volume"] / latest["AVG_VOLUME20"]
    )

    if volume_ratio >= 1.50:
        score += 15
    elif volume_ratio >= 1.20:
        score += 12
    elif volume_ratio >= 1.00:
        score += 10
    elif volume_ratio >= 0.80:
        score += 5

    # Relative strength vs SPY = 15
    rs_vs_spy = stock_return_20 - spy_return_20

    if rs_vs_spy >= 5:
        score += 15
    elif rs_vs_spy >= 2:
        score += 12
    elif rs_vs_spy >= 0:
        score += 8
    elif rs_vs_spy >= -2:
        score += 4

    # Market trend = 10
    if market_ok:
        score += 10

    # ATR quality = 5
    atr_percent = (
        latest["ATR"] / latest["Close"]
    ) * 100

    if 1 <= atr_percent <= 4:
        score += 5
    elif 0.5 <= atr_percent < 1:
        score += 3
    elif 4 < atr_percent <= 6:
        score += 3

    return min(score, 100)


# ============================================================
# DATA
# ============================================================

def download_all_data():
    symbols = TICKERS + ["SPY"]

    print()
    print("================================")
    print("BACKTESTER V2 - DOWNLOADING DATA")
    print("================================")
    print("Period:", BACKTEST_PERIOD)
    print("Stocks:", len(TICKERS))
    print()

    data = yf.download(
        tickers=symbols,
        period=BACKTEST_PERIOD,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False
    )

    if data.empty:
        raise RuntimeError("No historical data downloaded.")

    return data


def extract_symbol(data, ticker):
    if not isinstance(data.columns, pd.MultiIndex):
        return pd.DataFrame()

    if ticker not in data.columns.get_level_values(0):
        return pd.DataFrame()

    df = data[ticker].copy()

    needed = ["Open", "High", "Low", "Close", "Volume"]

    if any(column not in df.columns for column in needed):
        return pd.DataFrame()

    return df[needed].dropna(
        subset=["Open", "High", "Low", "Close"]
    )


# ============================================================
# CREATE HISTORICAL SIGNALS
# ============================================================

def build_signals_for_stock(ticker, df, spy):
    df = calculate_indicators(df)

    aligned_spy = spy.reindex(df.index).ffill()

    signals = []

    for i in range(200, len(df) - 1):
        latest = df.iloc[i]
        previous = df.iloc[i - 1]
        spy_row = aligned_spy.iloc[i]

        required_values = [
            latest["SMA200"],
            latest["EMA20"],
            latest["EMA50"],
            latest["RSI"],
            latest["ATR"],
            latest["AVG_VOLUME20"],
            latest["RETURN20"],
            spy_row["SPY_RETURN20"]
        ]

        if any(pd.isna(value) for value in required_values):
            continue

        trend_ok = (
            latest["Close"] > latest["SMA200"]
            and latest["EMA20"] > latest["EMA50"]
        )

        momentum_ok = (
            50 <= latest["RSI"] <= 70
        )

        volume_ok = (
            latest["Volume"] > latest["AVG_VOLUME20"]
        )

        entry_ok = (
            previous["Close"] <= previous["EMA20"]
            and latest["Close"] > latest["EMA20"]
        )

        basic_score = sum([
            trend_ok,
            momentum_ok,
            volume_ok,
            entry_ok
        ])

        if basic_score != REQUIRE_BASIC_SCORE:
            continue

        stock_return_20 = float(latest["RETURN20"])
        spy_return_20 = float(spy_row["SPY_RETURN20"])
        market_ok = bool(spy_row["MARKET_OK"])

        setup_score = calculate_setup_score(
            latest=latest,
            entry_ok=entry_ok,
            stock_return_20=stock_return_20,
            spy_return_20=spy_return_20,
            market_ok=market_ok
        )

        entry_index = i + 1
        if entry_index >= len(df):
            continue

        entry_date = df.index[entry_index]
        raw_entry = float(df.iloc[entry_index]["Open"])

        atr = float(latest["ATR"])

        signals.append({
            "ticker": ticker,
            "sector": TICKER_TO_SECTOR[ticker],
            "signal_date": df.index[i],
            "entry_date": entry_date,
            "entry_index": entry_index,
            "atr": atr,
            "raw_entry": raw_entry,
            "setup_score": int(setup_score),
            "basic_score": int(basic_score),
            "rsi": round(float(latest["RSI"]), 2),
            "volume_ratio": round(
                float(latest["Volume"] / latest["AVG_VOLUME20"]),
                3
            ),
            "relative_strength": round(
                float(stock_return_20 - spy_return_20),
                3
            ),
            "market_ok": market_ok
        })

    return signals


# ============================================================
# TRADE EXIT
# ============================================================

def determine_exit(df, signal):
    entry_index = signal["entry_index"]

    # Adverse entry slippage for a long position.
    entry_price = (
        signal["raw_entry"] * (1 + SLIPPAGE_PCT)
    )

    stop_distance = (
        signal["atr"] * ATR_MULTIPLIER
    )

    stop_price = entry_price - stop_distance
    target_price = (
        entry_price
        + stop_distance * REWARD_RISK_RATIO
    )

    if stop_price <= 0:
        return None

    final_index = min(
        entry_index + MAX_HOLD_DAYS - 1,
        len(df) - 1
    )

    raw_exit = None
    reason = None
    exit_index = None

    for j in range(entry_index, final_index + 1):
        bar = df.iloc[j]

        day_open = float(bar["Open"])
        day_high = float(bar["High"])
        day_low = float(bar["Low"])

        if day_open <= stop_price:
            raw_exit = day_open
            reason = "GAP_STOP"
            exit_index = j
            break

        if day_open >= target_price:
            raw_exit = day_open
            reason = "GAP_TARGET"
            exit_index = j
            break

        stop_hit = day_low <= stop_price
        target_hit = day_high >= target_price

        if stop_hit and target_hit:
            if CONSERVATIVE_SAME_DAY:
                raw_exit = stop_price
                reason = "STOP_SAME_DAY"
            else:
                raw_exit = target_price
                reason = "TARGET_SAME_DAY"
            exit_index = j
            break

        if stop_hit:
            raw_exit = stop_price
            reason = "STOP"
            exit_index = j
            break

        if target_hit:
            raw_exit = target_price
            reason = "TARGET"
            exit_index = j
            break

    if raw_exit is None:
        exit_index = final_index
        raw_exit = float(df.iloc[exit_index]["Close"])
        reason = "TIME_EXIT"

    # Adverse exit slippage for a long position.
    exit_price = raw_exit * (1 - SLIPPAGE_PCT)

    return {
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "exit_price": exit_price,
        "exit_index": exit_index,
        "exit_date": df.index[exit_index],
        "exit_reason": reason,
        "holding_days": exit_index - entry_index + 1
    }


# ============================================================
# PORTFOLIO SIMULATOR
# ============================================================

@dataclass
class OpenPosition:
    ticker: str
    sector: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    risk_dollars: float


def run_portfolio(signals, stock_frames, minimum_setup_score=0):
    signals = [
        signal
        for signal in signals
        if signal["setup_score"] >= minimum_setup_score
    ]

    signals = sorted(
        signals,
        key=lambda x: (
            x["entry_date"],
            -x["setup_score"],
            x["ticker"]
        )
    )

    equity = STARTING_CAPITAL
    open_positions = []
    completed = []
    skipped = []

    # Track realized equity curve only at exits.
    equity_curve = []

    for signal in signals:
        current_date = signal["entry_date"]

        # Remove positions that already exited before this new entry.
        still_open = []
        for position in open_positions:
            if position.exit_date < current_date:
                pass
            else:
                still_open.append(position)

        open_positions = still_open

        ticker = signal["ticker"]
        sector = signal["sector"]

        # Do not overlap same ticker.
        if any(p.ticker == ticker for p in open_positions):
            skipped.append({
                **signal,
                "skip_reason": "SAME_TICKER_ALREADY_OPEN"
            })
            continue

        if len(open_positions) >= MAX_OPEN_POSITIONS:
            skipped.append({
                **signal,
                "skip_reason": "MAX_OPEN_POSITIONS"
            })
            continue

        sector_count = sum(
            1
            for p in open_positions
            if p.sector == sector
        )

        if sector_count >= MAX_POSITIONS_PER_SECTOR:
            skipped.append({
                **signal,
                "skip_reason": "SECTOR_LIMIT"
            })
            continue

        current_open_risk = sum(
            p.risk_dollars
            for p in open_positions
        )

        max_total_open_risk = (
            equity * MAX_OPEN_RISK_PCT
        )

        desired_risk = (
            equity * RISK_PER_TRADE_PCT
        )

        available_risk = (
            max_total_open_risk
            - current_open_risk
        )

        allowed_risk = min(
            desired_risk,
            available_risk
        )

        if allowed_risk <= 0:
            skipped.append({
                **signal,
                "skip_reason": "MAX_TOTAL_RISK"
            })
            continue

        df = stock_frames[ticker]

        exit_info = determine_exit(
            df,
            signal
        )

        if exit_info is None:
            continue

        risk_per_share = (
            exit_info["entry_price"]
            - exit_info["stop_price"]
        )

        if risk_per_share <= 0:
            continue

        shares_by_risk = math.floor(
            allowed_risk / risk_per_share
        )

        shares_by_cash = math.floor(
            equity / exit_info["entry_price"]
        )

        shares = min(
            shares_by_risk,
            shares_by_cash
        )

        if shares < 1:
            skipped.append({
                **signal,
                "skip_reason": "POSITION_BELOW_1_SHARE"
            })
            continue

        planned_risk = (
            shares * risk_per_share
        )

        entry_cost = COMMISSION_PER_SIDE
        exit_cost = COMMISSION_PER_SIDE

        pnl = (
            shares
            * (
                exit_info["exit_price"]
                - exit_info["entry_price"]
            )
            - entry_cost
            - exit_cost
        )

        r_multiple = (
            pnl / planned_risk
            if planned_risk > 0
            else 0
        )

        equity += pnl

        completed.append({
            **signal,
            **exit_info,
            "shares": shares,
            "planned_risk": round(planned_risk, 2),
            "pnl": round(pnl, 2),
            "r_multiple": round(r_multiple, 4),
            "equity_after": round(equity, 2)
        })

        open_positions.append(
            OpenPosition(
                ticker=ticker,
                sector=sector,
                entry_date=signal["entry_date"],
                exit_date=exit_info["exit_date"],
                risk_dollars=planned_risk
            )
        )

        equity_curve.append({
            "date": exit_info["exit_date"],
            "equity": equity
        })

    return (
        pd.DataFrame(completed),
        pd.DataFrame(skipped),
        pd.DataFrame(equity_curve)
    )


# ============================================================
# METRICS
# ============================================================

def calculate_metrics(trades_df, equity_curve):
    if trades_df.empty:
        return {}

    wins = trades_df[trades_df["pnl"] > 0]
    losses = trades_df[trades_df["pnl"] < 0]

    total = len(trades_df)

    win_rate = (
        len(wins) / total * 100
    )

    expectancy_r = float(
        trades_df["r_multiple"].mean()
    )

    gross_profit = float(
        wins["pnl"].sum()
    )

    gross_loss = abs(
        float(losses["pnl"].sum())
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else math.inf
    )

    ending_equity = float(
        trades_df.iloc[-1]["equity_after"]
    )

    total_return_pct = (
        (ending_equity / STARTING_CAPITAL) - 1
    ) * 100

    max_drawdown_pct = 0.0

    if not equity_curve.empty:
        curve = equity_curve.sort_values("date").copy()
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown_pct"] = (
            (curve["equity"] / curve["peak"]) - 1
        ) * 100

        max_drawdown_pct = abs(
            float(curve["drawdown_pct"].min())
        )

    return {
        "total_trades": int(total),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate_pct": round(win_rate, 2),
        "expectancy_r": round(expectancy_r, 3),
        "profit_factor": (
            round(profit_factor, 3)
            if math.isfinite(profit_factor)
            else "INF"
        ),
        "ending_equity": round(ending_equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "avg_holding_days": round(
            float(trades_df["holding_days"].mean()),
            2
        )
    }


def print_metrics(title, metrics):
    print()
    print("================================")
    print(title)
    print("================================")

    if not metrics:
        print("No trades.")
        return

    print("Total Trades:", metrics["total_trades"])
    print("Wins:", metrics["wins"])
    print("Losses:", metrics["losses"])
    print("Win Rate:", f"{metrics['win_rate_pct']}%")
    print("Expectancy:", f"{metrics['expectancy_r']}R/trade")
    print("Profit Factor:", metrics["profit_factor"])
    print("Ending Equity:", f"${metrics['ending_equity']}")
    print("Total Return:", f"{metrics['total_return_pct']}%")
    print("Max Drawdown:", f"{metrics['max_drawdown_pct']}%")
    print("Avg Holding:", f"{metrics['avg_holding_days']} days")


# ============================================================
# PERIOD HELPERS
# ============================================================

def filter_signals(signals, start=None, end=None):
    filtered = []

    for signal in signals:
        date = pd.Timestamp(signal["signal_date"]).tz_localize(None)

        if start is not None and date < start:
            continue

        if end is not None and date > end:
            continue

        filtered.append(signal)

    return filtered


def normalize_dates(df):
    if df.empty:
        return df

    df = df.copy()

    for column in ["signal_date", "entry_date", "exit_date"]:
        if column in df.columns:
            df[column] = (
                pd.to_datetime(df[column])
                .dt.strftime("%Y-%m-%d")
            )

    return df


# ============================================================
# MAIN
# ============================================================

def main():
    data = download_all_data()

    spy = extract_symbol(data, "SPY")
    if spy.empty:
        raise RuntimeError("SPY data missing.")

    spy = calculate_spy_indicators(spy)

    stock_frames = {}
    all_signals = []
    failed = []

    print()
    print("================================")
    print("BUILDING HISTORICAL SIGNALS")
    print("================================")
    print()

    for number, ticker in enumerate(TICKERS, start=1):
        print(
            f"[{number}/{len(TICKERS)}] {ticker}"
        )

        df = extract_symbol(data, ticker)

        if df.empty:
            failed.append(ticker)
            continue

        stock_frames[ticker] = calculate_indicators(df)

        signals = build_signals_for_stock(
            ticker,
            df,
            spy
        )

        all_signals.extend(signals)

    print()
    print("Historical 4/4 signals found:", len(all_signals))

    # --------------------------------------------------------
    # PERIODS
    # --------------------------------------------------------

    train_signals = filter_signals(
        all_signals,
        end=TRAIN_END
    )

    oos_signals = filter_signals(
        all_signals,
        start=OOS_START
    )

    # --------------------------------------------------------
    # TEST 1: ALL 4/4 TRADES
    # --------------------------------------------------------

    train_all, train_all_skipped, train_all_curve = run_portfolio(
        train_signals,
        stock_frames,
        minimum_setup_score=0
    )

    oos_all, oos_all_skipped, oos_all_curve = run_portfolio(
        oos_signals,
        stock_frames,
        minimum_setup_score=0
    )

    # --------------------------------------------------------
    # TEST 2: 90+ SCORE TRADES
    # --------------------------------------------------------

    train_90, train_90_skipped, train_90_curve = run_portfolio(
        train_signals,
        stock_frames,
        minimum_setup_score=PREFERRED_SETUP_SCORE
    )

    oos_90, oos_90_skipped, oos_90_curve = run_portfolio(
        oos_signals,
        stock_frames,
        minimum_setup_score=PREFERRED_SETUP_SCORE
    )

    train_all_metrics = calculate_metrics(
        train_all,
        train_all_curve
    )

    oos_all_metrics = calculate_metrics(
        oos_all,
        oos_all_curve
    )

    train_90_metrics = calculate_metrics(
        train_90,
        train_90_curve
    )

    oos_90_metrics = calculate_metrics(
        oos_90,
        oos_90_curve
    )

    print()
    print("================================")
    print("VALIDATION PERIODS")
    print("================================")
    print("Older sample: through", TRAIN_END.date())
    print("Holdout-style: from", OOS_START.date())
    print()
    print(
        "NOTE: this is a holdout-style robustness check, "
        "not a pristine unseen OOS test, because V1 full-sample "
        "results were already reviewed."
    )

    print_metrics(
        "OLDER SAMPLE - ALL 4/4",
        train_all_metrics
    )

    print_metrics(
        "HOLDOUT-STYLE - ALL 4/4",
        oos_all_metrics
    )

    print_metrics(
        "OLDER SAMPLE - SCORE 90+",
        train_90_metrics
    )

    print_metrics(
        "HOLDOUT-STYLE - SCORE 90+",
        oos_90_metrics
    )

    # --------------------------------------------------------
    # SAVE RESULTS
    # --------------------------------------------------------

    output_dir = Path(__file__).parent

    outputs = {
        "v2_train_all_trades.csv": normalize_dates(train_all),
        "v2_oos_all_trades.csv": normalize_dates(oos_all),
        "v2_train_90plus_trades.csv": normalize_dates(train_90),
        "v2_oos_90plus_trades.csv": normalize_dates(oos_90),
        "v2_oos_90plus_skipped.csv": normalize_dates(oos_90_skipped)
    }

    for filename, frame in outputs.items():
        frame.to_csv(
            output_dir / filename,
            index=False
        )

    summary_rows = [
        {
            "period": "older",
            "filter": "all_4of4",
            **train_all_metrics
        },
        {
            "period": "holdout_style",
            "filter": "all_4of4",
            **oos_all_metrics
        },
        {
            "period": "older",
            "filter": "score_90_plus",
            **train_90_metrics
        },
        {
            "period": "holdout_style",
            "filter": "score_90_plus",
            **oos_90_metrics
        }
    ]

    pd.DataFrame(summary_rows).to_csv(
        output_dir / "backtest_v2_summary.csv",
        index=False
    )

    print()
    print("================================")
    print("V2 FILES CREATED")
    print("================================")
    print(output_dir / "backtest_v2_summary.csv")
    print(output_dir / "v2_train_all_trades.csv")
    print(output_dir / "v2_oos_all_trades.csv")
    print(output_dir / "v2_train_90plus_trades.csv")
    print(output_dir / "v2_oos_90plus_trades.csv")
    print(output_dir / "v2_oos_90plus_skipped.csv")

    if failed:
        print()
        print("Missing/invalid tickers:")
        print(", ".join(failed))

    print()
    print("================================")
    print("HOW TO JUDGE IT")
    print("================================")
    print(
        "We want the 90+ group to remain positive in the "
        "holdout-style period after slippage and portfolio limits."
    )
    print(
        "Do not judge a strategy by whether every trade wins. "
        "Judge it by expectancy, profit factor, drawdown, "
        "trade count, and stability across periods."
    )


if __name__ == "__main__":
    main()
