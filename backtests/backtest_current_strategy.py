import math
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
import yfinance as yf


# ============================================================
# CURRENT CONTINUOUS BEST-OPPORTUNITY BACKTEST
#
# Tests only the CURRENT TradingBot behavior:
#
#   Daily scan
#       -> dynamic 200-stock universe
#       -> SPY bullish
#       -> sector bullish
#       -> 4/4 technical setup
#       -> setup score >= 95
#       -> volume >= 1.20x
#       -> RSI 50-65
#       -> stock beats sector
#       -> sector beats SPY
#       -> stock beats SPY
#       -> select BEST CURRENT A+ candidate
#
# Execution assumptions:
#   - One open trade at a time
#   - Signal after daily close
#   - Entry next trading day's open
#   - 1% account risk
#   - 1.5 ATR stop
#   - 2R target
#   - 20 trading-day maximum hold
#   - 0.05% slippage each side
#
# IMPORTANT:
#   This is research only and does not place trades.
#
# LIMITATION:
#   Historical test uses today's S&P 500 constituents, so
#   survivorship bias remains.
# ============================================================


# ============================================================
# PROJECT PATH
# ============================================================

ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from project_paths import BACKTEST_RESULTS_DIR


# ============================================================
# SETTINGS
# ============================================================

BACKTEST_YEARS = 7
DOWNLOAD_PERIOD = "10y"
BATCH_SIZE = 50

STARTING_EQUITY = 10000.0
RISK_PER_TRADE_PCT = 1.0
ATR_MULTIPLIER = 1.5
REWARD_RISK_RATIO = 2.0
MAX_HOLD_DAYS = 20
SLIPPAGE_PCT = 0.05 / 100

# Current A+ master rules
MIN_SETUP_SCORE = 95
MIN_VOLUME_RATIO = 1.20
MIN_RSI = 50
MAX_RSI = 65

# Dynamic universe
TOTAL_UNIVERSE = 200
PRIMARY_TARGET = 150

SP500_URL = (
    "https://en.wikipedia.org/wiki/"
    "List_of_S%26P_500_companies"
)

SECTOR_ETFS = {
    "Information Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}


# ============================================================
# OUTPUTS
# ============================================================

SUMMARY_CSV = (
    BACKTEST_RESULTS_DIR
    / "current_strategy_summary.csv"
)

CURRENT_TRADES_CSV = (
    BACKTEST_RESULTS_DIR
    / "current_strategy_trades.csv"
)

CURRENT_EQUITY_CSV = (
    BACKTEST_RESULTS_DIR
    / "current_strategy_equity.csv"
)

DAILY_CANDIDATES_CSV = (
    BACKTEST_RESULTS_DIR
    / "current_strategy_daily_candidates.csv"
)


# ============================================================
# HELPERS
# ============================================================

def normalize_ticker(ticker):
    return str(ticker).strip().replace(".", "-")


def normalize_index(df):
    if df.empty:
        return df

    idx = pd.to_datetime(df.index)

    try:
        idx = idx.tz_localize(None)
    except TypeError:
        pass

    df = df.copy()
    df.index = idx.normalize()

    df = df[
        ~df.index.duplicated(
            keep="last"
        )
    ]

    return df.sort_index()


def safe_float(value, default=np.nan):
    try:
        value = float(value)

        if math.isnan(value):
            return default

        return value
    except Exception:
        return default


def percentile_rank(series):
    return (
        series.rank(
            pct=True,
            method="average"
        )
        * 100
    )


def iso_week_key(date_value):
    iso = pd.Timestamp(
        date_value
    ).date().isocalendar()

    return (
        int(iso.year),
        int(iso.week)
    )


# ============================================================
# S&P 500 MEMBERSHIP
# ============================================================

def load_sp500_constituents():
    print()
    print("================================")
    print("LOADING S&P 500 CONSTITUENTS")
    print("================================")

    request = Request(
        SP500_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            )
        }
    )

    with urlopen(
        request,
        timeout=30
    ) as response:
        html = response.read().decode(
            "utf-8",
            errors="ignore"
        )

    tables = pd.read_html(
        StringIO(html)
    )

    if not tables:
        raise RuntimeError(
            "Could not read the S&P 500 table."
        )

    df = tables[0].copy()

    required = {
        "Symbol",
        "Security",
        "GICS Sector"
    }

    if not required.issubset(df.columns):
        raise RuntimeError(
            "Unexpected S&P 500 table format."
        )

    df = df[
        [
            "Symbol",
            "Security",
            "GICS Sector"
        ]
    ].copy()

    df["ticker"] = df[
        "Symbol"
    ].map(normalize_ticker)

    df["company"] = df[
        "Security"
    ].astype(str)

    df["sector"] = df[
        "GICS Sector"
    ].astype(str)

    df = df[
        [
            "ticker",
            "company",
            "sector"
        ]
    ].drop_duplicates(
        subset=["ticker"]
    ).reset_index(drop=True)

    print(
        "Current constituents loaded:",
        len(df)
    )

    print()
    print(
        "NOTE: Current membership is used historically, "
        "so survivorship bias remains."
    )

    return df


# ============================================================
# DOWNLOAD
# ============================================================

def extract_symbol(data, ticker):
    if data.empty:
        return pd.DataFrame()

    if isinstance(
        data.columns,
        pd.MultiIndex
    ):
        level0 = data.columns.get_level_values(0)

        if ticker not in level0:
            return pd.DataFrame()

        df = data[ticker].copy()
    else:
        df = data.copy()

    required = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume"
    ]

    if any(
        field not in df.columns
        for field in required
    ):
        return pd.DataFrame()

    df = df[
        required
    ].dropna(
        subset=["Close"]
    )

    return normalize_index(df)


def download_many(tickers, label):
    output = {}

    print()
    print(f"Downloading {label}...")

    for start in range(
        0,
        len(tickers),
        BATCH_SIZE
    ):
        chunk = tickers[
            start:start + BATCH_SIZE
        ]

        print(
            f"{label}: "
            f"{start + 1}-"
            f"{min(start + BATCH_SIZE, len(tickers))} "
            f"of {len(tickers)}"
        )

        try:
            data = yf.download(
                tickers=chunk,
                period=DOWNLOAD_PERIOD,
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                threads=True,
                progress=False
            )

            for ticker in chunk:
                frame = extract_symbol(
                    data,
                    ticker
                )

                if not frame.empty:
                    output[ticker] = frame

        except Exception as exc:
            print(
                f"Batch failed: {exc}"
            )

    return output


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):
    df = df.copy()

    close = df["Close"]

    df["EMA20"] = close.ewm(
        span=20,
        adjust=False
    ).mean()

    df["EMA50"] = close.ewm(
        span=50,
        adjust=False
    ).mean()

    df["SMA200"] = close.rolling(
        200
    ).mean()

    delta = close.diff()

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

    df["AVG_VOLUME20"] = df[
        "Volume"
    ].rolling(20).mean()

    prev_close = close.shift(1)

    true_range = pd.concat(
        [
            df["High"] - df["Low"],
            (
                df["High"] - prev_close
            ).abs(),
            (
                df["Low"] - prev_close
            ).abs(),
        ],
        axis=1
    ).max(axis=1)

    df["ATR"] = true_range.rolling(
        14
    ).mean()

    df["RETURN20"] = (
        (
            close / close.shift(20)
        )
        - 1
    ) * 100

    df["RETURN60"] = (
        (
            close / close.shift(60)
        )
        - 1
    ) * 100

    df["AVG_DOLLAR_VOLUME20"] = (
        close * df["Volume"]
    ).rolling(20).mean()

    df["PREV_CLOSE"] = close.shift(1)

    df["PREV_EMA20"] = df[
        "EMA20"
    ].shift(1)

    return df


# ============================================================
# PREPARE STOCK PANEL
# ============================================================

def prepare_stock_data(
    constituents,
    stock_raw
):
    indicators = {}
    pieces = []

    for row in constituents.itertuples(
        index=False
    ):
        raw = stock_raw.get(
            row.ticker
        )

        if raw is None:
            continue

        df = add_indicators(raw)

        indicators[
            row.ticker
        ] = df

        temp = df.reset_index().rename(
            columns={"index": "Date"}
        )

        temp["ticker"] = row.ticker
        temp["company"] = row.company
        temp["sector"] = row.sector

        pieces.append(temp)

    if not pieces:
        raise RuntimeError(
            "No usable stock histories."
        )

    panel = pd.concat(
        pieces,
        ignore_index=True
    )

    panel["Date"] = pd.to_datetime(
        panel["Date"]
    ).dt.normalize()

    print()
    print(
        "Usable stock histories:",
        len(indicators)
    )

    return indicators, panel


# ============================================================
# SECTOR STATE
# ============================================================

def build_sector_state(
    date_value,
    spy_df,
    sector_dfs
):
    if date_value not in spy_df.index:
        return None

    spy = spy_df.loc[
        date_value
    ]

    needed = [
        "Close",
        "EMA20",
        "EMA50",
        "SMA200",
        "RETURN20",
        "RETURN60",
    ]

    if any(
        pd.isna(spy[field])
        for field in needed
    ):
        return None

    spy_bullish = bool(
        spy["Close"] > spy["SMA200"]
        and
        spy["Close"] > spy["EMA20"]
        and
        spy["EMA20"] > spy["EMA50"]
    )

    rows = []

    for sector, etf in SECTOR_ETFS.items():
        df = sector_dfs.get(etf)

        if (
            df is None
            or
            date_value not in df.index
        ):
            continue

        row = df.loc[
            date_value
        ]

        if any(
            pd.isna(row[field])
            for field in needed
        ):
            continue

        bullish = bool(
            row["Close"] > row["SMA200"]
            and
            row["Close"] > row["EMA20"]
            and
            row["EMA20"] > row["EMA50"]
        )

        vs_spy_20 = (
            float(row["RETURN20"])
            - float(spy["RETURN20"])
        )

        vs_spy_60 = (
            float(row["RETURN60"])
            - float(spy["RETURN60"])
        )

        sector_score = (
            (20 if bullish else 0)
            + vs_spy_20 * 3
            + vs_spy_60 * 2
        )

        rows.append({
            "sector": sector,
            "etf": etf,
            "bullish": bullish,
            "return_20": float(
                row["RETURN20"]
            ),
            "return_60": float(
                row["RETURN60"]
            ),
            "vs_spy_20": vs_spy_20,
            "vs_spy_60": vs_spy_60,
            "sector_score": sector_score,
        })

    if len(rows) < 2:
        return None

    sector_df = pd.DataFrame(
        rows
    ).sort_values(
        by=[
            "sector_score",
            "vs_spy_20",
            "vs_spy_60",
        ],
        ascending=False
    ).reset_index(drop=True)

    return {
        "spy": spy,
        "spy_bullish": spy_bullish,
        "sector_df": sector_df,
        "sector_lookup": (
            sector_df.set_index(
                "sector"
            ).to_dict(
                orient="index"
            )
        ),
    }


# ============================================================
# SETUP SCORE
# ============================================================

def calculate_setup_score(
    row,
    entry_ok,
    spy_return_20
):
    score = 0

    if row["Close"] > row["SMA200"]:
        score += 10

    if row["EMA20"] > row["EMA50"]:
        score += 10

    if row["Close"] > row["EMA20"]:
        score += 5

    if entry_ok:
        score += 20
    else:
        distance = abs(
            row["Close"] - row["EMA20"]
        ) / row["EMA20"]

        if distance <= 0.02:
            score += 10

    rsi = row["RSI"]

    if 50 <= rsi <= 65:
        score += 10
    elif 65 < rsi <= 70:
        score += 7
    elif 45 <= rsi < 50:
        score += 5

    volume_ratio = (
        row["Volume"]
        / row["AVG_VOLUME20"]
    )

    if volume_ratio >= 1.50:
        score += 15
    elif volume_ratio >= 1.20:
        score += 12
    elif volume_ratio >= 1.00:
        score += 10
    elif volume_ratio >= 0.80:
        score += 5

    stock_vs_spy = (
        row["RETURN20"]
        - spy_return_20
    )

    if stock_vs_spy >= 5:
        score += 15
    elif stock_vs_spy >= 2:
        score += 12
    elif stock_vs_spy >= 0:
        score += 8
    elif stock_vs_spy >= -2:
        score += 4

    # SPY is bullish for any candidate that reaches this function.
    score += 10

    atr_pct = (
        row["ATR"]
        / row["Close"]
    ) * 100

    if 1 <= atr_pct <= 4:
        score += 5
    elif 0.5 <= atr_pct < 1:
        score += 3
    elif 4 < atr_pct <= 6:
        score += 3

    return min(
        int(score),
        100
    )


# ============================================================
# BUILD DAILY BEST A+ CANDIDATE
# ============================================================

def get_daily_best_candidate(
    date_value,
    daily_stocks,
    sector_state
):
    if (
        sector_state is None
        or
        not sector_state["spy_bullish"]
    ):
        return None

    stocks = daily_stocks.copy()

    needed = [
        "Close",
        "EMA20",
        "EMA50",
        "SMA200",
        "RSI",
        "AVG_VOLUME20",
        "ATR",
        "RETURN20",
        "RETURN60",
        "AVG_DOLLAR_VOLUME20",
        "PREV_CLOSE",
        "PREV_EMA20",
    ]

    stocks = stocks.dropna(
        subset=needed
    )

    if stocks.empty:
        return None

    sector_lookup = sector_state[
        "sector_lookup"
    ]

    spy = sector_state["spy"]

    stocks["sector_return20"] = stocks[
        "sector"
    ].map(
        lambda sector: safe_float(
            sector_lookup.get(
                sector,
                {}
            ).get(
                "return_20"
            )
        )
    )

    stocks["sector_return60"] = stocks[
        "sector"
    ].map(
        lambda sector: safe_float(
            sector_lookup.get(
                sector,
                {}
            ).get(
                "return_60"
            )
        )
    )

    stocks["sector_score"] = stocks[
        "sector"
    ].map(
        lambda sector: safe_float(
            sector_lookup.get(
                sector,
                {}
            ).get(
                "sector_score"
            )
        )
    )

    stocks = stocks.dropna(
        subset=[
            "sector_return20",
            "sector_return60",
            "sector_score",
        ]
    )

    if stocks.empty:
        return None

    stocks["stock_vs_sector20"] = (
        stocks["RETURN20"]
        - stocks["sector_return20"]
    )

    stocks["strong_trend"] = (
        (
            stocks["Close"]
            > stocks["SMA200"]
        )
        &
        (
            stocks["EMA20"]
            > stocks["EMA50"]
        )
        &
        (
            stocks["Close"]
            > stocks["EMA20"]
        )
    )

    # Dynamic 200-stock universe ranking
    stocks["liquidity_rank"] = percentile_rank(
        stocks["AVG_DOLLAR_VOLUME20"]
    )

    stocks["return20_rank"] = percentile_rank(
        stocks["RETURN20"]
    )

    stocks["return60_rank"] = percentile_rank(
        stocks["RETURN60"]
    )

    stocks[
        "relative_sector_rank"
    ] = percentile_rank(
        stocks["stock_vs_sector20"]
    )

    stocks["universe_rank_score"] = (
        stocks["return20_rank"] * 0.30
        + stocks["return60_rank"] * 0.15
        + stocks["relative_sector_rank"] * 0.25
        + stocks["liquidity_rank"] * 0.20
        + stocks["strong_trend"].astype(int) * 10
    )

    top_two = (
        sector_state[
            "sector_df"
        ]["sector"]
        .head(2)
        .tolist()
    )

    primary = stocks[
        stocks["sector"].isin(
            top_two
        )
    ].sort_values(
        by=[
            "universe_rank_score",
            "AVG_DOLLAR_VOLUME20",
        ],
        ascending=False
    ).head(
        PRIMARY_TARGET
    )

    remaining = stocks[
        ~stocks["ticker"].isin(
            primary["ticker"]
        )
    ].sort_values(
        by=[
            "universe_rank_score",
            "sector_score",
            "AVG_DOLLAR_VOLUME20",
        ],
        ascending=False
    )

    secondary_needed = max(
        TOTAL_UNIVERSE - len(primary),
        0
    )

    secondary = remaining.head(
        secondary_needed
    )

    universe = pd.concat(
        [primary, secondary],
        ignore_index=True
    )

    if universe.empty:
        return None

    spy_return_20 = float(
        spy["RETURN20"]
    )

    candidates = []

    for row in universe.to_dict(
        orient="records"
    ):
        trend_ok = bool(
            row["Close"] > row["SMA200"]
            and
            row["EMA20"] > row["EMA50"]
        )

        momentum_ok = bool(
            50 <= row["RSI"] <= 70
        )

        volume_ok = bool(
            row["Volume"]
            > row["AVG_VOLUME20"]
        )

        entry_ok = bool(
            row["PREV_CLOSE"]
            <= row["PREV_EMA20"]
            and
            row["Close"]
            > row["EMA20"]
        )

        basic_score = sum(
            [
                trend_ok,
                momentum_ok,
                volume_ok,
                entry_ok,
            ]
        )

        if basic_score != 4:
            continue

        setup_score = calculate_setup_score(
            row=row,
            entry_ok=entry_ok,
            spy_return_20=spy_return_20
        )

        if setup_score < MIN_SETUP_SCORE:
            continue

        volume_ratio = (
            row["Volume"]
            / row["AVG_VOLUME20"]
        )

        if volume_ratio < MIN_VOLUME_RATIO:
            continue

        if not (
            MIN_RSI
            <= row["RSI"]
            <= MAX_RSI
        ):
            continue

        sector_info = sector_lookup.get(
            row["sector"],
            {}
        )

        if not bool(
            sector_info.get(
                "bullish",
                False
            )
        ):
            continue

        sector_vs_spy = (
            float(
                sector_info["return_20"]
            )
            - spy_return_20
        )

        stock_vs_sector = (
            row["RETURN20"]
            - float(
                sector_info["return_20"]
            )
        )

        stock_vs_spy = (
            row["RETURN20"]
            - spy_return_20
        )

        if stock_vs_sector <= 0:
            continue

        if sector_vs_spy <= 0:
            continue

        if stock_vs_spy <= 0:
            continue

        candidates.append({
            "signal_date": pd.Timestamp(
                date_value
            ),
            "ticker": row["ticker"],
            "company": row["company"],
            "sector": row["sector"],
            "sector_etf": sector_info.get(
                "etf"
            ),
            "setup_score": int(
                setup_score
            ),
            "signal_close": float(
                row["Close"]
            ),
            "atr": float(
                row["ATR"]
            ),
            "rsi": float(
                row["RSI"]
            ),
            "volume_ratio": float(
                volume_ratio
            ),
            "stock_vs_sector": float(
                stock_vs_sector
            ),
            "sector_vs_spy": float(
                sector_vs_spy
            ),
            "stock_vs_spy": float(
                stock_vs_spy
            ),
        })

    if not candidates:
        return None

    candidates.sort(
        key=lambda stock: (
            stock["setup_score"],
            stock["sector_vs_spy"],
            stock["stock_vs_sector"],
            stock["stock_vs_spy"],
            stock["volume_ratio"],
        ),
        reverse=True
    )

    best = candidates[0]
    best[
        "candidate_count"
    ] = len(candidates)

    return best


# ============================================================
# BUILD DAILY CANDIDATE STREAM ONCE
# ============================================================

def build_candidate_stream(
    panel,
    spy_df,
    sector_dfs,
    signal_dates
):
    panel_subset = panel[
        panel["Date"].isin(
            signal_dates
        )
    ]

    panel_by_date = {
        date: frame
        for date, frame
        in panel_subset.groupby(
            "Date"
        )
    }

    rows = []

    print()
    print(
        "Building historical daily A+ candidate stream..."
    )

    for index, date_value in enumerate(
        signal_dates,
        start=1
    ):
        if index % 100 == 0:
            print(
                f"Processed {index} / {len(signal_dates)} dates"
            )

        daily = panel_by_date.get(
            date_value
        )

        if (
            daily is None
            or
            daily.empty
        ):
            continue

        sector_state = build_sector_state(
            date_value=date_value,
            spy_df=spy_df,
            sector_dfs=sector_dfs
        )

        candidate = get_daily_best_candidate(
            date_value=date_value,
            daily_stocks=daily,
            sector_state=sector_state
        )

        if candidate:
            rows.append(candidate)

    candidate_df = pd.DataFrame(
        rows
    )

    if not candidate_df.empty:
        candidate_df[
            "signal_date"
        ] = pd.to_datetime(
            candidate_df[
                "signal_date"
            ]
        )

    return candidate_df


# ============================================================
# EXECUTION ENGINE — ONE POSITION AT A TIME
# ============================================================

def get_bar(
    stock_dfs,
    ticker,
    date_value
):
    df = stock_dfs.get(ticker)

    if (
        df is None
        or
        date_value not in df.index
    ):
        return None

    return df.loc[
        date_value
    ]


def run_strategy(
    mode,
    candidate_df,
    calendar,
    stock_dfs
):
    if mode not in {
        "OLD_WEEKLY_LOCK",
        "CURRENT_CONTINUOUS"
    }:
        raise ValueError(
            "Unknown mode."
        )

    candidate_lookup = {}

    if not candidate_df.empty:
        candidate_lookup = {
            date: row.iloc[0].to_dict()
            for date, row
            in candidate_df.groupby(
                "signal_date"
            )
        }

    cash = STARTING_EQUITY
    position = None
    pending = None
    weekly_used = set()

    trades = []
    equity_rows = []
    skipped = []

    calendar_index = {
        date: index
        for index, date
        in enumerate(calendar)
    }

    for i, date_value in enumerate(
        calendar
    ):
        # --------------------------------------------
        # 1. OPEN GAP EXIT
        # --------------------------------------------

        if position is not None:
            bar = get_bar(
                stock_dfs,
                position["ticker"],
                date_value
            )

            if bar is not None:
                open_price = float(
                    bar["Open"]
                )

                exit_price = None
                exit_reason = None

                if open_price <= position["stop"]:
                    exit_price = (
                        open_price
                        * (1 - SLIPPAGE_PCT)
                    )
                    exit_reason = "GAP_STOP"

                elif open_price >= position["target"]:
                    exit_price = (
                        open_price
                        * (1 - SLIPPAGE_PCT)
                    )
                    exit_reason = "GAP_TARGET"

                if exit_price is not None:
                    proceeds = (
                        position["shares"]
                        * exit_price
                    )

                    cash += proceeds

                    pnl = (
                        proceeds
                        - position["entry_cost"]
                    )

                    hold_days = (
                        calendar_index[date_value]
                        - position["entry_index"]
                        + 1
                    )

                    trades.append({
                        **position["signal"],
                        "entry_date":
                            position["entry_date"],
                        "exit_date":
                            date_value,
                        "entry_price":
                            position["entry_price"],
                        "exit_price":
                            exit_price,
                        "stop":
                            position["stop"],
                        "target":
                            position["target"],
                        "shares":
                            position["shares"],
                        "planned_risk":
                            position["planned_risk"],
                        "pnl":
                            pnl,
                        "r_multiple":
                            (
                                pnl
                                / position[
                                    "planned_risk"
                                ]
                                if position[
                                    "planned_risk"
                                ] > 0
                                else 0.0
                            ),
                        "holding_days":
                            hold_days,
                        "exit_reason":
                            exit_reason,
                    })

                    position = None

        # --------------------------------------------
        # 2. ENTER PENDING SIGNAL AT TODAY OPEN
        # --------------------------------------------

        if (
            pending is not None
            and
            position is None
        ):
            signal = pending
            pending = None

            bar = get_bar(
                stock_dfs,
                signal["ticker"],
                date_value
            )

            if bar is None:
                skipped.append({
                    **signal,
                    "skip_reason": "NO_ENTRY_BAR"
                })
            else:
                raw_open = float(
                    bar["Open"]
                )

                entry_price = (
                    raw_open
                    * (1 + SLIPPAGE_PCT)
                )

                stop_distance = (
                    signal["atr"]
                    * ATR_MULTIPLIER
                )

                equity_before_entry = cash

                max_risk = (
                    equity_before_entry
                    * (
                        RISK_PER_TRADE_PCT
                        / 100
                    )
                )

                shares_by_risk = math.floor(
                    max_risk
                    / stop_distance
                )

                shares_by_cash = math.floor(
                    cash
                    / entry_price
                )

                shares = min(
                    shares_by_risk,
                    shares_by_cash
                )

                if shares <= 0:
                    skipped.append({
                        **signal,
                        "skip_reason":
                            "INSUFFICIENT_CASH"
                    })
                else:
                    stop = (
                        entry_price
                        - stop_distance
                    )

                    target = (
                        entry_price
                        + stop_distance
                        * REWARD_RISK_RATIO
                    )

                    entry_cost = (
                        shares
                        * entry_price
                    )

                    planned_risk = (
                        shares
                        * stop_distance
                    )

                    cash -= entry_cost

                    position = {
                        "ticker":
                            signal["ticker"],
                        "entry_date":
                            date_value,
                        "entry_index":
                            calendar_index[
                                date_value
                            ],
                        "entry_price":
                            entry_price,
                        "entry_cost":
                            entry_cost,
                        "shares":
                            shares,
                        "stop":
                            stop,
                        "target":
                            target,
                        "planned_risk":
                            planned_risk,
                        "signal":
                            signal,
                    }

        # --------------------------------------------
        # 3. INTRADAY EXIT / TIME EXIT
        # Conservative ambiguity: stop first
        # --------------------------------------------

        if position is not None:
            bar = get_bar(
                stock_dfs,
                position["ticker"],
                date_value
            )

            if bar is not None:
                low = float(
                    bar["Low"]
                )

                high = float(
                    bar["High"]
                )

                close = float(
                    bar["Close"]
                )

                hold_days = (
                    calendar_index[date_value]
                    - position["entry_index"]
                    + 1
                )

                exit_price = None
                exit_reason = None

                if low <= position["stop"]:
                    exit_price = (
                        position["stop"]
                        * (1 - SLIPPAGE_PCT)
                    )
                    exit_reason = "STOP"

                elif high >= position["target"]:
                    exit_price = (
                        position["target"]
                        * (1 - SLIPPAGE_PCT)
                    )
                    exit_reason = "TARGET"

                elif hold_days >= MAX_HOLD_DAYS:
                    exit_price = (
                        close
                        * (1 - SLIPPAGE_PCT)
                    )
                    exit_reason = "TIME_EXIT"

                if exit_price is not None:
                    proceeds = (
                        position["shares"]
                        * exit_price
                    )

                    cash += proceeds

                    pnl = (
                        proceeds
                        - position["entry_cost"]
                    )

                    trades.append({
                        **position["signal"],
                        "entry_date":
                            position["entry_date"],
                        "exit_date":
                            date_value,
                        "entry_price":
                            position["entry_price"],
                        "exit_price":
                            exit_price,
                        "stop":
                            position["stop"],
                        "target":
                            position["target"],
                        "shares":
                            position["shares"],
                        "planned_risk":
                            position["planned_risk"],
                        "pnl":
                            pnl,
                        "r_multiple":
                            (
                                pnl
                                / position[
                                    "planned_risk"
                                ]
                                if position[
                                    "planned_risk"
                                ] > 0
                                else 0.0
                            ),
                        "holding_days":
                            hold_days,
                        "exit_reason":
                            exit_reason,
                    })

                    position = None

        # --------------------------------------------
        # 4. MARK EQUITY
        # --------------------------------------------

        equity = cash

        if position is not None:
            bar = get_bar(
                stock_dfs,
                position["ticker"],
                date_value
            )

            mark = (
                float(bar["Close"])
                if bar is not None
                else position["entry_price"]
            )

            equity += (
                position["shares"]
                * mark
            )

        equity_rows.append({
            "date": date_value,
            "equity": equity,
            "cash": cash,
            "open_position": (
                position["ticker"]
                if position is not None
                else None
            ),
        })

        # --------------------------------------------
        # 5. AFTER CLOSE: GENERATE NEXT-DAY ENTRY
        # Only while flat and no already-pending order.
        # --------------------------------------------

        if (
            position is not None
            or
            pending is not None
            or
            i >= len(calendar) - 1
        ):
            continue

        candidate = candidate_lookup.get(
            date_value
        )

        if candidate is None:
            continue

        if mode == "OLD_WEEKLY_LOCK":
            week = iso_week_key(
                date_value
            )

            if week in weekly_used:
                continue

            weekly_used.add(week)

        pending = candidate.copy()

    # --------------------------------------------
    # FINAL LIQUIDATION
    # --------------------------------------------

    final_date = calendar[-1]

    if position is not None:
        bar = get_bar(
            stock_dfs,
            position["ticker"],
            final_date
        )

        if bar is not None:
            exit_price = (
                float(bar["Close"])
                * (1 - SLIPPAGE_PCT)
            )

            proceeds = (
                position["shares"]
                * exit_price
            )

            cash += proceeds

            pnl = (
                proceeds
                - position["entry_cost"]
            )

            hold_days = (
                calendar_index[final_date]
                - position["entry_index"]
                + 1
            )

            trades.append({
                **position["signal"],
                "entry_date":
                    position["entry_date"],
                "exit_date":
                    final_date,
                "entry_price":
                    position["entry_price"],
                "exit_price":
                    exit_price,
                "stop":
                    position["stop"],
                "target":
                    position["target"],
                "shares":
                    position["shares"],
                "planned_risk":
                    position["planned_risk"],
                "pnl":
                    pnl,
                "r_multiple":
                    (
                        pnl
                        / position["planned_risk"]
                        if position[
                            "planned_risk"
                        ] > 0
                        else 0.0
                    ),
                "holding_days":
                    hold_days,
                "exit_reason":
                    "END_OF_TEST",
            })

            position = None

            equity_rows[-1][
                "equity"
            ] = cash

            equity_rows[-1][
                "cash"
            ] = cash

            equity_rows[-1][
                "open_position"
            ] = None

    return {
        "mode": mode,
        "trades": trades,
        "equity": equity_rows,
        "skipped": skipped,
        "ending_cash": cash,
    }


# ============================================================
# SUMMARY
# ============================================================

def max_drawdown_pct(series):
    if series.empty:
        return 0.0

    running_max = series.cummax()

    drawdown = (
        series / running_max - 1
    ) * 100

    return abs(
        float(drawdown.min())
    )


def max_consecutive_losses(trades):
    current = 0
    maximum = 0

    for trade in trades:
        if trade["pnl"] < 0:
            current += 1
            maximum = max(
                maximum,
                current
            )
        else:
            current = 0

    return maximum


def summarize(
    result,
    start_date,
    end_date,
    spy_df
):
    trades = result["trades"]

    equity_df = pd.DataFrame(
        result["equity"]
    )

    equity_df["date"] = pd.to_datetime(
        equity_df["date"]
    )

    ending_equity = float(
        equity_df.iloc[-1]["equity"]
    )

    total_return = (
        ending_equity
        / STARTING_EQUITY
        - 1
    ) * 100

    years = max(
        (
            end_date - start_date
        ).days / 365.25,
        0.01
    )

    cagr = (
        (
            ending_equity
            / STARTING_EQUITY
        )
        ** (1 / years)
        - 1
    ) * 100

    wins = [
        trade
        for trade in trades
        if trade["pnl"] > 0
    ]

    losses = [
        trade
        for trade in trades
        if trade["pnl"] < 0
    ]

    win_rate = (
        len(wins)
        / len(trades)
        * 100
        if trades
        else 0.0
    )

    avg_win_r = (
        np.mean(
            [
                trade["r_multiple"]
                for trade in wins
            ]
        )
        if wins
        else 0.0
    )

    avg_loss_r = (
        np.mean(
            [
                trade["r_multiple"]
                for trade in losses
            ]
        )
        if losses
        else 0.0
    )

    expectancy_r = (
        np.mean(
            [
                trade["r_multiple"]
                for trade in trades
            ]
        )
        if trades
        else 0.0
    )

    gross_profit = sum(
        max(
            trade["pnl"],
            0
        )
        for trade in trades
    )

    gross_loss = abs(
        sum(
            min(
                trade["pnl"],
                0
            )
            for trade in trades
        )
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else np.nan
    )

    avg_hold = (
        np.mean(
            [
                trade["holding_days"]
                for trade in trades
            ]
        )
        if trades
        else 0.0
    )

    equity_series = equity_df.set_index(
        "date"
    )["equity"]

    drawdown = max_drawdown_pct(
        equity_series
    )

    exposure_pct = (
        (
            equity_df[
                "open_position"
            ].notna().sum()
            /
            len(equity_df)
        )
        * 100
        if len(equity_df)
        else 0.0
    )

    trades_per_year = (
        len(trades) / years
    )

    # SPY benchmark
    spy_slice = spy_df.loc[
        (
            spy_df.index >= start_date
        )
        &
        (
            spy_df.index <= end_date
        )
    ]

    spy_start = float(
        spy_slice.iloc[0]["Close"]
    )

    spy_end = float(
        spy_slice.iloc[-1]["Close"]
    )

    spy_return = (
        spy_end / spy_start - 1
    ) * 100

    spy_cagr = (
        (
            spy_end / spy_start
        )
        ** (1 / years)
        - 1
    ) * 100

    spy_dd = max_drawdown_pct(
        spy_slice["Close"]
    )

    summary = {
        "strategy": result["mode"],
        "start_date": str(
            start_date.date()
        ),
        "end_date": str(
            end_date.date()
        ),
        "starting_equity":
            STARTING_EQUITY,
        "ending_equity":
            round(
                ending_equity,
                2
            ),
        "total_return_pct":
            round(
                total_return,
                2
            ),
        "cagr_pct":
            round(
                cagr,
                2
            ),
        "max_drawdown_pct":
            round(
                drawdown,
                2
            ),
        "total_trades":
            len(trades),
        "trades_per_year":
            round(
                trades_per_year,
                2
            ),
        "win_rate_pct":
            round(
                win_rate,
                2
            ),
        "average_win_r":
            round(
                float(avg_win_r),
                3
            ),
        "average_loss_r":
            round(
                float(avg_loss_r),
                3
            ),
        "expectancy_r":
            round(
                float(expectancy_r),
                3
            ),
        "profit_factor":
            (
                round(
                    float(profit_factor),
                    3
                )
                if not pd.isna(
                    profit_factor
                )
                else None
            ),
        "average_holding_days":
            round(
                float(avg_hold),
                2
            ),
        "max_consecutive_losses":
            max_consecutive_losses(
                trades
            ),
        "market_exposure_pct":
            round(
                exposure_pct,
                2
            ),
        "spy_total_return_pct":
            round(
                spy_return,
                2
            ),
        "spy_cagr_pct":
            round(
                spy_cagr,
                2
            ),
        "spy_max_drawdown_pct":
            round(
                spy_dd,
                2
            ),
    }

    return summary, equity_df


# ============================================================
# MAIN
# ============================================================

def main():
    print()
    print("================================")
    print("CURRENT STRATEGY BACKTEST")
    print("================================")

    print()
    print("WHAT")
    print(
        "Backtest the current continuous best-opportunity "
        "TradingBot strategy."
    )

    print()
    print("WHY")
    print(
        "We need historical evidence for the strategy we "
        "actually plan to use."
    )

    print()
    print("WHAT IT WILL DO")
    print(
        "It scans every historical trading day, finds the "
        "best current A+ setup, enters the next trading day's "
        "open while the portfolio is flat, and measures the result."
    )

    BACKTEST_RESULTS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    constituents = load_sp500_constituents()

    stock_raw = download_many(
        constituents[
            "ticker"
        ].tolist(),
        "S&P 500 stocks"
    )

    market_raw = download_many(
        [
            "SPY",
            *SECTOR_ETFS.values()
        ],
        "SPY + sector ETFs"
    )

    if "SPY" not in market_raw:
        raise RuntimeError(
            "SPY data missing."
        )

    stock_dfs, panel = prepare_stock_data(
        constituents,
        stock_raw
    )

    spy_df = add_indicators(
        market_raw["SPY"]
    )

    sector_dfs = {
        etf: add_indicators(
            market_raw[etf]
        )
        for etf in SECTOR_ETFS.values()
        if etf in market_raw
    }

    calendar = spy_df.index
    end_date = calendar.max()

    requested_start = (
        end_date
        - pd.DateOffset(
            years=BACKTEST_YEARS
        )
    )

    test_calendar = calendar[
        calendar >= requested_start
    ]

    if len(test_calendar) < 250:
        raise RuntimeError(
            "Not enough historical trading dates."
        )

    signal_dates = test_calendar[:-1]

    start_date = test_calendar[0]
    end_date = test_calendar[-1]

    print()
    print(
        "Backtest period:",
        start_date.date(),
        "to",
        end_date.date()
    )

    candidate_df = build_candidate_stream(
        panel=panel,
        spy_df=spy_df,
        sector_dfs=sector_dfs,
        signal_dates=signal_dates
    )

    candidate_df.to_csv(
        DAILY_CANDIDATES_CSV,
        index=False
    )

    print()
    print(
        "Days with at least one A+ candidate:",
        len(candidate_df)
    )

    current_result = run_strategy(
        mode="CURRENT_CONTINUOUS",
        candidate_df=candidate_df,
        calendar=test_calendar,
        stock_dfs=stock_dfs
    )

    current_summary, current_equity = summarize(
        current_result,
        start_date,
        end_date,
        spy_df
    )

    pd.DataFrame(
        [current_summary]
    ).to_csv(
        SUMMARY_CSV,
        index=False
    )

    pd.DataFrame(
        current_result["trades"]
    ).to_csv(
        CURRENT_TRADES_CSV,
        index=False
    )

    current_equity.to_csv(
        CURRENT_EQUITY_CSV,
        index=False
    )

    print()
    print("================================")
    print("FINAL BACKTEST RESULTS")
    print("================================")
    print()

    print(
        "Strategy:",
        current_summary["strategy"]
    )

    print(
        "Starting Equity:",
        f"${current_summary['starting_equity']:.2f}"
    )

    print(
        "Ending Equity:",
        f"${current_summary['ending_equity']:.2f}"
    )

    print(
        "Total Return:",
        f"{current_summary['total_return_pct']:.2f}%"
    )

    print(
        "CAGR:",
        f"{current_summary['cagr_pct']:.2f}%"
    )

    print(
        "Max Drawdown:",
        f"{current_summary['max_drawdown_pct']:.2f}%"
    )

    print()
    print(
        "Total Trades:",
        current_summary["total_trades"]
    )

    print(
        "Trades / Year:",
        current_summary["trades_per_year"]
    )

    print(
        "Win Rate:",
        f"{current_summary['win_rate_pct']:.2f}%"
    )

    print(
        "Average Win:",
        f"{current_summary['average_win_r']:.3f}R"
    )

    print(
        "Average Loss:",
        f"{current_summary['average_loss_r']:.3f}R"
    )

    print(
        "Expectancy:",
        f"{current_summary['expectancy_r']:.3f}R/trade"
    )

    print(
        "Profit Factor:",
        current_summary["profit_factor"]
    )

    print(
        "Average Hold:",
        f"{current_summary['average_holding_days']:.2f} trading days"
    )

    print(
        "Max Consecutive Losses:",
        current_summary["max_consecutive_losses"]
    )

    print(
        "Market Exposure:",
        f"{current_summary['market_exposure_pct']:.2f}%"
    )

    print()
    print("SPY SAME PERIOD")

    print(
        "SPY Return:",
        f"{current_summary['spy_total_return_pct']:.2f}%"
    )

    print(
        "SPY CAGR:",
        f"{current_summary['spy_cagr_pct']:.2f}%"
    )

    print(
        "SPY Max Drawdown:",
        f"{current_summary['spy_max_drawdown_pct']:.2f}%"
    )

    print()
    print("================================")
    print("FILES SAVED")
    print("================================")
    print(SUMMARY_CSV)
    print(CURRENT_TRADES_CSV)
    print(CURRENT_EQUITY_CSV)
    print(DAILY_CANDIDATES_CSV)

    print()
    print("IMPORTANT")
    print(
        "A 95/100 setup score is not a 95% probability of winning."
    )

    print(
        "Judge the strategy using expectancy, profit factor, "
        "return, drawdown, trade count, and SPY comparison."
    )

    print(
        "This test uses current S&P 500 members historically, "
        "so survivorship bias remains."
    )


if __name__ == "__main__":
    main()
