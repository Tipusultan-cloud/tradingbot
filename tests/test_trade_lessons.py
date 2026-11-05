import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

import trade_lessons
from paper_journal import (
    close_trade,
    export_paper_trades_csv,
    get_closed_trades,
    record_entry,
    save_daily_copy,
    set_trade_note,
)
from trade_manager import entry_reason


def bars(closes, start="2026-10-29"):
    return pd.DataFrame(
        {"Close": closes}, index=pd.bdate_range(start, periods=len(closes))
    )


# Bought Oct 30 at 100, stop 97 (risk $3/share, 10 shares = $30 = 1R).
TRADE = {
    "id": 1, "ticker": "ABC", "strategy": "PULLBACK", "shares": 10,
    "entry_time": "2026-10-30T13:58:00Z", "entry_price": 100.0, "stop_price": 97.0,
}


def closed(exit_price, reason, exit_time="2026-11-03T15:56:00Z", **extra):
    return {
        **TRADE, "exit_price": exit_price, "exit_reason": reason, "exit_time": exit_time,
        "realized_pnl": (exit_price - 100.0) * 10, **extra,
    }


class BuildLessonTests(unittest.TestCase):
    def test_stop_through_earnings(self):
        # MPC on 2026-11-03: held into the report, gapped to the stop.
        text = trade_lessons.build_lesson(
            closed(96.9, "STOP"),
            bars([101, 100, 99.5, 98, 96.9]),       # Oct 29 .. Nov 4
            bars([500, 500, 501, 503, 504]),
            [date(2026, 11, 3)],
        )
        self.assertTrue(text.startswith("Auto: Lost 1.03R ($-31.00) in 3 sessions: the stop was hit."))
        self.assertIn("Earnings came out on Nov 03 during the trade.", text)
        self.assertIn("SPY +0.6% over the same days (ABC -3.1%).", text)
        self.assertIn("Takeaway: holding through earnings", text)

    def test_gain_given_back(self):
        text = trade_lessons.build_lesson(
            closed(97.0, "STOP"), bars([100, 100, 104, 101, 97]), bars([1, 1, 1, 1, 1]),
        )
        self.assertIn("Best close +1.3R", text)
        self.assertIn("it was up 1.3R and gave it all back", text)

    def test_stock_weak_while_market_rose(self):
        text = trade_lessons.build_lesson(
            closed(97.0, "STOP"), bars([100, 100, 99, 98, 97]), bars([100, 100, 101, 102, 103]),
        )
        self.assertIn("weakness in the stock itself", text)
        self.assertIn("The dip kept falling after the buy.", text)

    def test_market_wide_drop(self):
        text = trade_lessons.build_lesson(
            closed(97.0, "STOP"), bars([100, 100, 99, 98, 97]), bars([100, 100, 98, 97, 96]),
        )
        self.assertIn("a market-wide drop", text)

    def test_target_hit(self):
        text = trade_lessons.build_lesson(
            closed(106.0, "PARTIAL+TARGET"), bars([100, 100, 103, 105, 107]), bars([1, 1, 1, 1, 1]),
        )
        self.assertIn("Won 2.00R ($+60.00)", text)
        self.assertIn("half sold at 2R, then for the rest the target was hit", text)
        self.assertIn("the plan worked", text)

    def test_gap_past_the_stop(self):
        text = trade_lessons.build_lesson(closed(94.0, "STOP"))
        self.assertIn("Lost 2.00R", text)
        self.assertIn("gapped past the stop", text)

    def test_without_price_data(self):
        text = trade_lessons.build_lesson(closed(97.0, "MANUAL_SELL"))
        self.assertEqual(
            text,
            "Auto: Lost 1.00R ($-30.00): sold by hand. Takeaway: sold by hand; "
            "check whether the rules would have done better.",
        )


class JournalCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Path(self.folder.name) / "journal.sqlite3"

    def tearDown(self):
        self.folder.cleanup()

    def close_one(self, ticker="ABC"):
        trade = record_entry(
            ticker, 100.0, 2.0, 10, 97.0, 106.0,
            entry_time="2026-10-30T13:58:00Z", database_path=self.database,
            strategy="PULLBACK", sector="Energy", entry_note="Bot 2 leader pullback",
        )
        trade_id = trade["id"] if isinstance(trade, dict) else trade
        close_trade(trade_id, 97.0, "STOP", "2026-11-03T15:56:00Z", self.database)
        return trade_id


class FillLessonTests(JournalCase):
    def fill(self, fetch_bars, today=date(2026, 11, 4)):
        return trade_lessons.fill_missing_lessons(
            self.database, fetch_bars=fetch_bars,
            fetch_earnings=lambda ticker: [], today=today,
        )

    def test_writes_lesson_once(self):
        self.close_one()
        written = self.fill(lambda ticker, day: bars([100, 100, 99, 98, 97]))
        self.assertEqual(len(written), 1)
        self.assertTrue(get_closed_trades(self.database)[0]["exit_note"].startswith("Auto: Lost"))
        self.assertEqual(self.fill(lambda ticker, day: bars([1])), [])

    def test_never_replaces_a_hand_written_lesson(self):
        trade_id = self.close_one()
        set_trade_note(trade_id, "exit_note", "My own lesson", self.database)
        self.assertEqual(self.fill(lambda ticker, day: bars([1])), [])
        self.assertEqual(get_closed_trades(self.database)[0]["exit_note"], "My own lesson")

    def test_waits_for_price_data_then_gives_up(self):
        self.close_one()

        def offline(ticker, day):
            raise ConnectionError("offline")

        self.assertEqual(self.fill(offline), [])
        written = self.fill(offline, today=date(2026, 11, 12))
        self.assertEqual(len(written), 1)
        self.assertIn("the stop was hit", written[0][1])


class TradeRecordTests(JournalCase):
    def read(self, path):
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_csv_has_sector_and_notes(self):
        trade_id = self.close_one()
        set_trade_note(trade_id, "exit_note", "Lesson text", self.database)
        row = self.read(export_paper_trades_csv(self.database))[0]
        self.assertEqual(row["sector"], "Energy")
        self.assertEqual(row["entry_note"], "Bot 2 leader pullback")
        self.assertEqual(row["exit_note"], "Lesson text")

    def test_daily_copy_keeps_earlier_days(self):
        self.close_one("ABC")
        history = Path(self.folder.name) / "history"
        save_daily_copy(self.database, history, date(2026, 11, 4))
        self.close_one("XYZ")
        save_daily_copy(self.database, history, date(2026, 11, 5))
        self.assertEqual(len(self.read(history / "paper_trades_2026-11-04.csv")), 1)
        self.assertEqual(len(self.read(history / "paper_trades_2026-11-05.csv")), 2)

    def test_test_journal_copies_stay_next_to_it(self):
        self.close_one()
        path = save_daily_copy(self.database, today=date(2026, 11, 4))
        self.assertEqual(path.parent, Path(self.folder.name) / "trade_history")


class EntryReasonTests(unittest.TestCase):
    def test_warnings_and_hand_pick_are_recorded(self):
        stock = {
            "return_6m": 75.3, "return_5d": -5.8, "sector": "Energy",
            "warnings": [{"label": "Earnings in 2d"}, {"label": "Bad news"}],
        }
        self.assertEqual(
            entry_reason(stock, "PULLBACK", "MANUAL"),
            "Bot 2 leader pullback · 6-month +75.3% · 5-day -5.8% · Energy · "
            "bought by hand · warnings: Earnings in 2d, Bad news",
        )

    def test_clean_stock(self):
        self.assertTrue(entry_reason({"sector": "Energy"}, "PULLBACK", "AUTO").endswith("Energy · no warnings"))


if __name__ == "__main__":
    unittest.main()
