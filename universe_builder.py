import json
import math
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from io import StringIO
from urllib.request import Request, urlopen

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from project_paths import (
    TOP_200_JSON,
    TOP_200_CSV
)
from us_market_universe import (
    build_sp500_top_traded_constituents,
    build_us_market_constituents,
)


# ============================================================
# DYNAMIC TOP-200 UNIVERSE BUILDER
# ============================================================

TOTAL_STOCKS = 200
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


def normalize_ticker(ticker):
    return str(ticker).strip().replace(".", "-")


def safe_float(value, default=0.0):
    try:
        value = float(value)

        if math.isnan(value):
            return default

        return value

    except Exception:
        return default


def ema(series, span):
    return series.ewm(
        span=span,
        adjust=False
    ).mean()


def rsi_14(close):
    delta = close.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


def percentile_rank(series):
    return (
        series.rank(
            pct=True,
            method="average"
        )
        * 100
    )


def load_sp500_constituents():

    print()
    print("================================")
    print("LOADING S&P 500 CONSTITUENTS")
    print("================================")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        )
    }

    request = Request(
        SP500_URL,
        headers=headers
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
        StringIO(
            html
        )
    )

    if not tables:
        raise RuntimeError(
            "Could not find the S&P 500 table."
        )

    df = tables[0].copy()

    needed = {
        "Symbol",
        "Security",
        "GICS Sector",
    }

    if not needed.issubset(
        df.columns
    ):
        raise RuntimeError(
            "Unexpected S&P 500 table format."
        )

    df = df[
        [
            "Symbol",
            "Security",
            "GICS Sector",
        ]
    ].copy()

    df["ticker"] = df[
        "Symbol"
    ].map(
        normalize_ticker
    )

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
            "sector",
        ]
    ].drop_duplicates(
        subset=["ticker"]
    ).reset_index(
        drop=True
    )

    print(
        "Companies loaded:",
        len(df)
    )

    return df


def download_market_data(
    tickers,
    period="1y"
):

    print()
    print(
        "Downloading daily market data..."
    )

    data = yf.download(
        tickers=tickers,
        period=period,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )

    if data.empty:
        raise RuntimeError(
            "No market data downloaded."
        )

    return data


def remove_incomplete_daily_bar(
    df
):
    if df.empty:
        return df

    now_et = datetime.now(
        ZoneInfo(
            "America/New_York"
        )
    )

    last_date = pd.Timestamp(
        df.index[-1]
    ).date()

    now_minutes = (
        now_et.hour * 60
        + now_et.minute
    )

    # At the 8:30 AM Central scheduled run, today's daily
    # candle is still incomplete. Use only completed sessions.
    if (
        last_date == now_et.date()
        and
        now_minutes < (16 * 60 + 15)
    ):
        df = df.iloc[:-1]

    return df


def extract_symbol(
    data,
    ticker
):

    if not isinstance(
        data.columns,
        pd.MultiIndex
    ):
        return pd.DataFrame()

    level0 = (
        data.columns
        .get_level_values(0)
    )

    if ticker not in level0:
        return pd.DataFrame()

    df = data[
        ticker
    ].copy()

    required = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]

    if any(
        column not in df.columns
        for column in required
    ):
        return pd.DataFrame()

    df = df[
        required
    ].dropna(
        subset=["Close"]
    )

    return remove_incomplete_daily_bar(
        df
    )


def rank_sectors():

    symbols = [
        "SPY",
        *SECTOR_ETFS.values()
    ]

    data = download_market_data(
        symbols,
        period="1y"
    )

    spy = extract_symbol(
        data,
        "SPY"
    )

    if len(spy) < 201:
        raise RuntimeError(
            "Not enough SPY history."
        )

    spy_close = spy[
        "Close"
    ]

    spy_return_20 = (
        (
            spy_close.iloc[-1]
            /
            spy_close.iloc[-21]
        )
        - 1
    ) * 100

    spy_return_60 = (
        (
            spy_close.iloc[-1]
            /
            spy_close.iloc[-61]
        )
        - 1
    ) * 100

    rows = []

    for sector, etf in SECTOR_ETFS.items():

        df = extract_symbol(
            data,
            etf
        )

        if len(df) < 201:
            continue

        close = df[
            "Close"
        ]

        ema20 = ema(
            close,
            20
        )

        ema50 = ema(
            close,
            50
        )

        sma200 = close.rolling(
            200
        ).mean()

        return_20 = (
            (
                close.iloc[-1]
                /
                close.iloc[-21]
            )
            - 1
        ) * 100

        return_60 = (
            (
                close.iloc[-1]
                /
                close.iloc[-61]
            )
            - 1
        ) * 100

        bullish = bool(
            close.iloc[-1]
            >
            sma200.iloc[-1]

            and

            close.iloc[-1]
            >
            ema20.iloc[-1]

            and

            ema20.iloc[-1]
            >
            ema50.iloc[-1]
        )

        vs_spy_20 = (
            return_20
            -
            spy_return_20
        )

        vs_spy_60 = (
            return_60
            -
            spy_return_60
        )

        sector_score = (
            (20 if bullish else 0)
            +
            (vs_spy_20 * 3)
            +
            (vs_spy_60 * 2)
        )

        rows.append({
            "sector":
                sector,

            "etf":
                etf,

            "bullish":
                bullish,

            "return_20":
                round(
                    safe_float(
                        return_20
                    ),
                    2
                ),

            "return_60":
                round(
                    safe_float(
                        return_60
                    ),
                    2
                ),

            "vs_spy_20":
                round(
                    safe_float(
                        vs_spy_20
                    ),
                    2
                ),

            "vs_spy_60":
                round(
                    safe_float(
                        vs_spy_60
                    ),
                    2
                ),

            "sector_score":
                round(
                    safe_float(
                        sector_score
                    ),
                    2
                ),
        })

    sectors = pd.DataFrame(
        rows
    ).sort_values(
        by=[
            "sector_score",
            "vs_spy_20",
            "vs_spy_60",
        ],
        ascending=False
    ).reset_index(
        drop=True
    )

    if len(sectors) < 2:
        raise RuntimeError(
            "Could not rank enough sectors."
        )

    print()
    print("================================")
    print("CURRENT SECTOR RANKING")
    print("================================")

    print(
        sectors[
            [
                "sector",
                "etf",
                "bullish",
                "return_20",
                "vs_spy_20",
                "sector_score",
            ]
        ].to_string(
            index=False
        )
    )

    return sectors


def build_stock_metrics(
    constituents,
    sector_ranking,
    frames=None
):

    # frames: optional ticker -> daily bars already downloaded.
    if frames is None:
        tickers = constituents[
            "ticker"
        ].tolist()

        data = download_market_data(
            tickers,
            period="1y"
        )

    sector_lookup = (
        sector_ranking
        .set_index(
            "sector"
        )
        .to_dict(
            orient="index"
        )
    )

    rows = []

    print()
    print(
        "Calculating stock rankings..."
    )

    for row in constituents.itertuples(
        index=False
    ):

        ticker = row.ticker
        company = row.company
        sector = row.sector

        if frames is None:
            df = extract_symbol(
                data,
                ticker
            )
        else:
            df = frames.get(
                ticker,
                pd.DataFrame()
            )

        if len(df) < 201:
            continue

        close = df[
            "Close"
        ]

        volume = df[
            "Volume"
        ]

        ema20 = ema(
            close,
            20
        )

        ema50 = ema(
            close,
            50
        )

        sma200 = close.rolling(
            200
        ).mean()

        rsi = rsi_14(
            close
        )

        return_20 = (
            (
                close.iloc[-1]
                /
                close.iloc[-21]
            )
            - 1
        ) * 100

        return_60 = (
            (
                close.iloc[-1]
                /
                close.iloc[-61]
            )
            - 1
        ) * 100

        avg_dollar_volume_20 = (
            close.tail(20)
            *
            volume.tail(20)
        ).mean()

        trend_ok = bool(
            close.iloc[-1]
            >
            sma200.iloc[-1]

            and

            ema20.iloc[-1]
            >
            ema50.iloc[-1]
        )

        strong_trend = bool(
            trend_ok
            and
            close.iloc[-1]
            >
            ema20.iloc[-1]
        )

        sector_info = sector_lookup.get(
            sector,
            {}
        )

        sector_return_20 = safe_float(
            sector_info.get(
                "return_20"
            )
        )

        sector_return_60 = safe_float(
            sector_info.get(
                "return_60"
            )
        )

        rows.append({
            "ticker":
                ticker,

            "company":
                company,

            "sector":
                sector,

            "price":
                round(
                    safe_float(
                        close.iloc[-1]
                    ),
                    2
                ),

            "return_20":
                round(
                    safe_float(
                        return_20
                    ),
                    2
                ),

            "return_60":
                round(
                    safe_float(
                        return_60
                    ),
                    2
                ),

            "stock_vs_sector_20":
                round(
                    safe_float(
                        return_20
                        -
                        sector_return_20
                    ),
                    2
                ),

            "stock_vs_sector_60":
                round(
                    safe_float(
                        return_60
                        -
                        sector_return_60
                    ),
                    2
                ),

            "rsi":
                round(
                    safe_float(
                        rsi.iloc[-1]
                    ),
                    2
                ),

            "avg_dollar_volume_20":
                round(
                    safe_float(
                        avg_dollar_volume_20
                    ),
                    2
                ),

            "trend_ok":
                trend_ok,

            "strong_trend":
                strong_trend,

            "sector_score":
                safe_float(
                    sector_info.get(
                        "sector_score"
                    )
                ),

            "sector_bullish":
                bool(
                    sector_info.get(
                        "bullish",
                        False
                    )
                ),
        })

    stocks = pd.DataFrame(
        rows
    )

    if stocks.empty:
        raise RuntimeError(
            "No stock metrics were calculated."
        )

    print(
        "Usable stocks with enough history:",
        len(stocks)
    )

    return stocks


def add_universe_scores(
    stocks
):

    stocks = stocks.copy()

    stocks[
        "liquidity_rank"
    ] = percentile_rank(
        stocks[
            "avg_dollar_volume_20"
        ]
    )

    stocks[
        "return20_rank"
    ] = percentile_rank(
        stocks[
            "return_20"
        ]
    )

    stocks[
        "return60_rank"
    ] = percentile_rank(
        stocks[
            "return_60"
        ]
    )

    stocks[
        "relative_sector_rank"
    ] = percentile_rank(
        stocks[
            "stock_vs_sector_20"
        ]
    )

    stocks[
        "universe_rank_score"
    ] = (
        stocks[
            "return20_rank"
        ]
        * 0.30

        +

        stocks[
            "return60_rank"
        ]
        * 0.15

        +

        stocks[
            "relative_sector_rank"
        ]
        * 0.25

        +

        stocks[
            "liquidity_rank"
        ]
        * 0.20

        +

        stocks[
            "strong_trend"
        ].astype(int)
        * 10
    ).round(2)

    return stocks


def select_top_200(
    stocks,
    sector_ranking
):

    top_two_sectors = (
        sector_ranking[
            "sector"
        ]
        .head(2)
        .tolist()
    )

    primary_pool = stocks[
        stocks[
            "sector"
        ].isin(
            top_two_sectors
        )
    ].copy()

    secondary_pool = stocks[
        ~stocks[
            "sector"
        ].isin(
            top_two_sectors
        )
    ].copy()

    primary_pool = primary_pool.sort_values(
        by=[
            "universe_rank_score",
            "avg_dollar_volume_20",
        ],
        ascending=False
    )

    secondary_pool = secondary_pool.sort_values(
        by=[
            "universe_rank_score",
            "sector_score",
            "avg_dollar_volume_20",
        ],
        ascending=False
    )

    selected_primary = primary_pool.head(
        PRIMARY_TARGET
    ).copy()

    selected_primary[
        "bucket"
    ] = "TOP_2_SECTORS"

    secondary_needed = (
        TOTAL_STOCKS
        -
        len(
            selected_primary
        )
    )

    selected_secondary = secondary_pool.head(
        secondary_needed
    ).copy()

    selected_secondary[
        "bucket"
    ] = "OTHER_SECTORS"

    selected = pd.concat(
        [
            selected_primary,
            selected_secondary,
        ],
        ignore_index=True
    )

    if len(selected) < TOTAL_STOCKS:
        raise RuntimeError(
            f"Only {len(selected)} usable stocks were available. "
            f"Need {TOTAL_STOCKS}."
        )

    selected = selected.sort_values(
        by=[
            "universe_rank_score",
            "avg_dollar_volume_20",
        ],
        ascending=False
    ).reset_index(
        drop=True
    )

    selected[
        "universe_rank"
    ] = range(
        1,
        len(selected) + 1
    )

    return (
        selected,
        top_two_sectors,
        len(selected_primary),
        len(selected_secondary)
    )


def save_outputs(
    selected,
    sector_ranking,
    top_two_sectors,
    primary_count,
    secondary_count,
):

    selected.to_csv(
        TOP_200_CSV,
        index=False
    )

    records = (
        selected
        .where(
            pd.notnull(
                selected
            ),
            None
        )
        .to_dict(
            orient="records"
        )
    )

    payload = {
        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "strategy":
            "DYNAMIC_TOP_200_UNIVERSE",

        "total_selected":
            int(
                len(selected)
            ),

        "top_two_sectors":
            top_two_sectors,

        "allocation": {
            "requested_top_two":
                PRIMARY_TARGET,

            "actual_top_two":
                int(
                    primary_count
                ),

            "actual_other":
                int(
                    secondary_count
                ),
        },

        "sector_ranking":
            sector_ranking.to_dict(
                orient="records"
            ),

        "stocks":
            records,

        "tickers":
            selected[
                "ticker"
            ].tolist(),
    }

    with TOP_200_JSON.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            payload,
            file,
            indent=2,
            default=str
        )

    print()
    print("================================")
    print("TOP-200 UNIVERSE CREATED")
    print("================================")

    print(
        "Top 2 sectors:",
        ", ".join(
            top_two_sectors
        )
    )

    print(
        "From top 2 sectors:",
        primary_count
    )

    print(
        "From remaining sectors:",
        secondary_count
    )

    print(
        "Total:",
        len(selected)
    )

    print()
    print(
        "JSON:",
        TOP_200_JSON
    )

    print(
        "CSV:",
        TOP_200_CSV
    )


def load_source_universe():
    """S&P 500 by default; see .env.example for UNIVERSE_SOURCE options."""

    sp500 = load_sp500_constituents()

    source = os.getenv(
        "UNIVERSE_SOURCE",
        "SP500"
    ).strip().upper()

    if source == "SP500":
        return sp500, None

    download = lambda batch: download_market_data(
        batch,
        period="1y"
    )

    if source == "SP500_TOP200":
        print()
        print("================================")
        print("ADDING 200 MOST-TRADED US STOCKS")
        print("================================")

        return build_sp500_top_traded_constituents(
            sp500=sp500,
            download=download,
            extract=extract_symbol
        )

    if source != "US_ALL":
        raise RuntimeError(
            f"Unknown UNIVERSE_SOURCE {source!r}; "
            "use SP500, SP500_TOP200 or US_ALL."
        )

    print()
    print("================================")
    print("LOADING ALL US-LISTED STOCKS")
    print("================================")

    return build_us_market_constituents(
        sp500=sp500,
        download=download,
        extract=extract_symbol
    )


def main():

    load_dotenv()

    constituents, frames = load_source_universe()

    sector_ranking = rank_sectors()

    stocks = build_stock_metrics(
        constituents=constituents,
        sector_ranking=sector_ranking,
        frames=frames
    )

    stocks = add_universe_scores(
        stocks
    )

    (
        selected,
        top_two_sectors,
        primary_count,
        secondary_count,
    ) = select_top_200(
        stocks=stocks,
        sector_ranking=sector_ranking
    )

    save_outputs(
        selected=selected,
        sector_ranking=sector_ranking,
        top_two_sectors=top_two_sectors,
        primary_count=primary_count,
        secondary_count=secondary_count
    )


if __name__ == "__main__":
    main()
