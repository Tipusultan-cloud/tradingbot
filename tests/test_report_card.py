import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper_journal import close_trade, record_entry
from report_card import BACKTEST, bot_stats, build_report, format_report, trade_r, verdict

NOW = datetime(2026, 11, 6, 23, 0, tzinfo=timezone.utc)


class ReportCardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "journal.sqlite3"
        self.count = 0

    def closed(self, strategy, exit_price, days_ago, note=None):
        """10 shares bought at 100 with the stop at 97: 1R = $30."""
        self.count += 1
        trade = record_entry(
            f"T{self.count}", 100, 2, 10, 97, 106, database_path=self.database,
            strategy=strategy,
        )
        close_trade(
            trade["id"], exit_price, "TARGET" if exit_price > 100 else "STOP",
            (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            database_path=self.database,
        )

    def test_r_is_pnl_over_planned_risk(self):
        self.assertAlmostEqual(
            trade_r({"entry_price": 100, "stop_price": 97, "shares": 10, "realized_pnl": 60}), 2.0
        )
        self.assertIsNone(trade_r({"entry_price": 100, "stop_price": 100, "shares": 10, "realized_pnl": 5}))

    def test_bot_stats(self):
        stats = bot_stats([
            {"ticker": "A", "entry_price": 100, "stop_price": 97, "shares": 10, "realized_pnl": 60},
            {"ticker": "B", "entry_price": 100, "stop_price": 97, "shares": 10, "realized_pnl": -30},
        ])
        self.assertEqual((stats["wins"], stats["losses"], stats["win_rate"]), (1, 1, 50.0))
        self.assertAlmostEqual(stats["avg_win_r"], 2.0)
        self.assertAlmostEqual(stats["avg_loss_r"], -1.0)
        self.assertAlmostEqual(stats["expectancy_r"], 0.5)
        self.assertEqual((stats["best"]["ticker"], stats["worst"]["ticker"]), ("A", "B"))

    def test_verdict_waits_for_ten_trades(self):
        stats = bot_stats([])
        self.assertEqual(verdict(stats, BACKTEST["PULLBACK"])[0], "EARLY")

    def test_verdict_on_track_and_below(self):
        good = {"closed": 12, "win_rate": 45.0, "expectancy_r": 0.25}
        self.assertEqual(verdict(good, BACKTEST["PULLBACK"])[0], "ON_TRACK")
        bad = {"closed": 12, "win_rate": 30.0, "expectancy_r": 0.05}
        code, text = verdict(bad, BACKTEST["PULLBACK"])
        self.assertEqual(code, "BELOW")
        self.assertIn("win rate 30%", text)
        self.assertIn("+0.05R per trade", text)

    def test_bot_3_has_no_r_benchmark(self):
        stats = {"closed": 12, "win_rate": 30.0, "expectancy_r": -0.1}
        self.assertEqual(verdict(stats, BACKTEST["MOMENTUM"])[0], "ON_TRACK")

    def test_report_splits_bots_and_this_week(self):
        self.closed("PULLBACK", 106, days_ago=2)    # +2R this week
        self.closed("PULLBACK", 97, days_ago=10)    # -1R, older
        self.closed("MOMENTUM", 115, days_ago=1)    # +5R this week
        report = build_report(self.database, now=NOW)
        bot2 = report["bots"]["PULLBACK"]
        self.assertEqual(bot2["all_time"]["closed"], 2)
        self.assertEqual(bot2["week"]["closed"], 1)
        self.assertAlmostEqual(report["total"]["week"]["total_r"], 7.0)
        self.assertEqual(sorted(t["ticker"] for t in report["week_trades"]), ["T1", "T3"])
        text = format_report(report)
        self.assertIn("This week: 2 closed trade(s), 2 won, +7.00R", text)
        self.assertIn("Bot 2 - Dip Buyer (open: 0)", text)


if __name__ == "__main__":
    unittest.main()
