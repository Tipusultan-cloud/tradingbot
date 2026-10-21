import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from project_paths import (
    QUALIFIED_SETUPS_JSON
)

from universe_loader import (
    load_top_200_universe
)


BATCH_SIZE = 50

MIN_SETUP_SCORE = 95

RECLAIM_MIN_VOLUME_RATIO = 1.20
RECLAIM_MIN_RSI = 50
RECLAIM_MAX_RSI = 65

BREAKOUT_MIN_VOLUME_RATIO = 1.50
BREAKOUT_MIN_RSI = 55
BREAKOUT_MAX_RSI = 65
BREAKOUT_LOOKBACK = 20


def calculate_indicators(df):
    df = df.copy()

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

    delta = df["Close"].diff()

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

    df["RSI"] = 100 - (
        100 / (1 + rs)
    )

    df["AVG_VOLUME20"] = df[
        "Volume"
    ].rolling(
        window=20
    ).mean()

    previous_close = df[
        "Close"
    ].shift(1)

    tr1 = df["High"] - df["Low"]

    tr2 = (
        df["High"]
        - previous_close
    ).abs()

    tr3 = (
        df["Low"]
        - previous_close
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    df["ATR"] = true_range.rolling(
        window=14
    ).mean()

    df["RETURN20"] = (
        (
            df["Close"]
            /
            df["Close"].shift(20)
        )
        - 1
    ) * 100

    # Highest HIGH from the previous 20 completed sessions.
    # shift(1) prevents today's bar from defining its own breakout.
    df["PRIOR_HIGH20"] = df[
        "High"
    ].shift(1).rolling(
        window=BREAKOUT_LOOKBACK
    ).max()

    return df


def remove_incomplete_daily_bar(df):
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

    if (
        last_date == now_et.date()
        and
        now_minutes < (16 * 60 + 15)
    ):
        df = df.iloc[:-1]

    return df


def extract_ticker_frame(
    data,
    ticker
):
    if data.empty:
        return pd.DataFrame()

    if isinstance(
        data.columns,
        pd.MultiIndex
    ):

        level0 = data.columns.get_level_values(
            0
        )

        if ticker not in level0:
            return pd.DataFrame()

        df = data[
            ticker
        ].copy()

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


def download_universe_data(
    tickers
):
    frames = {}

    for start in range(
        0,
        len(tickers),
        BATCH_SIZE
    ):

        chunk = tickers[
            start:start + BATCH_SIZE
        ]

        print(
            f"Downloading stocks "
            f"{start + 1}-"
            f"{min(start + BATCH_SIZE, len(tickers))} "
            f"of {len(tickers)}..."
        )

        data = yf.download(
            tickers=chunk,
            period="1y",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False
        )

        for ticker in chunk:

            df = extract_ticker_frame(
                data,
                ticker
            )

            if not df.empty:
                frames[
                    ticker
                ] = df

    return frames


def load_single_ticker(
    ticker
):
    data = yf.download(
        tickers=[ticker],
        period="1y",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        threads=False,
        progress=False
    )

    return extract_ticker_frame(
        data,
        ticker
    )


def bullish_regime(
    latest
):
    return bool(
        latest["Close"] > latest["SMA200"]
        and
        latest["Close"] > latest["EMA20"]
        and
        latest["EMA20"] > latest["EMA50"]
    )


def calculate_setup_score(
    latest,
    entry_type,
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

    # Validated entry pattern = 20
    if entry_type in {
        "EMA20_RECLAIM",
        "MOMENTUM_BREAKOUT"
    }:
        score += 20

    # Momentum = 10
    rsi = latest["RSI"]

    if 50 <= rsi <= 65:
        score += 10
    elif 65 < rsi <= 70:
        score += 7
    elif 45 <= rsi < 50:
        score += 5

    # Volume = 15
    volume_ratio = (
        latest["Volume"]
        / latest["AVG_VOLUME20"]
    )

    if volume_ratio >= 1.50:
        score += 15
    elif volume_ratio >= 1.30:
        score += 13
    elif volume_ratio >= 1.20:
        score += 12
    elif volume_ratio >= 1.00:
        score += 10
    elif volume_ratio >= 0.80:
        score += 5

    # Relative strength vs SPY = 15
    stock_vs_spy = (
        stock_return_20
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

    # Market = 10
    if market_ok:
        score += 10

    # ATR quality = 5
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
        int(score),
        100
    )

def main():

    print()
    print("================================")
    print("STRICT DUAL-SETUP SWING SCANNER")
    print("================================")
    print()

    (
        sectors,
        ticker_to_sector,
        tickers
    ) = load_top_200_universe()

    print(
        "Dynamic universe loaded:",
        len(tickers),
        "stocks"
    )

    print(
        "Sectors represented:",
        len(sectors)
    )

    print()
    print(
        "Checking strict SPY market regime..."
    )

    spy_df = calculate_indicators(
        load_single_ticker(
            "SPY"
        )
    ).dropna()

    if len(spy_df) < 2:
        raise RuntimeError(
            "Could not load enough SPY data."
        )

    spy_latest = spy_df.iloc[-1]

    market_bullish = bullish_regime(
        spy_latest
    )

    spy_return_20 = float(
        spy_latest[
            "RETURN20"
        ]
    )

    print(
        "SPY:",
        "BULLISH"
        if market_bullish
        else
        "NOT BULLISH"
    )

    print()

    market_data = download_universe_data(
        tickers
    )

    qualified = []
    watch = []
    ignored = []
    errors = []

    print()
    print(
        "Scanning validated entry setups..."
    )

    for ticker in tickers:

        sector = ticker_to_sector[
            ticker
        ]

        try:

            raw = market_data.get(
                ticker
            )

            if raw is None or raw.empty:
                errors.append(
                    ticker
                )
                continue

            df = calculate_indicators(
                raw
            ).dropna()

            if len(df) < 2:
                errors.append(
                    ticker
                )
                continue

            latest = df.iloc[-1]
            previous = df.iloc[-2]

            stock_return_20 = float(
                latest[
                    "RETURN20"
                ]
            )

            stock_vs_spy = (
                stock_return_20
                - spy_return_20
            )

            volume_ratio = float(
                latest["Volume"]
                / latest["AVG_VOLUME20"]
            )

            reclaim_ok = bool(
                latest["Close"] > latest["SMA200"]
                and
                latest["EMA20"] > latest["EMA50"]
                and
                previous["Close"] <= previous["EMA20"]
                and
                latest["Close"] > latest["EMA20"]
                and
                RECLAIM_MIN_RSI
                <= latest["RSI"]
                <= RECLAIM_MAX_RSI
                and
                volume_ratio >= RECLAIM_MIN_VOLUME_RATIO
            )

            breakout_ok = bool(
                latest["Close"] > latest["EMA20"]
                and
                latest["EMA20"] > latest["EMA50"]
                and
                latest["EMA50"] > latest["SMA200"]
                and
                latest["Close"] > latest["PRIOR_HIGH20"]
                and
                BREAKOUT_MIN_RSI
                <= latest["RSI"]
                <= BREAKOUT_MAX_RSI
                and
                volume_ratio >= BREAKOUT_MIN_VOLUME_RATIO
            )

            setup_valid = bool(
                reclaim_ok
                or breakout_ok
            )

            # If the same bar qualifies for both, keep the
            # historically stronger EMA20-reclaim label.
            if reclaim_ok:
                setup_type = "EMA20_RECLAIM"
            elif breakout_ok:
                setup_type = "MOMENTUM_BREAKOUT"
            else:
                setup_type = None

            setup_score = calculate_setup_score(
                latest=latest,
                entry_type=setup_type,
                stock_return_20=stock_return_20,
                spy_return_20=spy_return_20,
                market_ok=market_bullish
            )

            trend_ok = bool(
                latest["Close"] > latest["SMA200"]
                and
                latest["EMA20"] > latest["EMA50"]
            )

            momentum_ok = bool(
                (
                    setup_type == "EMA20_RECLAIM"
                    and
                    RECLAIM_MIN_RSI
                    <= latest["RSI"]
                    <= RECLAIM_MAX_RSI
                )
                or
                (
                    setup_type == "MOMENTUM_BREAKOUT"
                    and
                    BREAKOUT_MIN_RSI
                    <= latest["RSI"]
                    <= BREAKOUT_MAX_RSI
                )
            )

            volume_ok = bool(
                (
                    setup_type == "EMA20_RECLAIM"
                    and
                    volume_ratio >= RECLAIM_MIN_VOLUME_RATIO
                )
                or
                (
                    setup_type == "MOMENTUM_BREAKOUT"
                    and
                    volume_ratio >= BREAKOUT_MIN_VOLUME_RATIO
                )
            )

            # Compatibility field for older dashboard/reporting.
            # A qualified validated setup is treated as a complete setup.
            basic_score = (
                4
                if setup_valid
                else int(
                    trend_ok
                    + bool(50 <= latest["RSI"] <= 70)
                    + bool(volume_ratio >= 1.0)
                    + bool(latest["Close"] > latest["EMA20"])
                )
            )

            result = {
                "ticker":
                    ticker,

                "sector":
                    sector,

                "setup_type":
                    setup_type,

                "setup_valid":
                    bool(
                        setup_valid
                    ),

                "grade":
                    (
                        "A+"
                        if setup_valid
                        and setup_score >= MIN_SETUP_SCORE
                        else None
                    ),

                "basic_score":
                    int(
                        basic_score
                    ),

                "setup_score":
                    int(
                        setup_score
                    ),

                "price":
                    round(
                        float(
                            latest["Close"]
                        ),
                        2
                    ),

                "prior_high20":
                    round(
                        float(
                            latest["PRIOR_HIGH20"]
                        ),
                        2
                    ),

                "atr":
                    round(
                        float(
                            latest["ATR"]
                        ),
                        2
                    ),

                "rsi":
                    round(
                        float(
                            latest["RSI"]
                        ),
                        2
                    ),

                "volume_ratio":
                    round(
                        float(
                            volume_ratio
                        ),
                        2
                    ),

                "stock_return_20":
                    round(
                        float(
                            stock_return_20
                        ),
                        2
                    ),

                "stock_vs_spy":
                    round(
                        float(
                            stock_vs_spy
                        ),
                        2
                    ),

                "relative_strength":
                    round(
                        float(
                            stock_vs_spy
                        ),
                        2
                    ),

                "trend":
                    bool(
                        trend_ok
                    ),

                "momentum":
                    bool(
                        momentum_ok
                    ),

                "volume":
                    bool(
                        volume_ok
                    ),

                "entry":
                    bool(
                        setup_valid
                    )
            }

            if (
                market_bullish
                and
                setup_valid
                and
                setup_score >= MIN_SETUP_SCORE
            ):
                qualified.append(
                    result
                )

            elif (
                setup_valid
                and
                setup_score >= 90
            ):
                watch.append(
                    result
                )

            else:
                ignored.append(
                    result
                )

        except Exception as e:

            errors.append(
                ticker
            )

            print(
                f"{ticker}: {e}"
            )

    qualified.sort(
        key=lambda stock: (
            stock[
                "setup_score"
            ],
            1
            if stock[
                "setup_type"
            ] == "EMA20_RECLAIM"
            else 0,
            stock[
                "stock_vs_spy"
            ],
            stock[
                "volume_ratio"
            ]
        ),
        reverse=True
    )

    watch.sort(
        key=lambda stock: (
            stock[
                "setup_score"
            ],
            stock[
                "stock_vs_spy"
            ],
            stock[
                "volume_ratio"
            ]
        ),
        reverse=True
    )

    reclaim_count = sum(
        stock[
            "setup_type"
        ] == "EMA20_RECLAIM"
        for stock in qualified
    )

    breakout_count = sum(
        stock[
            "setup_type"
        ] == "MOMENTUM_BREAKOUT"
        for stock in qualified
    )

    print()
    print("================================")
    print("STRICT DUAL SCANNER RESULTS")
    print("================================")

    print(
        "Stocks requested:",
        len(tickers)
    )

    print(
        "Stocks with usable data:",
        len(tickers)
        - len(errors)
    )

    print(
        "Qualified A+ setups:",
        len(
            qualified
        )
    )

    print(
        "  EMA20 reclaim:",
        reclaim_count
    )

    print(
        "  Momentum breakout:",
        breakout_count
    )

    print(
        "Watchlist:",
        len(
            watch
        )
    )

    print(
        "Ignored:",
        len(
            ignored
        )
    )

    print(
        "Data errors/missing:",
        len(
            errors
        )
    )

    if qualified:

        print()
        print(
            "FINAL QUALIFIED STOCKS"
        )

        for number, stock in enumerate(
            qualified,
            start=1
        ):

            print(
                f"{number}. "
                f"{stock['ticker']} "
                f"| {stock['sector']} "
                f"| {stock['setup_type']} "
                f"| Setup {stock['setup_score']}/100 "
                f"| ${stock['price']} "
                f"| RSI {stock['rsi']} "
                f"| Vol {stock['volume_ratio']}x"
            )

    payload = {
        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "strategy":
            "STRICT_DUAL_DYNAMIC_200",

        "universe_size":
            len(tickers),

        "rules": {
            "minimum_setup_score":
                MIN_SETUP_SCORE,

            "ema20_reclaim": {
                "rsi_range": [
                    RECLAIM_MIN_RSI,
                    RECLAIM_MAX_RSI
                ],

                "minimum_volume_ratio":
                    RECLAIM_MIN_VOLUME_RATIO
            },

            "momentum_breakout": {
                "lookback_days":
                    BREAKOUT_LOOKBACK,

                "rsi_range": [
                    BREAKOUT_MIN_RSI,
                    BREAKOUT_MAX_RSI
                ],

                "minimum_volume_ratio":
                    BREAKOUT_MIN_VOLUME_RATIO
            }
        },

        "market": {
            "ticker":
                "SPY",

            "bullish":
                bool(
                    market_bullish
                ),

            "return_20":
                round(
                    float(
                        spy_return_20
                    ),
                    2
                )
        },

        "qualified":
            qualified,

        "watch":
            watch,

        "errors":
            errors
    }

    with QUALIFIED_SETUPS_JSON.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            payload,
            file,
            indent=2
        )

    print()
    print(
        "Results saved to:"
    )

    print(
        QUALIFIED_SETUPS_JSON
    )


if __name__ == "__main__":
    main()
