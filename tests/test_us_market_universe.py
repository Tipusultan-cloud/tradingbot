import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import us_market_universe
from us_market_universe import (
    is_liquid,
    load_us_listed_stocks,
    lookup_sectors,
)

NASDAQ_LISTED = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
ABCDW|Some Acquisition Corp - Warrants|S|N|N|100|N|N
ABCDU|Some Acquisition Corp - Units|S|N|N|100|N|N
UAL|United Airlines Holdings, Inc. - Common Stock|Q|N|N|100|N|N
File Creation Time: 1026202621:31|||||||"""

OTHER_LISTED = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. New Common Stock|N|BRK.B|N|100|N|BRK=B
ABR$D|Arbor Realty Trust 6.375% Series D Preferred|N|ABR-D|N|100|N|ABR-D
JPM|JP Morgan Chase & Co. Common Stock|N|JPM|N|100|N|JPM
XYZN|Example Corp 5.25% Senior Notes due 2030|N|XYZN|N|100|N|XYZN
File Creation Time: 1026202621:31||||||"""


def fake_fetch(url):
    return NASDAQ_LISTED if "nasdaqlisted" in url else OTHER_LISTED


def daily(close=50.0, volume=1_000_000, bars=250):
    return pd.DataFrame(
        {"Close": [close] * bars, "Volume": [volume] * bars},
        index=pd.bdate_range("2025-01-01", periods=bars),
    )


class ListedStocksTests(unittest.TestCase):
    def test_keeps_common_stocks_only(self):
        stocks = load_us_listed_stocks(fetch=fake_fetch)
        self.assertEqual(
            stocks["ticker"].tolist(), ["AAPL", "BRK-B", "JPM", "UAL"]
        )

    def test_company_names_kept(self):
        stocks = load_us_listed_stocks(fetch=fake_fetch).set_index("ticker")
        self.assertEqual(stocks.loc["JPM", "company"], "JP Morgan Chase & Co. Common Stock")


class LiquidityTests(unittest.TestCase):
    def test_liquid_stock_passes(self):
        # $50 x 1M shares = $50M a day.
        self.assertTrue(is_liquid(daily()))

    def test_cheap_stock_fails(self):
        self.assertFalse(is_liquid(daily(close=4.0, volume=50_000_000)))

    def test_thin_volume_fails(self):
        # $50 x 100k = $5M a day.
        self.assertFalse(is_liquid(daily(volume=100_000)))

    def test_short_history_fails(self):
        self.assertFalse(is_liquid(daily(bars=150)))


class SectorLookupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(
            us_market_universe,
            "SECTOR_CACHE_JSON",
            Path(folder.name) / "sector_cache.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def yahoo(self, ticker):
        self.calls.append(ticker)
        return {"PLTR": "Technology", "HOOD": "Financial Services"}.get(ticker)

    def test_known_sectors_skip_lookup_and_yahoo_names_are_mapped(self):
        sectors = lookup_sectors(
            ["AAPL", "PLTR", "HOOD"],
            known={"AAPL": "Information Technology"},
            fetch_sector=self.yahoo,
        )
        self.assertEqual(
            sectors,
            {
                "AAPL": "Information Technology",
                "PLTR": "Information Technology",
                "HOOD": "Financials",
            },
        )
        self.assertEqual(self.calls, ["PLTR", "HOOD"])

    def test_results_and_misses_are_cached(self):
        lookup_sectors(["PLTR", "NOSECTOR"], known={}, fetch_sector=self.yahoo)
        sectors = lookup_sectors(["PLTR", "NOSECTOR"], known={}, fetch_sector=self.yahoo)
        self.assertEqual(sectors, {"PLTR": "Information Technology", "NOSECTOR": None})
        self.assertEqual(self.calls, ["PLTR", "NOSECTOR"])

    def test_failed_lookup_is_retried_next_run(self):
        def failing(ticker):
            raise ConnectionError("rate limited")

        self.assertEqual(
            lookup_sectors(["PLTR"], known={}, fetch_sector=failing), {"PLTR": None}
        )
        lookup_sectors(["PLTR"], known={}, fetch_sector=self.yahoo)
        self.assertEqual(self.calls, ["PLTR"])


class YahooSectorTests(unittest.TestCase):
    def sector(self, info):
        fake = types.SimpleNamespace(Ticker=lambda ticker: types.SimpleNamespace(info=info))
        with patch.dict(sys.modules, {"yfinance": fake}):
            return us_market_universe._yahoo_sector("TEST")

    def test_returns_sector(self):
        self.assertEqual(
            self.sector({"quoteType": "EQUITY", "sector": "Technology"}), "Technology"
        )

    def test_real_answer_without_sector_is_none(self):
        self.assertIsNone(self.sector({"quoteType": "ETF"}))

    def test_refused_request_raises_so_it_is_not_cached(self):
        for info in ({}, None, {"trailingPegRatio": None}):
            with self.assertRaises(LookupError):
                self.sector(info)


class TopTradedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(
            us_market_universe,
            "SECTOR_CACHE_JSON",
            Path(folder.name) / "sector_cache.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, bars, yahoo_sectors, count):
        sp500 = pd.DataFrame([
            {"ticker": "AAPL", "company": "Apple", "sector": "Information Technology"},
            {"ticker": "THIN", "company": "Thin S&P", "sector": "Utilities"},
        ])
        listed = pd.DataFrame(
            [{"ticker": t, "company": f"{t} Inc"} for t in
             ["AAPL", "THIN", "BIG", "MID", "SMALL", "NOSECT", "PENNY"]]
        )
        with (
            patch("builtins.print"),
            patch.object(
                us_market_universe, "_yahoo_sector", side_effect=yahoo_sectors.get
            ),
        ):
            return us_market_universe.build_sp500_top_traded_constituents(
                sp500,
                download=lambda batch: bars,
                extract=lambda data, ticker: data.get(ticker, pd.DataFrame()),
                count=count,
                listed=listed,
            )

    def test_adds_most_traded_non_sp500_stocks(self):
        bars = {
            "AAPL": daily(),
            "THIN": daily(volume=10_000),             # S&P: kept even if thin.
            "BIG": daily(volume=10_000_000),          # $500M a day.
            "NOSECT": daily(volume=8_000_000),        # No sector: skipped.
            "MID": daily(volume=5_000_000),           # $250M a day.
            "SMALL": daily(volume=1_000_000),         # $50M a day.
            "PENNY": daily(close=2.0, volume=90_000_000),  # Under $5.
        }
        sectors = {"BIG": "Technology", "MID": "Healthcare", "SMALL": "Energy"}
        constituents, frames = self.build(bars, sectors, count=2)
        self.assertEqual(
            constituents["ticker"].tolist(), ["AAPL", "THIN", "BIG", "MID"]
        )
        self.assertEqual(
            constituents.set_index("ticker").loc["MID", "sector"], "Health Care"
        )
        self.assertEqual(sorted(frames), ["AAPL", "BIG", "MID", "THIN"])

    def test_fewer_candidates_than_count(self):
        bars = {"AAPL": daily(), "THIN": daily(), "BIG": daily(volume=10_000_000)}
        constituents, _ = self.build(bars, {"BIG": "Technology"}, count=200)
        self.assertEqual(constituents["ticker"].tolist(), ["AAPL", "THIN", "BIG"])


if __name__ == "__main__":
    unittest.main()
