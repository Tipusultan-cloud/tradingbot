import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_journal
from paper_journal import (
    DuplicateSignalError,
    OpenTradeExistsError,
    get_paper_trades,
    record_entry,
    record_exit,
)


class PaperJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "paper.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def open_trade(self):
        return record_entry(
            ticker="abc",
            entry_price=100,
            atr=2,
            shares=10,
            stop_price=97,
            target_price=106,
            setup_score=96,
            database_path=self.database,
        )

    def test_persists_paper_entry_and_calculates_realized_pnl(self):
        opened = self.open_trade()
        csv_path = self.database.with_suffix(".csv")
        with csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
            open_export = list(csv.DictReader(csv_file))
        self.assertEqual(open_export[0]["status"], "OPEN")

        closed = record_exit(
            "ABC",
            106,
            database_path=self.database,
        )
        trades = get_paper_trades(database_path=self.database)

        self.assertEqual(opened["status"], "OPEN")
        self.assertEqual(closed["status"], "CLOSED")
        self.assertEqual(closed["realized_pnl"], 60)
        self.assertEqual(trades["open"], [])
        self.assertEqual(trades["recent"][0]["ticker"], "ABC")
        self.assertEqual(trades["summary"]["total_trades"], 1)
        self.assertEqual(trades["summary"]["closed_trades"], 1)
        self.assertEqual(trades["summary"]["realized_pnl"], 60)
        self.assertEqual(trades["summary"]["wins"], 1)
        self.assertEqual(trades["summary"]["win_rate"], 100)
        self.assertEqual(trades["summary"]["open_risk_at_stop"], 0)
        with csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
            exported = list(csv.DictReader(csv_file))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["ticker"], "ABC")
        self.assertEqual(exported[0]["status"], "CLOSED")
        self.assertEqual(float(exported[0]["realized_pnl"]), 60)

    def test_empty_journal_has_zero_summary_and_no_win_rate(self):
        trades = get_paper_trades(database_path=self.database)

        self.assertEqual(trades["summary"]["total_trades"], 0)
        self.assertEqual(trades["summary"]["realized_pnl"], 0)
        self.assertEqual(trades["summary"]["open_risk_at_stop"], 0)
        self.assertIsNone(trades["summary"]["win_rate"])

    def test_open_risk_at_stop_uses_entry_to_stop_distance(self):
        self.open_trade()

        trades = get_paper_trades(database_path=self.database)

        self.assertEqual(trades["summary"]["open_risk_at_stop"], 30)
        self.assertEqual(trades["open"][0]["risk_at_stop"], 30)

    def test_prevents_duplicate_buy_signal(self):
        self.open_trade()

        with self.assertRaises(DuplicateSignalError):
            self.open_trade()

    def test_prevents_second_open_trade_for_ticker(self):
        self.open_trade()

        with patch.object(paper_journal.time, "time", return_value=10**12):
            with self.assertRaises(OpenTradeExistsError):
                record_entry(
                    ticker="ABC",
                    entry_price=101,
                    atr=2,
                    shares=5,
                    stop_price=98,
                    target_price=107,
                    setup_score=97,
                    database_path=self.database,
                )

    def test_sell_without_open_trade_is_not_journaled(self):
        result = record_exit(
            "ABC",
            100,
            database_path=self.database,
        )

        self.assertIsNone(result)

    def test_rejects_non_positive_entry_values(self):
        with self.assertRaisesRegex(ValueError, "Entry price"):
            record_entry(
                ticker="ABC",
                entry_price=float("nan"),
                atr=2,
                shares=1,
                stop_price=97,
                target_price=106,
                database_path=self.database,
            )


if __name__ == "__main__":
    unittest.main()
