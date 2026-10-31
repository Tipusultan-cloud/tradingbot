import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from paper_exits import close_paper_trades, market_close_utc
from paper_journal import close_trade, get_paper_trades, record_entry, record_exit

# Monday 2026-01-05, 15:00 UTC = 10:00 New York.
ENTRY_TIME = "2026-01-05T15:00:00Z"
ENTRY_DATE = date(2026, 1, 5)
# Entry 100, stop 97 -> risk 3/share; half (5 shares) at 2R = 106, the
# rest at 2.5R = 107.5.


def bars(closes):
    """Daily bars from the entry day; each day opens at the previous close."""
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "Open": opens,
            "High": np.maximum(opens, closes),
            "Low": np.minimum(opens, closes),
            "Close": closes,
            "ATR": 2.0,
        },
        index=pd.bdate_range(ENTRY_DATE, periods=len(closes)),
    )


class PaperExitTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Path(self.folder.name) / "paper.sqlite3"

    def tearDown(self):
        self.folder.cleanup()

    def open_trade(self, ticker="ABC", strategy="PULLBACK"):
        return record_entry(
            ticker, 100, 2, 10, 97, 106,
            entry_time=ENTRY_TIME,
            database_path=self.database,
            strategy=strategy,
        )

    def run_exits(self, prices):
        return close_paper_trades(
            database_path=self.database,
            fetch=lambda ticker, entry_date: prices[ticker],
        )

    def test_stop_hit_closes_trade(self):
        self.open_trade()

        result = self.run_exits({"ABC": bars([100, 99, 96.5, 95])})

        trade = result["closed"][0]
        self.assertEqual(trade["status"], "CLOSED")
        self.assertEqual(trade["exit_reason"], "STOP")
        self.assertEqual(trade["exit_price"], 97)
        self.assertAlmostEqual(trade["realized_pnl"], -30)
        self.assertEqual(trade["exit_time"], "2026-01-07T21:00:00Z")
        self.assertEqual(trade["strategy"], "PULLBACK")

    def test_2r_sells_half_and_moves_stop_to_entry(self):
        self.open_trade()

        result = self.run_exits({"ABC": bars([100, 103, 107])})

        self.assertEqual(result["closed"], [])
        self.assertEqual(result["still_open"], ["ABC"])
        trade = get_paper_trades(database_path=self.database)["open"][0]
        self.assertEqual((trade["partial_shares"], trade["partial_exit_price"]), (5, 106))
        self.assertEqual(trade["partial_exit_time"], "2026-01-07T21:00:00Z")
        self.assertEqual(trade["active_stop_price"], 100)
        self.assertEqual((trade["shares_open"], trade["risk_at_stop"]), (5, 0))

    def test_rest_sells_at_2_5r(self):
        self.open_trade()
        self.run_exits({"ABC": bars([100, 103, 107])})

        trade = self.run_exits({"ABC": bars([100, 103, 107, 108])})["closed"][0]

        self.assertEqual(trade["exit_reason"], "PARTIAL+TARGET")
        self.assertEqual(trade["exit_price"], 107.5)
        self.assertAlmostEqual(trade["realized_pnl"], 5 * 6 + 5 * 7.5)

    def test_half_and_rest_on_the_same_day(self):
        self.open_trade()

        result = self.run_exits({"ABC": bars([100, 103, 108])})

        self.assertEqual(len(result["halves"]), 1)
        self.assertEqual(result["closed"][0]["exit_reason"], "PARTIAL+TARGET")
        self.assertAlmostEqual(result["closed"][0]["realized_pnl"], 67.5)

    def test_rest_stops_at_entry_after_half(self):
        self.open_trade()

        trade = self.run_exits({"ABC": bars([100, 103, 107, 99])})["closed"][0]

        # A stop above the original one is named TRAIL_STOP by the replay.
        self.assertEqual(trade["exit_reason"], "PARTIAL+TRAIL_STOP")
        self.assertEqual(trade["exit_price"], 100)
        self.assertAlmostEqual(trade["realized_pnl"], 30)

    def test_gap_below_stop_fills_at_open(self):
        self.open_trade()
        prices = bars([100, 99, 99])
        prices.loc[prices.index[2], ["Open", "Low"]] = 94.0

        trade = self.run_exits({"ABC": prices})["closed"][0]

        self.assertEqual(trade["exit_reason"], "GAP_STOP")
        self.assertEqual(trade["exit_price"], 94)

    def test_time_exit_after_20_sessions(self):
        self.open_trade()

        trade = self.run_exits({"ABC": bars([100] + [101] * 25)})["closed"][0]

        self.assertEqual(trade["exit_reason"], "TIME_EXIT")
        self.assertEqual(trade["exit_price"], 101)
        self.assertEqual(trade["exit_time"][:10], "2026-01-30")

    def test_trade_inside_range_stays_open(self):
        self.open_trade()

        result = self.run_exits({"ABC": bars([100, 101, 99, 102])})

        self.assertEqual(result["closed"], [])
        self.assertEqual(result["still_open"], ["ABC"])
        self.assertEqual(len(get_paper_trades(database_path=self.database)["open"]), 1)

    def test_entry_day_low_does_not_stop_out(self):
        self.open_trade()
        prices = bars([100, 101])
        prices.loc[prices.index[0], "Low"] = 95.0

        self.assertEqual(self.run_exits({"ABC": prices})["closed"], [])

    def test_price_failure_keeps_trade_open(self):
        self.open_trade()

        def broken(ticker, entry_date):
            raise ConnectionError("offline")

        result = close_paper_trades(database_path=self.database, fetch=broken)

        self.assertEqual(result["closed"], [])
        self.assertIn("offline", result["failures"][0])
        self.assertEqual(len(get_paper_trades(database_path=self.database)["open"]), 1)

    def test_empty_prices_are_a_failure(self):
        self.open_trade()

        result = self.run_exits({"ABC": pd.DataFrame()})

        self.assertIn("no price data", result["failures"][0])

    def test_trade_closed_by_tradingview_is_left_alone(self):
        self.open_trade()
        record_exit("ABC", 104, database_path=self.database)

        result = self.run_exits({"ABC": bars([100, 96])})

        self.assertEqual(result["closed"], [])
        trade = get_paper_trades(database_path=self.database)["recent"][0]
        self.assertEqual(trade["exit_reason"], "TRADINGVIEW_SELL")

    def test_only_the_triggered_trade_closes(self):
        self.open_trade("ABC")
        self.open_trade("XYZ", strategy="A_PLUS")

        result = self.run_exits({
            "ABC": bars([100, 96]),
            "XYZ": bars([100, 101]),
        })

        self.assertEqual([t["ticker"] for t in result["closed"]], ["ABC"])
        self.assertEqual(result["still_open"], ["XYZ"])

    def test_close_trade_ignores_already_closed_trade(self):
        trade = self.open_trade()
        first = close_trade(
            trade["id"], 97, "STOP", "2026-01-07T21:00:00Z",
            database_path=self.database,
        )
        second = close_trade(
            trade["id"], 95, "STOP", "2026-01-08T21:00:00Z",
            database_path=self.database,
        )

        self.assertEqual(first["exit_price"], 97)
        self.assertIsNone(second)

    def test_close_time_follows_daylight_saving(self):
        self.assertEqual(market_close_utc("2026-01-07"), "2026-01-07T21:00:00Z")
        self.assertEqual(market_close_utc("2026-07-07"), "2026-07-07T20:00:00Z")


if __name__ == "__main__":
    unittest.main()
