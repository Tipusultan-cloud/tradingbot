"""Earnings exit, one trade per sector, account loss limit, trade notes."""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import app
import trade_manager
from paper_journal import (
    close_trade,
    get_paper_trades,
    held_sectors,
    record_entry,
    set_trade_note,
)
from risk_guard import trading_pause
from slot_selector import build_momentum_selection, build_slot_selection
from tests.test_auto_trading import GOOD_HEADERS, STOCK, FakeRequest, JournalCase
from trade_manager import TradeError

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


def earnings_in(days):
    return lambda ticker: {"trading_days_until": days, "next_earnings_date": "2026-11-04"}


class EarningsExitTests(JournalCase):
    def filled_trade(self, strategy="PULLBACK"):
        self.buy(strategy=strategy)
        self.client.fill("order-1", 100.0, 33)
        self.sync()

    def exits(self, check):
        return trade_manager.earnings_exits(
            self.client, lambda ticker: 104.0, check,
            database_path=self.database, delay=0,
        )

    def test_sells_the_morning_before_earnings(self):
        self.filled_trade()
        messages = self.exits(earnings_in(1))
        self.assertIn("SOLD BEFORE EARNINGS ABC", messages[0])
        closed = get_paper_trades(database_path=self.database)["recent"][0]
        self.assertEqual(closed["exit_reason"], "EARNINGS_EXIT")

    def test_keeps_the_trade_when_earnings_are_later(self):
        self.filled_trade()
        self.assertEqual(self.exits(earnings_in(2)), [])
        self.assertEqual(self.exits(earnings_in(None)), [])
        self.assertEqual(len(get_paper_trades(database_path=self.database)["open"]), 1)

    def test_bot_3_rides_through_earnings(self):
        self.filled_trade(strategy="MOMENTUM")
        self.assertEqual(self.exits(earnings_in(0)), [])

    def test_auto_buy_right_before_earnings_is_skipped(self):
        stock = {**STOCK, "earnings": {"trading_days_until": 2}}
        with self.assertRaisesRegex(TradeError, "earnings in 2 trading day"):
            self.buy(stock)
        self.buy({**stock, "earnings": {"trading_days_until": 3}})


class SectorLimitTests(JournalCase):
    def test_auto_buy_in_a_held_sector_is_skipped(self):
        self.buy({**STOCK, "sector": "Energy"})
        with self.assertRaisesRegex(TradeError, "Energy already has ABC"):
            self.buy({"ticker": "DEF", "atr": 2.0, "sector": "Energy"})
        self.buy({"ticker": "DEF", "atr": 2.0, "sector": "Energy"}, source="MANUAL")

    def test_sector_and_note_carry_from_order_to_trade(self):
        self.buy({**STOCK, "sector": "Energy", "return_6m": 40, "return_5d": -6})
        self.client.fill("order-1", 100.0, 33)
        trade = self.sync()["filled"][0]
        self.assertEqual(trade["sector"], "Energy")
        self.assertEqual(
            trade["entry_note"], "Bot 2 leader pullback · 6-month +40.0% · 5-day -6.0% · Energy · no warnings"
        )
        self.assertEqual(held_sectors(self.database), {"Energy": ["ABC"]})

    def test_slot_picks_skip_a_sector_already_held_or_picked(self):
        pullbacks = {"qualified": [
            {"ticker": "AAA", "sector": "Energy"},
            {"ticker": "BBB", "sector": "Utilities"},
            {"ticker": "CCC", "sector": "Utilities"},
            {"ticker": "DDD", "sector": "Materials"},
        ]}
        counts = {"Energy": 1}
        selection = build_slot_selection(None, pullbacks, [], 2, sector_counts=counts)
        self.assertEqual([s["ticker"] for s in selection["selections"]], ["BBB", "DDD"])
        momentum = {"qualified": [
            {"ticker": "EEE", "sector": "Materials"},
            {"ticker": "FFF", "sector": "Financials"},
        ]}
        picks = build_momentum_selection(momentum, [], 0, 1, sector_counts=counts)
        self.assertEqual([s["ticker"] for s in picks["momentum_selections"]], ["FFF"])

    def test_limit_of_zero_turns_the_rule_off(self):
        with patch.dict(os.environ, {"MAX_TRADES_PER_SECTOR": "0"}):
            self.buy({**STOCK, "sector": "Energy"})
            self.buy({"ticker": "DEF", "atr": 2.0, "sector": "Energy"})


class TradingPauseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "journal.sqlite3"
        self.env = patch.dict(os.environ, {"PAPER_ACCOUNT_BALANCE": "10000"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.count = 0

    def closed(self, pnl, days_ago):
        """A closed trade with `pnl` dollars, `days_ago` days before NOW."""
        self.count += 1
        ticker = f"T{self.count}"
        trade = record_entry(ticker, 100, 2, 10, 97, 106, database_path=self.database)
        close_trade(
            trade["id"], 100 + pnl / 10, "STOP" if pnl < 0 else "TARGET",
            (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            database_path=self.database,
        )

    def pause(self):
        return trading_pause(self.database, now=NOW)

    def test_no_trades_no_pause(self):
        self.assertIsNone(self.pause())

    def test_monthly_loss_over_6_pct_pauses(self):
        for days_ago in (3, 10, 20):
            self.closed(-210, days_ago)  # -$630 in 30 days, limit $600
        self.closed(30, 1)
        self.assertIn("in the last 30 days", self.pause())

    def test_old_losses_drop_out_of_the_window(self):
        for days_ago in (3, 10, 40):
            self.closed(-210, days_ago)
        self.closed(30, 1)
        self.assertIsNone(self.pause())

    def test_five_losses_in_a_row_pause_for_a_week(self):
        for days_ago in (12, 11, 10, 9, 2):
            self.closed(-50, days_ago)
        self.assertIn("last 5 closed trades were all losses", self.pause())

    def test_streak_pause_ends_after_a_week_or_a_win(self):
        for days_ago in (20, 19, 18, 17, 8):
            self.closed(-50, days_ago)
        self.assertIsNone(self.pause())
        self.closed(-50, 1)
        self.closed(80, 0)
        self.assertIsNone(self.pause())


class TradeNoteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "journal.sqlite3"

    def test_saves_and_clears_notes(self):
        trade = record_entry("ABC", 100, 2, 10, 97, 106, database_path=self.database)
        saved = set_trade_note(trade["id"], "exit_note", "  Sold too early.  ", self.database)
        self.assertEqual(saved["exit_note"], "Sold too early.")
        cleared = set_trade_note(trade["id"], "exit_note", "", self.database)
        self.assertIsNone(cleared["exit_note"])

    def test_rejects_bad_field_unknown_trade_and_long_text(self):
        trade = record_entry("ABC", 100, 2, 10, 97, 106, database_path=self.database)
        with self.assertRaises(ValueError):
            set_trade_note(trade["id"], "ticker", "x", self.database)
        with self.assertRaises(ValueError):
            set_trade_note(999, "entry_note", "x", self.database)
        with self.assertRaises(ValueError):
            set_trade_note(trade["id"], "entry_note", "x" * 2001, self.database)


class NoteEndpointTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "journal.sqlite3"
        env = patch.dict(os.environ, {"PAPER_JOURNAL_PATH": str(self.database)})
        env.start()
        self.addCleanup(env.stop)

    def post(self, body, headers=GOOD_HEADERS):
        return asyncio.run(app.dashboard_note(FakeRequest(body, headers)))

    def test_saves_a_note_from_the_dashboard(self):
        trade = record_entry("ABC", 100, 2, 10, 97, 106, database_path=self.database)
        result = self.post({"trade_id": trade["id"], "field": "entry_note", "text": "Clean dip"})
        self.assertEqual(result["entry_note"], "Clean dip")

    def test_other_sites_and_bad_fields_are_refused(self):
        trade = record_entry("ABC", 100, 2, 10, 97, 106, database_path=self.database)
        with self.assertRaises(HTTPException) as refused:
            self.post({"trade_id": trade["id"], "field": "entry_note", "text": "x"}, headers={})
        self.assertEqual(refused.exception.status_code, 403)
        with self.assertRaises(HTTPException) as bad:
            self.post({"trade_id": trade["id"], "field": "status", "text": "x"})
        self.assertEqual(bad.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
