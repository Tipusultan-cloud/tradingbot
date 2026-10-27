"""US-listed common stocks as a source for the daily universe.

Used by universe_builder.py when UNIVERSE_SOURCE in .env is:
  US_ALL        every liquid US-listed common stock
  SP500_TOP200  the S&P 500 plus the 200 most-traded other US stocks

  1. Symbol lists from Nasdaq Trader cover NASDAQ, NYSE, NYSE
     American, NYSE Arca and Cboe listings. ETFs, test issues,
     warrants, units, rights, preferred shares and notes are dropped.
  2. One year of daily prices is downloaded in batches, and only
     liquid stocks are kept (price and dollar-volume floors below).
  3. Each liquid stock needs a sector for the sector-ETF rules. S&P 500
     members use their GICS sector; others are looked up on Yahoo
     Finance once and cached in data/sector_cache.json. Stocks whose
     sector cannot be found are skipped.
"""

import json
import re
from io import StringIO
from urllib.request import Request, urlopen

import pandas as pd

from project_paths import DATA_DIR

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

MIN_PRICE = 5.0
MIN_AVG_DOLLAR_VOLUME_20 = 20_000_000
MIN_HISTORY_BARS = 201

DOWNLOAD_BATCH_SIZE = 200

SECTOR_CACHE_JSON = DATA_DIR / "sector_cache.json"

# SP500_TOP200: how many non-S&P stocks to add, by dollar volume.
TOP_TRADED_COUNT = 200
SECTOR_LOOKUP_CHUNK = 50

EXCLUDED_NAME = re.compile(
    r"\b(?:warrants?|units?|rights?|preferred|preference|notes?|debentures?|"
    r"subordinated)\b",
    re.IGNORECASE,
)

# Yahoo Finance sector names -> GICS names used by SECTOR_ETFS.
YAHOO_TO_GICS = {
    "Technology": "Information Technology",
    "Communication Services": "Communication Services",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Energy": "Energy",
    "Financial Services": "Financials",
    "Healthcare": "Health Care",
    "Industrials": "Industrials",
    "Basic Materials": "Materials",
    "Real Estate": "Real Estate",
    "Utilities": "Utilities",
}


def _fetch_text(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 TradingBot"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="ignore")


def _parse_symbol_file(text, symbol_column):
    # The last line is "File Creation Time: ...".
    lines = [line for line in text.splitlines() if not line.startswith("File Creation")]
    frame = pd.read_csv(StringIO("\n".join(lines)), sep="|", dtype=str).fillna("")
    frame = frame[(frame["ETF"] == "N") & (frame["Test Issue"] == "N")]
    return pd.DataFrame({
        "symbol": frame[symbol_column].str.strip(),
        "company": frame["Security Name"].str.strip(),
    })


def load_us_listed_stocks(fetch=_fetch_text):
    """Common stocks listed on US exchanges: ticker and company name."""
    frame = pd.concat([
        _parse_symbol_file(fetch(NASDAQ_LISTED_URL), "Symbol"),
        _parse_symbol_file(fetch(OTHER_LISTED_URL), "ACT Symbol"),
    ])
    # Share classes use "." (BRK.B); "$" and other marks are preferreds.
    frame = frame[frame["symbol"].str.fullmatch(r"[A-Z]+(\.[A-Z])?")]
    frame = frame[~frame["company"].str.contains(EXCLUDED_NAME)]
    frame["ticker"] = frame["symbol"].str.replace(".", "-", regex=False)
    return (
        frame[["ticker", "company"]]
        .drop_duplicates(subset=["ticker"])
        .sort_values("ticker")
        .reset_index(drop=True)
    )


def download_daily_bars(tickers, download, extract):
    """One year of completed daily bars per ticker, downloaded in batches."""
    frames = {}
    for start in range(0, len(tickers), DOWNLOAD_BATCH_SIZE):
        batch = tickers[start:start + DOWNLOAD_BATCH_SIZE]
        print(f"Downloading {start + 1}-{start + len(batch)} of {len(tickers)}...")
        try:
            data = download(batch)
        except Exception as exc:  # yfinance raises many error types
            print(f"Batch download failed, skipping these tickers: {exc}")
            continue
        for ticker in batch:
            frame = extract(data, ticker)
            if not frame.empty:
                frames[ticker] = frame
    return frames


def avg_dollar_volume_20(frame):
    recent = frame.tail(20)
    return float((recent["Close"] * recent["Volume"]).mean())


def is_liquid(frame):
    if len(frame) < MIN_HISTORY_BARS:
        return False
    return (
        float(frame["Close"].iloc[-1]) >= MIN_PRICE
        and avg_dollar_volume_20(frame) >= MIN_AVG_DOLLAR_VOLUME_20
    )


def _yahoo_sector(ticker):
    import yfinance as yf

    info = yf.Ticker(ticker).info or {}
    # A refused request (e.g. HTTP 401) returns empty info instead of
    # raising; raise so the miss is retried rather than cached.
    if "quoteType" not in info:
        raise LookupError("Yahoo returned no data")
    return info.get("sector")


def load_sector_cache():
    if not SECTOR_CACHE_JSON.exists():
        return {}
    with SECTOR_CACHE_JSON.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_sector_cache(cache):
    with SECTOR_CACHE_JSON.open("w", encoding="utf-8") as file:
        json.dump(cache, file, indent=2, sort_keys=True)


def lookup_sectors(tickers, known, fetch_sector=None):
    """GICS sector per ticker; None when it cannot be determined.

    `known` holds sectors already known (S&P 500 GICS). Yahoo results,
    including misses, are cached so each ticker is looked up once.
    """
    fetch_sector = fetch_sector or _yahoo_sector
    cache = load_sector_cache()
    missing = [t for t in tickers if t not in known and t not in cache]
    if missing:
        print(f"Looking up sectors for {len(missing)} new ticker(s)...")
    for index, ticker in enumerate(missing, start=1):
        if index % 100 == 0:
            print(f"Sectors: {index} / {len(missing)}")
            save_sector_cache(cache)
        try:
            cache[ticker] = YAHOO_TO_GICS.get(fetch_sector(ticker))
        except Exception as exc:  # yfinance raises many error types
            print(f"Sector lookup failed for {ticker}: {exc}")
            continue  # Not cached: retried on the next run.
    if missing:
        save_sector_cache(cache)
    return {t: known.get(t) or cache.get(t) for t in tickers}


def build_us_market_constituents(sp500, download, extract):
    """Liquid US common stocks with sectors, plus their daily bars.

    Returns (constituents, frames) where constituents has ticker,
    company, sector and frames maps ticker -> daily bars.
    """
    listed = load_us_listed_stocks()
    print(f"US-listed common stocks: {len(listed)}")

    tickers = sorted(set(listed["ticker"]) | set(sp500["ticker"]))
    frames = download_daily_bars(tickers, download, extract)
    liquid = sorted(t for t, frame in frames.items() if is_liquid(frame))
    print(
        f"Liquid stocks (price >= ${MIN_PRICE:.0f}, "
        f"20-day $ volume >= ${MIN_AVG_DOLLAR_VOLUME_20 / 1e6:.0f}M): {len(liquid)}"
    )

    gics = dict(zip(sp500["ticker"], sp500["sector"]))
    sectors = lookup_sectors(liquid, known=gics)

    names = dict(zip(listed["ticker"], listed["company"]))
    names.update(zip(sp500["ticker"], sp500["company"]))
    constituents = pd.DataFrame(
        [
            {"ticker": t, "company": names.get(t, t), "sector": sectors[t]}
            for t in liquid
            if sectors[t]
        ]
    )
    skipped = len(liquid) - len(constituents)
    print(f"With a known sector: {len(constituents)} (skipped {skipped})")
    return constituents, {t: frames[t] for t in constituents["ticker"]}


def build_sp500_top_traded_constituents(
    sp500, download, extract, count=TOP_TRADED_COUNT, listed=None
):
    """S&P 500 plus the `count` most-traded other US stocks, with bars.

    "Most traded" = highest 20-day average dollar volume among liquid
    non-S&P common stocks that have a known sector. Returns
    (constituents, frames) like build_us_market_constituents.
    """
    if listed is None:
        listed = load_us_listed_stocks()
    sp500_tickers = set(sp500["ticker"])
    others = listed[~listed["ticker"].isin(sp500_tickers)]

    frames = download_daily_bars(
        sorted(sp500_tickers | set(others["ticker"])), download, extract
    )
    ranked = sorted(
        (t for t in others["ticker"] if t in frames and is_liquid(frames[t])),
        key=lambda t: avg_dollar_volume_20(frames[t]),
        reverse=True,
    )

    # Look up sectors in the ranked order until enough stocks have one.
    chosen = []
    sectors = {}
    for start in range(0, len(ranked), SECTOR_LOOKUP_CHUNK):
        chunk = ranked[start:start + SECTOR_LOOKUP_CHUNK]
        sectors.update(lookup_sectors(chunk, known={}))
        chosen += [t for t in chunk if sectors[t]]
        if len(chosen) >= count:
            break
    chosen = chosen[:count]

    names = dict(zip(others["ticker"], others["company"]))
    top_traded = pd.DataFrame(
        [{"ticker": t, "company": names[t], "sector": sectors[t]} for t in chosen],
        columns=["ticker", "company", "sector"],
    )
    sp500_with_data = sp500[sp500["ticker"].isin(frames)]
    constituents = pd.concat(
        [sp500_with_data[["ticker", "company", "sector"]], top_traded],
        ignore_index=True,
    )
    print(
        f"Universe source: {len(sp500_with_data)} S&P 500 + "
        f"{len(top_traded)} most-traded other US stocks"
    )
    return constituents, {t: frames[t] for t in constituents["ticker"]}
