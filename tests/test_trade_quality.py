"""Off-plan trades and slippage (planned scan price vs real fill)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_journal import close_trade, record_entry
from report_card import build_report, format_report, slippage_stats
from tests.test_auto_trading import JournalCase, STOCK

import trade_manager


class SlippageTests(unittest.TestCase):
    def test_average_in_percent_and_r(self):
        trades = [
            # Planned 100, filled 101, stop 98: +1% and 1/3 R.
            {"planned_price": 100.0, "entry_price": 101.0, "stop_price": 98.0},
            # Planned 50, filled 49.5, stop 48.5: -1% and -0.5 R.
            {"planned_price": 50.0, "entry_price": 49.5, "stop_price": 48.5},
            {"planned_price": None, "entry_price": 10.0, "stop_price": 9.0},
        ]
        stats = slippage_stats(trades)
        self.assertEqual(stats["buys"], 2)
        self.assertAlmostEqual(stats["avg_pct"], 0.0)
        self.assertAlmostEqual(stats["avg_r"], (1 / 3 - 0.5) / 2)

    def test_no_planned_price(self):
        self.assertEqual(slippage_stats([{"entry_price": 1, "stop_price": 0.5}])["buys"], 0)


class OffPlanReportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Path(self.folder.name) / "journal.sqlite3"

    def tearDown(self):
        self.folder.cleanup()

    def trade(self, ticker, exit_price, off_plan, planned=None):
        trade = record_entry(
            ticker, 100.0, 2.0, 10, 97.0, 106.0, entry_time="2026-11-02T14:40:00Z",
            database_path=self.database, strategy="PULLBACK",
            planned_price=planned, off_plan=off_plan,
        )
        close_trade(trade["id"], exit_price, "STOP" if exit_price < 100 else "TARGET",
                    "2026-11-04T15:00:00Z", self.database)

    def test_off_plan_trades_do_not_score_the_bot(self):
        self.trade("MPC", 97.0, off_plan=True)
        self.trade("VLO", 106.0, off_plan=False, planned=99.5)
        report = build_report(self.database)
        bot2 = report["bots"]["PULLBACK"]
        self.assertEqual(bot2["all_time"]["closed"], 1)
        self.assertEqual(bot2["all_time"]["wins"], 1)
        self.assertEqual(report["off_plan"]["closed"], 1)
        self.assertEqual(report["off_plan"]["tickers"], ["MPC"])
        self.assertEqual(bot2["slippage"]["buys"], 1)
        text = format_report(report)
        self.assertIn("Off-plan trades (not counted above): 1 closed (MPC)", text)
        self.assertIn("Slippage: +0.50% (+0.17R) a buy, over 1 buy(s)", text)


class PlannedPriceCarryTests(JournalCase):
    def test_scan_price_and_off_plan_carry_to_the_trade(self):
        stock = {**STOCK, "price": 99.0}
        trade_manager.open_trade(
            self.client, stock, "PULLBACK", "MANUAL", lambda ticker: 100.0,
            self.database, off_plan=True,
        )
        self.client.fill("order-1", 100.4, 33)
        trade = trade_manager.sync(self.client, lambda ticker: 100.4, self.database)["filled"][0]
        self.assertEqual(trade["planned_price"], 99.0)
        self.assertEqual(trade["off_plan"], 1)

    def test_risk_preview_price_when_no_scan_price(self):
        stock = {**STOCK, "risk_preview": {"entry_reference": 98.5}}
        trade_manager.open_trade(
            self.client, stock, "PULLBACK", "AUTO", lambda ticker: 100.0, self.database,
        )
        self.client.fill("order-1", 100.0, 33)
        trade = trade_manager.sync(self.client, lambda ticker: 100.0, self.database)["filled"][0]
        self.assertEqual(trade["planned_price"], 98.5)
        self.assertEqual(trade["off_plan"], 0)


if __name__ == "__main__":
    unittest.main()
