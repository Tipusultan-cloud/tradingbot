import math
from collections import defaultdict
from pathlib import Path

import pandas as pd
import yfinance as yf


# ============================================================
# BACKTESTER V3
# True chronological portfolio simulation
# ============================================================

BACKTEST_PERIOD = "7y"

# ------------------------------------------------------------
# FROZEN STRATEGY RULES
# ------------------------------------------------------------

ATR_MULTIPLIER = 1.5
REWARD_RISK_RATIO = 2.0
MAX_HOLD_DAYS = 20

REQUIRE_BASIC_SCORE = 4
PREFERRED_SETUP_SCORE = 90

# ------------------------------------------------------------
# EXECUTION ASSUMPTIONS
# ------------------------------------------------------------

# 0.05% adverse slippage on both entry and exit.
SLIPPAGE_PCT = 0.0005

# Keep configurable. Many U.S. stock brokers charge $0 commission.
COMMISSION_PER_SIDE = 0.00

# If stop and target are both touched in the same daily bar,
# daily OHLC cannot tell which occurred first.
# Conservative assumption: stop happens first.
CONSERVATIVE_SAME_DAY = True

# ------------------------------------------------------------
# PORTFOLIO RULES
# ------------------------------------------------------------

STARTING_CAPITAL = 10000.0

RISK_PER_TRADE_PCT = 0.01
MAX_TOTAL_OPEN_RISK_PCT = 0.03

MAX_OPEN_POSITIONS = 4
MAX_POSITIONS_PER_SECTOR = 2

# No leverage: shares are also limited by available cash.
ALLOW_LEVERAGE = False

# ------------------------------------------------------------
# VALIDATION PERIODS
# ------------------------------------------------------------

OLDER_END = pd.Timestamp("2023-12-31")
HOLDOUT_START = pd.Timestamp("2024-01-01")


# ============================================================
# STOCK UNIVERSE
# Same universe as scanner.py
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
# DATE NORMALIZATION
# ============================================================

def normalize_index(df):
    df = df.copy()

    index = pd.to_datetime(df.index)

    if getattr(index, "tz", None) is not None:
        index = index.tz_localize(None)

    df.index = index.normalize()

    return df


# ============================================================
# INDICATORS
# Same logic as current scanner.py / previous backtests
# ============================================================

def calculate_indicators(df):
    df = normalize_index(df)

    df["EMA20"] = df["Close"].ewm(
        span=20,
        adjust=False
    ).mean()

    df["EMA50"] = df["Close"].ewm(
        span=50,
        adjust=False
    ).mean()

    df["SMA200"] = df["Close"].rolling(
        window=200
    ).mean()

    # RSI 14
    delta = df["Close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss

    df["RSI"] = 100 - (
        100 / (1 + rs)
    )

    # 20-day average volume
    df["AVG_VOLUME20"] = df["Volume"].rolling(
        window=20
    ).mean()

    # ATR 14
    previous_close = df["Close"].shift(1)

    tr1 = df["High"] - df["Low"]

    tr2 = (
        df["High"] - previous_close
    ).abs()

    tr3 = (
        df["Low"] - previous_close
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    df["ATR"] = true_range.rolling(
        window=14
    ).mean()

    # 20-day stock return
    df["RETURN20"] = (
        (
            df["Close"]
            / df["Close"].shift(20)
        )
        - 1
    ) * 100

    return df


def calculate_spy_indicators(df):
    df = normalize_index(df)

    df["EMA50"] = df["Close"].ewm(
        span=50,
        adjust=False
    ).mean()

    df["SPY_RETURN20"] = (
        (
            df["Close"]
            / df["Close"].shift(20)
        )
        - 1
    ) * 100

    df["MARKET_OK"] = (
        df["Close"] > df["EMA50"]
    )

    return df


# ============================================================
# 100-POINT SETUP SCORE
# Same scoring model as scanner.py
# ============================================================

def calculate_setup_score(
    latest,
    entry_ok,
    stock_return_20,
    spy_return_20,
    market_ok
):
    score = 0

    # 1. Trend = 25
    if latest["Close"] > latest["SMA200"]:
        score += 10

    if latest["EMA20"] > latest["EMA50"]:
        score += 10

    if latest["Close"] > latest["EMA20"]:
        score += 5

    # 2. Entry quality = 20
    if entry_ok:
        score += 20
    else:
        distance_from_ema20 = abs(
            latest["Close"] - latest["EMA20"]
        ) / latest["EMA20"]

        if distance_from_ema20 <= 0.02:
            score += 10

    # 3. Momentum = 10
    rsi = latest["RSI"]

    if 50 <= rsi <= 65:
        score += 10

    elif 65 < rsi <= 70:
        score += 7

    elif 45 <= rsi < 50:
        score += 5

    # 4. Volume = 15
    volume_ratio = (
        latest["Volume"]
        / latest["AVG_VOLUME20"]
    )

    if volume_ratio >= 1.50:
        score += 15

    elif volume_ratio >= 1.20:
        score += 12

    elif volume_ratio >= 1.00:
        score += 10

    elif volume_ratio >= 0.80:
        score += 5

    # 5. Relative strength vs SPY = 15
    relative_strength = (
        stock_return_20
        - spy_return_20
    )

    if relative_strength >= 5:
        score += 15

    elif relative_strength >= 2:
        score += 12

    elif relative_strength >= 0:
        score += 8

    elif relative_strength >= -2:
        score += 4

    # 6. Broad market trend = 10
    if market_ok:
        score += 10

    # 7. Volatility quality = 5
    atr_percent = (
        latest["ATR"]
        / latest["Close"]
    ) * 100

    if 1 <= atr_percent <= 4:
        score += 5

    elif 0.5 <= atr_percent < 1:
        score += 3

    elif 4 < atr_percent <= 6:
        score += 3

    return min(
        score,
        100
    )


# ============================================================
# DATA DOWNLOAD
# ============================================================

def download_all_data():
    symbols = TICKERS + ["SPY"]

    print()
    print("================================")
    print("BACKTESTER V3")
    print("DOWNLOADING HISTORICAL DATA")
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
        raise RuntimeError(
            "No historical data was downloaded."
        )

    return data


def extract_symbol(data, ticker):
    if not isinstance(
        data.columns,
        pd.MultiIndex
    ):
        return pd.DataFrame()

    level0 = data.columns.get_level_values(0)

    if ticker not in level0:
        return pd.DataFrame()

    df = data[ticker].copy()

    required_columns = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume"
    ]

    if any(
        column not in df.columns
        for column in required_columns
    ):
        return pd.DataFrame()

    df = df[required_columns].dropna(
        subset=[
            "Open",
            "High",
            "Low",
            "Close"
        ]
    )

    return normalize_index(df)


# ============================================================
# HISTORICAL SIGNAL CREATION
# ============================================================

def build_signals_for_stock(
    ticker,
    df,
    spy
):
    df = calculate_indicators(df)

    aligned_spy = spy.reindex(
        df.index
    ).ffill()

    signals = []

    for i in range(
        200,
        len(df) - 1
    ):
        latest = df.iloc[i]
        previous = df.iloc[i - 1]
        spy_row = aligned_spy.iloc[i]

        required_values = [
            latest["SMA200"],
            latest["EMA20"],
            latest["EMA50"],
            latest["RSI"],
            latest["AVG_VOLUME20"],
            latest["ATR"],
            latest["RETURN20"],
            spy_row["SPY_RETURN20"]
        ]

        if any(
            pd.isna(value)
            for value in required_values
        ):
            continue

        # ------------------------------------
        # SAME 4/4 GATE
        # ------------------------------------

        trend_ok = (
            latest["Close"] > latest["SMA200"]
            and
            latest["EMA20"] > latest["EMA50"]
        )

        momentum_ok = (
            50 <= latest["RSI"] <= 70
        )

        volume_ok = (
            latest["Volume"]
            >
            latest["AVG_VOLUME20"]
        )

        entry_ok = (
            previous["Close"]
            <=
            previous["EMA20"]

            and

            latest["Close"]
            >
            latest["EMA20"]
        )

        basic_score = sum([
            trend_ok,
            momentum_ok,
            volume_ok,
            entry_ok
        ])

        if basic_score != REQUIRE_BASIC_SCORE:
            continue

        stock_return_20 = float(
            latest["RETURN20"]
        )

        spy_return_20 = float(
            spy_row["SPY_RETURN20"]
        )

        market_ok = bool(
            spy_row["MARKET_OK"]
        )

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

        entry_date = df.index[
            entry_index
        ]

        signals.append({
            "ticker": ticker,
            "sector": TICKER_TO_SECTOR[ticker],

            "signal_date": df.index[i],
            "entry_date": entry_date,

            "setup_score": int(
                setup_score
            ),

            "basic_score": int(
                basic_score
            ),

            "atr": float(
                latest["ATR"]
            ),

            "rsi": round(
                float(latest["RSI"]),
                2
            ),

            "volume_ratio": round(
                float(
                    latest["Volume"]
                    /
                    latest["AVG_VOLUME20"]
                ),
                3
            ),

            "relative_strength": round(
                float(
                    stock_return_20
                    -
                    spy_return_20
                ),
                3
            ),

            "market_ok": market_ok
        })

    return signals


# ============================================================
# PORTFOLIO HELPERS
# ============================================================

def adverse_entry_price(raw_open):
    return raw_open * (
        1 + SLIPPAGE_PCT
    )


def adverse_exit_price(raw_exit):
    return raw_exit * (
        1 - SLIPPAGE_PCT
    )


def current_open_risk(
    positions
):
    return sum(
        position["planned_risk"]
        for position in positions.values()
    )


def sector_position_count(
    positions,
    sector
):
    return sum(
        1
        for position in positions.values()
        if position["sector"] == sector
    )


def get_mark_price(
    ticker,
    date,
    stock_frames,
    field,
    fallback
):
    df = stock_frames[
        ticker
    ]

    if date in df.index:
        value = df.at[
            date,
            field
        ]

        if not pd.isna(value):
            return float(value)

    return float(fallback)


def portfolio_equity_at_open(
    cash,
    positions,
    date,
    stock_frames
):
    equity = cash

    for ticker, position in positions.items():

        mark = get_mark_price(
            ticker=ticker,
            date=date,
            stock_frames=stock_frames,
            field="Open",
            fallback=position["last_price"]
        )

        equity += (
            position["shares"]
            * mark
        )

    return equity


def portfolio_equity_at_close(
    cash,
    positions,
    date,
    stock_frames
):
    equity = cash

    for ticker, position in positions.items():

        mark = get_mark_price(
            ticker=ticker,
            date=date,
            stock_frames=stock_frames,
            field="Close",
            fallback=position["last_price"]
        )

        position["last_price"] = mark

        equity += (
            position["shares"]
            * mark
        )

    return equity


# ============================================================
# CLOSE POSITION
# ============================================================

def close_position(
    ticker,
    position,
    raw_exit_price,
    exit_date,
    exit_reason,
    cash,
    completed_trades
):
    exit_price = adverse_exit_price(
        raw_exit_price
    )

    proceeds = (
        position["shares"]
        * exit_price
    )

    cash += (
        proceeds
        - COMMISSION_PER_SIDE
    )

    pnl = (
        position["shares"]
        * (
            exit_price
            -
            position["entry_price"]
        )
        -
        COMMISSION_PER_SIDE
        -
        position["entry_commission"]
    )

    r_multiple = (
        pnl
        /
        position["planned_risk"]
    )

    completed_trades.append({
        "ticker":
            ticker,

        "sector":
            position["sector"],

        "signal_date":
            position["signal_date"],

        "entry_date":
            position["entry_date"],

        "exit_date":
            exit_date,

        "setup_score":
            position["setup_score"],

        "basic_score":
            position["basic_score"],

        "entry_price":
            round(
                position["entry_price"],
                4
            ),

        "stop_price":
            round(
                position["stop_price"],
                4
            ),

        "target_price":
            round(
                position["target_price"],
                4
            ),

        "exit_price":
            round(
                exit_price,
                4
            ),

        "shares":
            int(
                position["shares"]
            ),

        "planned_risk":
            round(
                position["planned_risk"],
                2
            ),

        "pnl":
            round(
                pnl,
                2
            ),

        "r_multiple":
            round(
                r_multiple,
                4
            ),

        "holding_days":
            int(
                position["holding_days"]
            ),

        "exit_reason":
            exit_reason,

        "rsi":
            position["rsi"],

        "volume_ratio":
            position["volume_ratio"],

        "relative_strength":
            position["relative_strength"],

        "market_ok":
            position["market_ok"]
    })

    return cash


# ============================================================
# TRUE DAY-BY-DAY PORTFOLIO ENGINE
# ============================================================

def run_chronological_portfolio(
    signals,
    stock_frames,
    calendar,
    minimum_setup_score,
    start_date=None,
    end_date=None
):
    signals = [
        signal
        for signal in signals
        if signal["setup_score"]
        >=
        minimum_setup_score
    ]

    # Filter by actual entry date.
    if start_date is not None:
        signals = [
            signal
            for signal in signals
            if signal["entry_date"]
            >=
            start_date
        ]

    if end_date is not None:
        signals = [
            signal
            for signal in signals
            if signal["entry_date"]
            <=
            end_date
        ]

    signals_by_entry_date = defaultdict(
        list
    )

    for signal in signals:
        signals_by_entry_date[
            signal["entry_date"]
        ].append(
            signal
        )

    # Highest-rated candidates get first access
    # to limited portfolio slots/cash.
    for date in signals_by_entry_date:
        signals_by_entry_date[
            date
        ].sort(
            key=lambda signal: (
                signal["setup_score"],
                signal["relative_strength"],
                signal["volume_ratio"]
            ),
            reverse=True
        )

    cash = STARTING_CAPITAL

    positions = {}

    completed_trades = []

    skipped_trades = []

    equity_curve = []

    max_positions_seen = 0
    max_open_risk_pct_seen = 0.0

    calendar = [
        pd.Timestamp(date)
        for date in calendar
    ]

    if start_date is not None:
        calendar = [
            date
            for date in calendar
            if date >= start_date
        ]

    if end_date is not None:
        calendar = [
            date
            for date in calendar
            if date <= end_date
        ]

    for date in calendar:

        # ====================================================
        # STEP 1
        # INCREMENT HOLDING DAYS FOR OVERNIGHT POSITIONS
        # ====================================================

        for ticker, position in list(
            positions.items()
        ):

            df = stock_frames[
                ticker
            ]

            if date in df.index:

                # Entry day was already day 1.
                if date > position[
                    "entry_date"
                ]:
                    position[
                        "holding_days"
                    ] += 1


        # ====================================================
        # STEP 2
        # PROCESS ONLY GAP EXITS AT THE OPEN
        #
        # These proceeds legitimately exist before new
        # positions are entered at today's open.
        # ====================================================

        gap_exit_tickers = []

        for ticker, position in list(
            positions.items()
        ):

            df = stock_frames[
                ticker
            ]

            if date not in df.index:
                continue

            day_open = float(
                df.at[
                    date,
                    "Open"
                ]
            )

            if (
                day_open
                <=
                position["stop_price"]
            ):

                cash = close_position(
                    ticker=ticker,
                    position=position,
                    raw_exit_price=day_open,
                    exit_date=date,
                    exit_reason="GAP_STOP",
                    cash=cash,
                    completed_trades=completed_trades
                )

                gap_exit_tickers.append(
                    ticker
                )

            elif (
                day_open
                >=
                position["target_price"]
            ):

                cash = close_position(
                    ticker=ticker,
                    position=position,
                    raw_exit_price=day_open,
                    exit_date=date,
                    exit_reason="GAP_TARGET",
                    cash=cash,
                    completed_trades=completed_trades
                )

                gap_exit_tickers.append(
                    ticker
                )

        for ticker in gap_exit_tickers:
            positions.pop(
                ticker,
                None
            )


        # ====================================================
        # STEP 3
        # PROCESS NEW ENTRIES AT TODAY'S OPEN
        #
        # We do this BEFORE any intraday exits, so we do not
        # illegally use future intraday proceeds to fund
        # today's opening trades.
        # ====================================================

        todays_signals = (
            signals_by_entry_date.get(
                date,
                []
            )
        )

        for signal in todays_signals:

            ticker = signal[
                "ticker"
            ]

            sector = signal[
                "sector"
            ]

            if ticker in positions:

                skipped_trades.append({
                    **signal,
                    "skip_reason":
                        "SAME_TICKER_ALREADY_OPEN"
                })

                continue

            if (
                len(positions)
                >=
                MAX_OPEN_POSITIONS
            ):

                skipped_trades.append({
                    **signal,
                    "skip_reason":
                        "MAX_OPEN_POSITIONS"
                })

                continue

            if (
                sector_position_count(
                    positions,
                    sector
                )
                >=
                MAX_POSITIONS_PER_SECTOR
            ):

                skipped_trades.append({
                    **signal,
                    "skip_reason":
                        "SECTOR_LIMIT"
                })

                continue

            df = stock_frames.get(
                ticker
            )

            if (
                df is None
                or
                date not in df.index
            ):

                skipped_trades.append({
                    **signal,
                    "skip_reason":
                        "NO_ENTRY_BAR"
                })

                continue

            raw_open = float(
                df.at[
                    date,
                    "Open"
                ]
            )

            if raw_open <= 0:
                continue

            entry_price = (
                adverse_entry_price(
                    raw_open
                )
            )

            stop_distance = (
                signal["atr"]
                *
                ATR_MULTIPLIER
            )

            stop_price = (
                entry_price
                -
                stop_distance
            )

            target_price = (
                entry_price
                +
                (
                    stop_distance
                    *
                    REWARD_RISK_RATIO
                )
            )

            if (
                stop_price <= 0
                or
                stop_distance <= 0
            ):
                continue

            equity_at_open = (
                portfolio_equity_at_open(
                    cash=cash,
                    positions=positions,
                    date=date,
                    stock_frames=stock_frames
                )
            )

            desired_risk = (
                equity_at_open
                *
                RISK_PER_TRADE_PCT
            )

            current_risk = (
                current_open_risk(
                    positions
                )
            )

            max_total_risk = (
                equity_at_open
                *
                MAX_TOTAL_OPEN_RISK_PCT
            )

            available_risk = (
                max_total_risk
                -
                current_risk
            )

            allowed_risk = min(
                desired_risk,
                available_risk
            )

            if allowed_risk <= 0:

                skipped_trades.append({
                    **signal,
                    "skip_reason":
                        "MAX_TOTAL_RISK"
                })

                continue

            shares_by_risk = math.floor(
                allowed_risk
                /
                stop_distance
            )

            if ALLOW_LEVERAGE:

                shares_by_cash = (
                    shares_by_risk
                )

            else:

                spendable_cash = max(
                    cash
                    -
                    COMMISSION_PER_SIDE,
                    0
                )

                shares_by_cash = math.floor(
                    spendable_cash
                    /
                    entry_price
                )

            shares = min(
                shares_by_risk,
                shares_by_cash
            )

            if shares < 1:

                skipped_trades.append({
                    **signal,
                    "skip_reason":
                        "INSUFFICIENT_CASH_OR_RISK"
                })

                continue

            position_cost = (
                shares
                *
                entry_price
            )

            total_entry_cash = (
                position_cost
                +
                COMMISSION_PER_SIDE
            )

            if (
                not ALLOW_LEVERAGE
                and
                total_entry_cash > cash
            ):

                skipped_trades.append({
                    **signal,
                    "skip_reason":
                        "INSUFFICIENT_CASH"
                })

                continue

            planned_risk = (
                shares
                *
                stop_distance
            )

            cash -= (
                total_entry_cash
            )

            positions[
                ticker
            ] = {
                "ticker":
                    ticker,

                "sector":
                    sector,

                "signal_date":
                    signal[
                        "signal_date"
                    ],

                "entry_date":
                    date,

                "entry_price":
                    entry_price,

                "stop_price":
                    stop_price,

                "target_price":
                    target_price,

                "shares":
                    shares,

                "planned_risk":
                    planned_risk,

                "setup_score":
                    signal[
                        "setup_score"
                    ],

                "basic_score":
                    signal[
                        "basic_score"
                    ],

                "rsi":
                    signal[
                        "rsi"
                    ],

                "volume_ratio":
                    signal[
                        "volume_ratio"
                    ],

                "relative_strength":
                    signal[
                        "relative_strength"
                    ],

                "market_ok":
                    signal[
                        "market_ok"
                    ],

                "holding_days":
                    1,

                "last_price":
                    entry_price,

                "entry_commission":
                    COMMISSION_PER_SIDE
            }


        # ====================================================
        # STEP 4
        # PROCESS INTRADAY STOP/TARGET EXITS
        #
        # Happens after today's entries have been determined.
        # ====================================================

        intraday_exit_tickers = []

        for ticker, position in list(
            positions.items()
        ):

            df = stock_frames[
                ticker
            ]

            if date not in df.index:
                continue

            bar = df.loc[
                date
            ]

            day_high = float(
                bar["High"]
            )

            day_low = float(
                bar["Low"]
            )

            stop_hit = (
                day_low
                <=
                position["stop_price"]
            )

            target_hit = (
                day_high
                >=
                position["target_price"]
            )

            raw_exit = None
            exit_reason = None

            if (
                stop_hit
                and
                target_hit
            ):

                if (
                    CONSERVATIVE_SAME_DAY
                ):

                    raw_exit = position[
                        "stop_price"
                    ]

                    exit_reason = (
                        "STOP_SAME_DAY"
                    )

                else:

                    raw_exit = position[
                        "target_price"
                    ]

                    exit_reason = (
                        "TARGET_SAME_DAY"
                    )

            elif stop_hit:

                raw_exit = position[
                    "stop_price"
                ]

                exit_reason = "STOP"

            elif target_hit:

                raw_exit = position[
                    "target_price"
                ]

                exit_reason = "TARGET"

            if raw_exit is not None:

                cash = close_position(
                    ticker=ticker,
                    position=position,
                    raw_exit_price=raw_exit,
                    exit_date=date,
                    exit_reason=exit_reason,
                    cash=cash,
                    completed_trades=completed_trades
                )

                intraday_exit_tickers.append(
                    ticker
                )

        for ticker in intraday_exit_tickers:

            positions.pop(
                ticker,
                None
            )


        # ====================================================
        # STEP 5
        # TIME EXITS AT THE CLOSE
        # ====================================================

        time_exit_tickers = []

        for ticker, position in list(
            positions.items()
        ):

            if (
                position[
                    "holding_days"
                ]
                <
                MAX_HOLD_DAYS
            ):

                continue

            df = stock_frames[
                ticker
            ]

            if date not in df.index:
                continue

            raw_close = float(
                df.at[
                    date,
                    "Close"
                ]
            )

            cash = close_position(
                ticker=ticker,
                position=position,
                raw_exit_price=raw_close,
                exit_date=date,
                exit_reason="TIME_EXIT",
                cash=cash,
                completed_trades=completed_trades
            )

            time_exit_tickers.append(
                ticker
            )

        for ticker in time_exit_tickers:

            positions.pop(
                ticker,
                None
            )


        # ====================================================
        # STEP 6
        # END-OF-DAY MARK-TO-MARKET EQUITY
        # ====================================================

        equity = (
            portfolio_equity_at_close(
                cash=cash,
                positions=positions,
                date=date,
                stock_frames=stock_frames
            )
        )

        open_risk = (
            current_open_risk(
                positions
            )
        )

        open_risk_pct = (
            (
                open_risk
                /
                equity
            )
            * 100
            if equity > 0
            else 0
        )

        max_positions_seen = max(
            max_positions_seen,
            len(positions)
        )

        max_open_risk_pct_seen = max(
            max_open_risk_pct_seen,
            open_risk_pct
        )

        equity_curve.append({
            "date":
                date,

            "equity":
                round(
                    equity,
                    2
                ),

            "cash":
                round(
                    cash,
                    2
                ),

            "open_positions":
                len(
                    positions
                ),

            "open_risk":
                round(
                    open_risk,
                    2
                ),

            "open_risk_pct":
                round(
                    open_risk_pct,
                    3
                )
        })


    # ========================================================
    # PERIOD-END FORCED CLOSE
    # ========================================================

    if calendar:

        final_date = calendar[
            -1
        ]

        for ticker, position in list(
            positions.items()
        ):

            df = stock_frames[
                ticker
            ]

            available_dates = df.index[
                df.index
                <=
                final_date
            ]

            if len(
                available_dates
            ) == 0:
                continue

            last_date = available_dates[
                -1
            ]

            raw_close = float(
                df.at[
                    last_date,
                    "Close"
                ]
            )

            cash = close_position(
                ticker=ticker,
                position=position,
                raw_exit_price=raw_close,
                exit_date=last_date,
                exit_reason="PERIOD_END",
                cash=cash,
                completed_trades=completed_trades
            )

        positions.clear()

        # Replace final equity row with fully realized cash.
        if equity_curve:

            equity_curve[
                -1
            ][
                "equity"
            ] = round(
                cash,
                2
            )

            equity_curve[
                -1
            ][
                "cash"
            ] = round(
                cash,
                2
            )

            equity_curve[
                -1
            ][
                "open_positions"
            ] = 0

            equity_curve[
                -1
            ][
                "open_risk"
            ] = 0.0

            equity_curve[
                -1
            ][
                "open_risk_pct"
            ] = 0.0


    completed_df = pd.DataFrame(
        completed_trades
    )

    skipped_df = pd.DataFrame(
        skipped_trades
    )

    equity_df = pd.DataFrame(
        equity_curve
    )

    run_stats = {
        "max_positions_seen":
            max_positions_seen,

        "max_open_risk_pct_seen":
            round(
                max_open_risk_pct_seen,
                3
            )
    }

    return (
        completed_df,
        skipped_df,
        equity_df,
        run_stats
    )


# ============================================================
# METRICS
# ============================================================

def max_consecutive_losses(
    trades_df
):
    if trades_df.empty:
        return 0

    ordered = trades_df.sort_values(
        [
            "exit_date",
            "ticker"
        ]
    )

    longest = 0
    current = 0

    for pnl in ordered[
        "pnl"
    ]:

        if pnl < 0:
            current += 1

            longest = max(
                longest,
                current
            )

        else:
            current = 0

    return longest


def calculate_metrics(
    trades_df,
    equity_df,
    skipped_df,
    run_stats
):
    if equity_df.empty:
        return {}

    ending_equity = float(
        equity_df.iloc[
            -1
        ][
            "equity"
        ]
    )

    total_return_pct = (
        (
            ending_equity
            /
            STARTING_CAPITAL
        )
        - 1
    ) * 100

    curve = equity_df.copy()

    curve[
        "running_peak"
    ] = curve[
        "equity"
    ].cummax()

    curve[
        "drawdown_pct"
    ] = (
        (
            curve[
                "equity"
            ]
            /
            curve[
                "running_peak"
            ]
        )
        - 1
    ) * 100

    max_drawdown_pct = abs(
        float(
            curve[
                "drawdown_pct"
            ].min()
        )
    )

    if trades_df.empty:

        return {
            "total_trades":
                0,

            "ending_equity":
                round(
                    ending_equity,
                    2
                ),

            "total_return_pct":
                round(
                    total_return_pct,
                    2
                ),

            "max_drawdown_pct":
                round(
                    max_drawdown_pct,
                    2
                ),

            "skipped_signals":
                len(
                    skipped_df
                ),

            **run_stats
        }

    wins = trades_df[
        trades_df["pnl"] > 0
    ]

    losses = trades_df[
        trades_df["pnl"] < 0
    ]

    total_trades = len(
        trades_df
    )

    win_rate = (
        len(wins)
        /
        total_trades
    ) * 100

    expectancy_r = float(
        trades_df[
            "r_multiple"
        ].mean()
    )

    average_win_r = (
        float(
            wins[
                "r_multiple"
            ].mean()
        )
        if not wins.empty
        else 0.0
    )

    average_loss_r = (
        float(
            losses[
                "r_multiple"
            ].mean()
        )
        if not losses.empty
        else 0.0
    )

    gross_profit = float(
        wins[
            "pnl"
        ].sum()
    )

    gross_loss = abs(
        float(
            losses[
                "pnl"
            ].sum()
        )
    )

    profit_factor = (
        gross_profit
        /
        gross_loss
        if gross_loss > 0
        else math.inf
    )

    first_date = pd.Timestamp(
        equity_df.iloc[
            0
        ][
            "date"
        ]
    )

    last_date = pd.Timestamp(
        equity_df.iloc[
            -1
        ][
            "date"
        ]
    )

    years = max(
        (
            last_date
            -
            first_date
        ).days
        /
        365.25,
        0
    )

    if (
        years > 0
        and
        ending_equity > 0
    ):

        cagr_pct = (
            (
                ending_equity
                /
                STARTING_CAPITAL
            )
            **
            (
                1
                /
                years
            )
            - 1
        ) * 100

    else:

        cagr_pct = 0.0

    return {
        "total_trades":
            int(
                total_trades
            ),

        "wins":
            int(
                len(
                    wins
                )
            ),

        "losses":
            int(
                len(
                    losses
                )
            ),

        "win_rate_pct":
            round(
                win_rate,
                2
            ),

        "average_win_r":
            round(
                average_win_r,
                3
            ),

        "average_loss_r":
            round(
                average_loss_r,
                3
            ),

        "expectancy_r":
            round(
                expectancy_r,
                3
            ),

        "profit_factor":
            (
                round(
                    profit_factor,
                    3
                )
                if math.isfinite(
                    profit_factor
                )
                else
                "INF"
            ),

        "starting_equity":
            STARTING_CAPITAL,

        "ending_equity":
            round(
                ending_equity,
                2
            ),

        "total_return_pct":
            round(
                total_return_pct,
                2
            ),

        "cagr_pct":
            round(
                cagr_pct,
                2
            ),

        "max_drawdown_pct":
            round(
                max_drawdown_pct,
                2
            ),

        "max_consecutive_losses":
            max_consecutive_losses(
                trades_df
            ),

        "average_holding_days":
            round(
                float(
                    trades_df[
                        "holding_days"
                    ].mean()
                ),
                2
            ),

        "skipped_signals":
            int(
                len(
                    skipped_df
                )
            ),

        **run_stats
    }


def print_metrics(
    title,
    metrics
):
    print()
    print("================================")
    print(title)
    print("================================")

    if not metrics:
        print("No results.")
        return

    print(
        "Total Trades:",
        metrics.get(
            "total_trades",
            0
        )
    )

    print(
        "Wins:",
        metrics.get(
            "wins",
            0
        )
    )

    print(
        "Losses:",
        metrics.get(
            "losses",
            0
        )
    )

    print(
        "Win Rate:",
        f"{metrics.get('win_rate_pct', 0)}%"
    )

    print(
        "Average Win:",
        f"{metrics.get('average_win_r', 0)}R"
    )

    print(
        "Average Loss:",
        f"{metrics.get('average_loss_r', 0)}R"
    )

    print(
        "Expectancy:",
        f"{metrics.get('expectancy_r', 0)}R/trade"
    )

    print(
        "Profit Factor:",
        metrics.get(
            "profit_factor",
            0
        )
    )

    print(
        "Starting Equity:",
        f"${metrics.get('starting_equity', STARTING_CAPITAL)}"
    )

    print(
        "Ending Equity:",
        f"${metrics.get('ending_equity', 0)}"
    )

    print(
        "Total Return:",
        f"{metrics.get('total_return_pct', 0)}%"
    )

    print(
        "CAGR:",
        f"{metrics.get('cagr_pct', 0)}%"
    )

    print(
        "Max Drawdown:",
        f"{metrics.get('max_drawdown_pct', 0)}%"
    )

    print(
        "Max Consecutive Losses:",
        metrics.get(
            "max_consecutive_losses",
            0
        )
    )

    print(
        "Average Holding:",
        f"{metrics.get('average_holding_days', 0)} days"
    )

    print(
        "Max Positions Seen:",
        metrics.get(
            "max_positions_seen",
            0
        )
    )

    print(
        "Max Open Risk:",
        f"{metrics.get('max_open_risk_pct_seen', 0)}%"
    )

    print(
        "Skipped Signals:",
        metrics.get(
            "skipped_signals",
            0
        )
    )


# ============================================================
# CSV DATE CLEANUP
# ============================================================

def clean_dates_for_csv(
    df
):
    if df.empty:
        return df

    df = df.copy()

    for column in [
        "signal_date",
        "entry_date",
        "exit_date",
        "date"
    ]:

        if column in df.columns:

            df[
                column
            ] = pd.to_datetime(
                df[
                    column
                ]
            ).dt.strftime(
                "%Y-%m-%d"
            )

    return df


# ============================================================
# RUN ONE TEST
# ============================================================

def run_test(
    label,
    signals,
    stock_frames,
    calendar,
    minimum_score,
    start_date,
    end_date
):
    print()
    print(
        f"Running: {label}"
    )

    trades, skipped, equity, stats = (
        run_chronological_portfolio(
            signals=signals,
            stock_frames=stock_frames,
            calendar=calendar,
            minimum_setup_score=minimum_score,
            start_date=start_date,
            end_date=end_date
        )
    )

    metrics = calculate_metrics(
        trades_df=trades,
        equity_df=equity,
        skipped_df=skipped,
        run_stats=stats
    )

    print_metrics(
        label,
        metrics
    )

    return (
        trades,
        skipped,
        equity,
        metrics
    )


# ============================================================
# MAIN
# ============================================================

def main():

    data = download_all_data()

    spy = extract_symbol(
        data,
        "SPY"
    )

    if spy.empty:
        raise RuntimeError(
            "SPY data is missing."
        )

    spy = calculate_spy_indicators(
        spy
    )

    stock_frames = {}

    all_signals = []

    failed_tickers = []

    print()
    print("================================")
    print("BUILDING HISTORICAL 4/4 SIGNALS")
    print("================================")
    print()

    for number, ticker in enumerate(
        TICKERS,
        start=1
    ):

        print(
            f"[{number}/{len(TICKERS)}] "
            f"{ticker}"
        )

        df = extract_symbol(
            data,
            ticker
        )

        if df.empty:

            failed_tickers.append(
                ticker
            )

            continue

        prepared = (
            calculate_indicators(
                df
            )
        )

        stock_frames[
            ticker
        ] = prepared

        signals = (
            build_signals_for_stock(
                ticker=ticker,
                df=df,
                spy=spy
            )
        )

        all_signals.extend(
            signals
        )

    print()
    print(
        "Historical 4/4 signals found:",
        len(
            all_signals
        )
    )

    calendar = list(
        spy.index
    )

    # Earliest usable day in the downloaded sample.
    earliest_date = min(
        calendar
    )

    latest_date = max(
        calendar
    )

    # ========================================================
    # TEST A
    # OLDER PERIOD - ALL 4/4
    # ========================================================

    (
        older_all_trades,
        older_all_skipped,
        older_all_equity,
        older_all_metrics
    ) = run_test(
        label="V3 OLDER - ALL 4/4",
        signals=all_signals,
        stock_frames=stock_frames,
        calendar=calendar,
        minimum_score=0,
        start_date=earliest_date,
        end_date=OLDER_END
    )

    # ========================================================
    # TEST B
    # HOLDOUT-STYLE - ALL 4/4
    # ========================================================

    (
        holdout_all_trades,
        holdout_all_skipped,
        holdout_all_equity,
        holdout_all_metrics
    ) = run_test(
        label="V3 HOLDOUT-STYLE - ALL 4/4",
        signals=all_signals,
        stock_frames=stock_frames,
        calendar=calendar,
        minimum_score=0,
        start_date=HOLDOUT_START,
        end_date=latest_date
    )

    # ========================================================
    # TEST C
    # OLDER PERIOD - SCORE 90+
    # ========================================================

    (
        older_90_trades,
        older_90_skipped,
        older_90_equity,
        older_90_metrics
    ) = run_test(
        label="V3 OLDER - SCORE 90+",
        signals=all_signals,
        stock_frames=stock_frames,
        calendar=calendar,
        minimum_score=PREFERRED_SETUP_SCORE,
        start_date=earliest_date,
        end_date=OLDER_END
    )

    # ========================================================
    # TEST D
    # HOLDOUT-STYLE - SCORE 90+
    # ========================================================

    (
        holdout_90_trades,
        holdout_90_skipped,
        holdout_90_equity,
        holdout_90_metrics
    ) = run_test(
        label="V3 HOLDOUT-STYLE - SCORE 90+",
        signals=all_signals,
        stock_frames=stock_frames,
        calendar=calendar,
        minimum_score=PREFERRED_SETUP_SCORE,
        start_date=HOLDOUT_START,
        end_date=latest_date
    )


    # ========================================================
    # SAVE OUTPUTS
    # ========================================================

    output_dir = Path(
        __file__
    ).parent

    output_frames = {
        "v3_older_all_trades.csv":
            older_all_trades,

        "v3_older_all_skipped.csv":
            older_all_skipped,

        "v3_older_all_equity.csv":
            older_all_equity,

        "v3_holdout_all_trades.csv":
            holdout_all_trades,

        "v3_holdout_all_skipped.csv":
            holdout_all_skipped,

        "v3_holdout_all_equity.csv":
            holdout_all_equity,

        "v3_older_90plus_trades.csv":
            older_90_trades,

        "v3_older_90plus_skipped.csv":
            older_90_skipped,

        "v3_older_90plus_equity.csv":
            older_90_equity,

        "v3_holdout_90plus_trades.csv":
            holdout_90_trades,

        "v3_holdout_90plus_skipped.csv":
            holdout_90_skipped,

        "v3_holdout_90plus_equity.csv":
            holdout_90_equity
    }

    for filename, frame in (
        output_frames.items()
    ):

        clean_dates_for_csv(
            frame
        ).to_csv(
            output_dir
            /
            filename,
            index=False
        )

    summary_rows = [
        {
            "test":
                "older_all_4of4",
            **older_all_metrics
        },

        {
            "test":
                "holdout_all_4of4",
            **holdout_all_metrics
        },

        {
            "test":
                "older_score_90_plus",
            **older_90_metrics
        },

        {
            "test":
                "holdout_score_90_plus",
            **holdout_90_metrics
        }
    ]

    summary_df = pd.DataFrame(
        summary_rows
    )

    summary_file = (
        output_dir
        /
        "backtest_v3_summary.csv"
    )

    summary_df.to_csv(
        summary_file,
        index=False
    )


    # ========================================================
    # SKIP REASON SUMMARY
    # ========================================================

    if not holdout_90_skipped.empty:

        skip_summary = (
            holdout_90_skipped[
                "skip_reason"
            ]
            .value_counts()
            .rename_axis(
                "skip_reason"
            )
            .reset_index(
                name="count"
            )
        )

    else:

        skip_summary = pd.DataFrame(
            columns=[
                "skip_reason",
                "count"
            ]
        )

    skip_file = (
        output_dir
        /
        "v3_holdout_90plus_skip_summary.csv"
    )

    skip_summary.to_csv(
        skip_file,
        index=False
    )


    # ========================================================
    # FINAL SCREEN OUTPUT
    # ========================================================

    print()
    print("================================")
    print("V3 COMPLETE")
    print("================================")

    print(
        "Summary:"
    )

    print(
        summary_file
    )

    print()

    print(
        "Most important result:"
    )

    print(
        "V3 HOLDOUT-STYLE - SCORE 90+"
    )

    print()

    print(
        "This is the closest of V1/V2/V3 "
        "to a real $10,000 portfolio simulation."
    )

    print()

    print(
        "It processes entries/exits in chronological "
        "order, reserves cash, limits open risk, "
        "limits positions, and marks the portfolio "
        "to market every trading day."
    )

    print()

    print(
        "Score 90+ is still NOT a 90% chance of winning."
    )

    if failed_tickers:

        print()

        print(
            "Missing/invalid tickers:"
        )

        print(
            ", ".join(
                failed_tickers
            )
        )


if __name__ == "__main__":
    main()
