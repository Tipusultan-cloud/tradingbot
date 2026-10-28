import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import app
import slot_selector
from paper_journal import count_open_trades, get_paper_trades, record_entry
from scan_notifications import build_slot_message
from scanner_pullback import find_pullback_setups, latest_metrics
from slot_selector import build_slot_selection, load_fresh


def metric(ticker, return_6m, return_5d=-6.0, uptrend=True):
    # Defaults describe a qualifying pullback, apart from leadership rank.
    return {
        "ticker": ticker,
        "company": f"{ticker} Inc",
        "sector": "Energy",
        "signal_date": "2026-10-27",
        "price": 110.0 if uptrend else 90.0,
        "ema20": 105.0,
        "ema50": 100.0,
        "sma200": 95.0,
        "atr": 2.0,
        "return_6m": return_6m,
        "return_5d": return_5d,
    }


def field_of(count):
    # Ten stocks with 6-month returns 0..90%: S7-S9 are top-20% leaders.
    return [metric(f"S{i}", return_6m=i * 10.0, return_5d=1.0) for i in range(count)]


class PullbackScanTests(unittest.TestCase):
    def test_leader_with_sharp_drop_qualifies(self):
        stocks = field_of(10)
        stocks[9]["return_5d"] = -6.0

        setups = find_pullback_setups(stocks)

        self.assertEqual([s["ticker"] for s in setups], ["S9"])
        self.assertEqual(setups[0]["setup_type"], "LEADER_PULLBACK")
        self.assertEqual(setups[0]["pullback_rank"], 1)

    def test_non_leader_drop_is_ignored(self):
        stocks = field_of(10)
        stocks[3]["return_5d"] = -9.0

        self.assertEqual(find_pullback_setups(stocks), [])

    def test_drop_of_five_percent_or_less_is_ignored(self):
        stocks = field_of(10)
        stocks[9]["return_5d"] = -5.0

        self.assertEqual(find_pullback_setups(stocks), [])

    def test_leader_must_be_in_uptrend(self):
        stocks = field_of(10)
        stocks[9].update(metric("S9", 90.0, uptrend=False))
        stocks[8].update(ema20=99.0, return_5d=-7.0)

        self.assertEqual(find_pullback_setups(stocks), [])

    def test_strongest_leader_ranks_first(self):
        stocks = field_of(10)
        stocks[8]["return_5d"] = -7.0
        stocks[9]["return_5d"] = -6.0

        setups = find_pullback_setups(stocks)

        self.assertEqual([s["ticker"] for s in setups], ["S9", "S8"])

    def test_latest_metrics_needs_enough_history(self):
        frame = price_frame(np.linspace(50, 100, 150))
        self.assertIsNone(latest_metrics("ABC", frame))

    def test_latest_metrics_measures_returns(self):
        closes = np.linspace(50, 100, 260)
        frame = price_frame(closes)

        row = latest_metrics("ABC", frame)

        self.assertAlmostEqual(
            row["return_5d"], (closes[-1] / closes[-6] - 1) * 100
        )
        self.assertAlmostEqual(
            row["return_6m"], (closes[-1] / closes[-127] - 1) * 100
        )
        self.assertAlmostEqual(row["atr"], 1.0)


def price_frame(closes):
    index = pd.bdate_range("2025-01-01", periods=len(closes))
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes + 0.5,
            "Low": closes - 0.5,
            "Close": closes,
            "Volume": 1_000_000,
        },
        index=index,
    )


A_PLUS = {"ticker": "APL", "price": 100.0, "atr": 2.0, "setup_score": 98}


def pullbacks(*tickers):
    return {
        "qualified": [
            {"ticker": t, "price": 50.0, "atr": 1.0, "return_6m": 40.0}
            for t in tickers
        ]
    }


class SlotSelectionTests(unittest.TestCase):
    def test_a_plus_fills_first_slot_then_pullback(self):
        result = build_slot_selection(
            {"best_current": A_PLUS}, pullbacks("PB1", "PB2"), [], 2
        )

        self.assertEqual(
            [(s["slot"], s["ticker"], s["source"]) for s in result["selections"]],
            [(1, "APL", "A_PLUS"), (2, "PB1", "PULLBACK")],
        )
        self.assertEqual(result["free_slots"], 2)
        self.assertEqual(result["selections"][1]["risk_preview"]["stop"], 48.5)

    def test_pullbacks_fill_slots_when_no_a_plus(self):
        result = build_slot_selection(
            {"best_current": None}, pullbacks("PB1", "PB2", "PB3"), [], 2
        )
        self.assertEqual(
            [s["ticker"] for s in result["selections"]], ["PB1", "PB2"]
        )

    def test_open_trades_use_slots_and_are_not_bought_again(self):
        result = build_slot_selection(
            {"best_current": A_PLUS}, pullbacks("PB1", "PB2"), ["apl"], 2
        )

        self.assertEqual(result["free_slots"], 1)
        self.assertEqual(
            [(s["slot"], s["ticker"]) for s in result["selections"]],
            [(2, "PB1")],
        )

    def test_full_slots_select_nothing(self):
        result = build_slot_selection(
            {"best_current": A_PLUS}, pullbacks("PB1"), ["X", "Y"], 2
        )
        self.assertEqual(result["free_slots"], 0)
        self.assertEqual(result["selections"], [])

    def test_stock_in_both_lists_takes_one_slot(self):
        result = build_slot_selection(
            {"best_current": A_PLUS}, pullbacks("APL", "PB1"), [], 2
        )
        self.assertEqual(
            [(s["ticker"], s["source"]) for s in result["selections"]],
            [("APL", "A_PLUS"), ("PB1", "PULLBACK")],
        )

    def test_missing_sources_select_nothing(self):
        result = build_slot_selection(None, None, [], 2)
        self.assertEqual(result["selections"], [])

    def test_max_open_trades_setting(self):
        with patch.dict("os.environ", {"MAX_OPEN_TRADES": "3"}):
            self.assertEqual(slot_selector.max_open_trades(), 3)
        with patch.dict("os.environ", {"MAX_OPEN_TRADES": "0"}):
            with self.assertRaisesRegex(RuntimeError, "at least 1"):
                slot_selector.max_open_trades()
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(slot_selector.max_open_trades(), 2)


class LoadFreshTests(unittest.TestCase):
    def test_stale_and_missing_files_are_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            stale = Path(folder) / "stale.json"
            stale.write_text(json.dumps({
                "generated_at": "2000-01-01T00:00:00+00:00",
            }))
            warnings = []

            self.assertIsNone(load_fresh(stale, "Pullback scan", warnings))
            self.assertIsNone(
                load_fresh(Path(folder) / "missing.json", "Master", warnings)
            )

        self.assertIn("stale", warnings[0])
        self.assertIn("do not exist", warnings[1])

    def test_fresh_file_is_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fresh.json"
            path.write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "qualified": [],
            }))
            warnings = []

            payload = load_fresh(path, "Pullback scan", warnings)

        self.assertEqual(payload["qualified"], [])
        self.assertEqual(warnings, [])


class SlotSelectorMainTests(unittest.TestCase):
    def test_writes_selection_with_pullback_checks(self):
        now = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            master = folder / "master.json"
            pullback = folder / "pullback.json"
            output = folder / "slots.json"
            master.write_text(json.dumps({"generated_at": now, "best_current": None}))
            pullback.write_text(json.dumps({"generated_at": now, **pullbacks("PB1")}))
            with (
                patch.object(slot_selector, "MASTER_CANDIDATES_JSON", master),
                patch.object(slot_selector, "PULLBACK_SETUPS_JSON", pullback),
                patch.object(slot_selector, "SLOT_SELECTION_JSON", output),
                patch.object(slot_selector, "get_paper_trades", return_value={"open": []}),
                patch.object(slot_selector, "check_earnings", return_value={"status": "CLEAR"}),
                patch.object(slot_selector, "check_news", return_value={"status": "UNKNOWN"}),
                patch.object(slot_selector, "load_dotenv"),
                patch.dict("os.environ", {"MAX_OPEN_TRADES": "2"}),
                patch("builtins.print"),
            ):
                slot_selector.main()
            payload = json.loads(output.read_text())

        self.assertEqual(payload["status"], "SLOTS TO FILL")
        self.assertEqual(payload["selections"][0]["ticker"], "PB1")
        self.assertEqual(payload["selections"][0]["earnings"]["status"], "CLEAR")
        self.assertEqual(payload["selections"][0]["news"]["status"], "UNKNOWN")


class JournalStrategyTests(unittest.TestCase):
    def test_strategy_is_stored_and_open_trades_counted(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "paper.sqlite3"
            record_entry(
                "abc", 100, 2, 10, 97, 106,
                database_path=database, strategy="PULLBACK",
            )

            trades = get_paper_trades(database_path=database)
            self.assertEqual(trades["open"][0]["strategy"], "PULLBACK")
            self.assertEqual(count_open_trades(database), 1)
            csv_text = database.with_suffix(".csv").read_text(encoding="utf-8-sig")
            self.assertIn("strategy", csv_text.splitlines()[0])
            self.assertIn("PULLBACK", csv_text)

    def test_old_journal_gains_strategy_column(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "old.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL, entry_time TEXT NOT NULL,
                    entry_price REAL NOT NULL, atr REAL NOT NULL,
                    shares INTEGER NOT NULL, stop_price REAL NOT NULL,
                    target_price REAL NOT NULL, setup_score REAL,
                    status TEXT NOT NULL, exit_time TEXT, exit_price REAL,
                    realized_pnl REAL, exit_reason TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO paper_trades (ticker, entry_time, entry_price, atr,"
                " shares, stop_price, target_price, status)"
                " VALUES ('OLD', '2026-01-01T00:00:00Z', 10, 1, 1, 9, 12, 'OPEN')"
            )
            connection.commit()
            connection.close()

            trades = get_paper_trades(database_path=database)

        self.assertEqual(trades["open"][0]["ticker"], "OLD")
        self.assertIsNone(trades["open"][0]["strategy"])


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class WebhookSlotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        now = datetime.now(timezone.utc).isoformat()
        self.scanner = root / "scanner.json"
        self.master = root / "master.json"
        self.slots = root / "slots.json"
        self.database = root / "paper.sqlite3"
        self.scanner.write_text(json.dumps({"generated_at": now, "qualified": []}))
        self.master.write_text(json.dumps({"generated_at": now, "best_current": None}))
        self.write_slots(now, [
            {"ticker": "PB1", "source": "PULLBACK", "sector": "Energy"},
            {"ticker": "APL", "source": "A_PLUS"},
        ])
        self.patches = [
            patch.object(app, "QUALIFIED_FILE", self.scanner),
            patch.object(app, "MASTER_CANDIDATES_JSON", self.master),
            patch.object(app, "SLOT_SELECTION_JSON", self.slots),
            patch.object(app, "MAX_OPEN_TRADES", 2),
            patch.dict("os.environ", {"PAPER_JOURNAL_PATH": str(self.database)}),
            patch("builtins.print"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    def write_slots(self, generated_at, selections):
        self.slots.write_text(json.dumps({
            "generated_at": generated_at,
            "selections": selections,
        }))

    def buy(self, ticker):
        return asyncio.run(app.webhook(FakeRequest({
            "secret": app.WEBHOOK_SECRET,
            "ticker": ticker,
            "action": "BUY",
            "price": 50,
            "atr": 1,
        })))

    def test_pullback_slot_pick_is_confirmed_and_journaled(self):
        result = self.buy("PB1")

        self.assertEqual(result["status"], "FINAL_CONFIRMED")
        self.assertEqual(result["strategy"], "PULLBACK")
        self.assertEqual(result["stop_price"], 48.5)
        trade = get_paper_trades(database_path=self.database)["open"][0]
        self.assertEqual((trade["ticker"], trade["strategy"]), ("PB1", "PULLBACK"))

    def test_a_plus_slot_entry_still_needs_scanner_and_master(self):
        result = self.buy("APL")

        self.assertEqual(result["status"], "not_confirmed")
        self.assertEqual(result["scanner"], "FAIL")

    def test_unknown_ticker_is_rejected(self):
        result = self.buy("ZZZ")

        self.assertEqual(result["status"], "not_confirmed")
        self.assertIn("not a current pullback", result["pullback_slot"])

    def test_stale_slot_selection_is_rejected(self):
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        self.write_slots(old, [{"ticker": "PB1", "source": "PULLBACK"}])

        result = self.buy("PB1")

        self.assertEqual(result["status"], "not_confirmed")
        self.assertIn("stale", result["pullback_slot"])

    def test_buy_is_rejected_when_all_slots_are_full(self):
        for ticker in ("X", "Y"):
            record_entry(ticker, 100, 2, 1, 97, 106, database_path=self.database)

        result = self.buy("PB1")

        self.assertEqual(result["status"], "not_confirmed")
        self.assertIn("All 2 paper slots are full", result["reason"])
        self.assertEqual(count_open_trades(self.database), 2)


class SlotEmailTests(unittest.TestCase):
    def test_slot_lines_in_email(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "slots.json"
            path.write_text(json.dumps({
                "status": "SLOTS TO FILL",
                "max_slots": 2,
                "open_positions": ["XOM"],
                "selections": [{
                    "slot": 2,
                    "ticker": "PB1",
                    "source": "PULLBACK",
                    "risk_preview": {"entry_reference": 50, "stop": 48.5, "target": 53},
                    "earnings": {"status": "CLEAR"},
                }],
            }))

            message = build_slot_message(path)

        self.assertIn("SLOTS TO FILL (1 of 2 open)", message)
        self.assertIn("Slot 2: PB1 (leader pullback)", message)
        self.assertIn("stop $48.5", message)
        self.assertIn("earnings CLEAR", message)

    def test_missing_slot_file_adds_nothing(self):
        self.assertEqual(build_slot_message(Path("does-not-exist.json")), "")


class DashboardSlotTests(unittest.TestCase):
    def dashboard(self, slots_generated_at):
        now = datetime.now(timezone.utc).isoformat()
        payloads = {
            "universe": {"generated_at": now, "stocks": []},
            "technical": {"generated_at": now, "qualified": []},
            "topdown": {"generated_at": now, "qualified": [], "market": {}},
            "master": {"generated_at": now, "best_current": None, "approved": []},
            "best": {"generated_at": now, "status": "WAIT"},
            "pullback": {"generated_at": now, "qualified_count": 3, "qualified": []},
            "slots": {
                "generated_at": slots_generated_at,
                "status": "SLOTS TO FILL",
                "max_slots": 2,
                "free_slots": 2,
                "open_positions": [],
                "selections": [{"ticker": "PB1", "source": "PULLBACK"}],
            },
        }
        names = {
            "universe": "TOP_200_JSON",
            "technical": "QUALIFIED_SETUPS_JSON",
            "topdown": "QUALIFIED_TOPDOWN_JSON",
            "master": "MASTER_CANDIDATES_JSON",
            "best": "BEST_CURRENT_CANDIDATE_JSON",
            "pullback": "PULLBACK_SETUPS_JSON",
            "slots": "SLOT_SELECTION_JSON",
        }
        empty_journal = {
            "open": [], "recent": [],
            "summary": {"realized_pnl": 0},
        }
        with tempfile.TemporaryDirectory() as folder:
            patches = []
            for key, payload in payloads.items():
                path = Path(folder) / f"{key}.json"
                path.write_text(json.dumps(payload))
                patches.append(patch.object(app, names[key], path))
            patches += [
                patch.object(app, "get_paper_trades", return_value=empty_journal),
                patch.object(
                    app, "get_shadow_comparison",
                    return_value={"trades": [], "totals": {}},
                ),
            ]
            for item in patches:
                item.start()
            try:
                return app.get_dashboard_data()
            finally:
                for item in reversed(patches):
                    item.stop()

    def test_current_slots_are_shown(self):
        data = self.dashboard(datetime.now(timezone.utc).isoformat())

        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["slots"]["selections"][0]["ticker"], "PB1")
        self.assertEqual(data["counts"]["pullback_setups"], 3)
        self.assertEqual(data["scans"]["slots"]["status"], "current")

    def test_stale_slots_are_hidden(self):
        data = self.dashboard("2000-01-01T00:00:00+00:00")

        self.assertEqual(data["slots"]["status"], "STALE DATA")
        self.assertEqual(data["slots"]["selections"], [])
        self.assertEqual(data["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
