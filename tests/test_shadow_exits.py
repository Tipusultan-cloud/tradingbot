import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from paper_journal import record_entry, record_exit
from shadow_exits import (
    entry_session_date,
    get_shadow_comparison,
    simulate,
    update_shadow_exits,
)

ENTRY_DATE = date(2026, 1, 5)  # Monday
# Entry 100, stop 97 -> risk 3/share; 2R = 106.
ENTRY, STOP, SHARES = 100.0, 97.0, 10


def bars(closes, atr=2.0, start=ENTRY_DATE):
    """Daily bars where each day opens at the previous close."""
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "Open": opens,
            "High": np.maximum(opens, closes),
            "Low": np.minimum(opens, closes),
            "Close": closes,
            "ATR": atr,
        },
        index=pd.bdate_range(start, periods=len(closes)),
    )


def run(rule, closes, **kwargs):
    return simulate(rule, ENTRY_DATE, ENTRY, STOP, SHARES, bars(closes, **kwargs))


# Rises 1/day to 130, then drops 2/day.
TREND = [100] + list(range(101, 131)) + list(range(128, 100, -2))


class Rule2R20DTests(unittest.TestCase):
    def test_sells_all_at_2r(self):
        state = run("RULE_2R_20D", [100, 102, 104, 107, 110])
        self.assertEqual(state["status"], "CLOSED")
        self.assertEqual(state["exit_reason"], "TARGET")
        self.assertEqual(state["exit_price"], 106.0)
        self.assertAlmostEqual(state["r_multiple"], 2.0)

    def test_stop_loss(self):
        state = run("RULE_2R_20D", [100, 99, 98, 96, 95])
        self.assertEqual(state["exit_reason"], "STOP")
        self.assertAlmostEqual(state["r_multiple"], -1.0)

    def test_time_exit_after_20_days(self):
        state = run("RULE_2R_20D", [100 + day * 0.1 for day in range(30)])
        self.assertEqual(state["exit_reason"], "TIME_EXIT")
        self.assertEqual(state["exit_date"], bars([0] * 20).index[-1].date().isoformat())

    def test_still_open_marks_to_last_close(self):
        state = run("RULE_2R_20D", [100, 101, 102])
        self.assertEqual(state["status"], "OPEN")
        self.assertAlmostEqual(state["pnl"], 20.0)


class HalfTrailTests(unittest.TestCase):
    def test_rides_trend_past_2r(self):
        state = run("HALF_2R_TRAIL", TREND)
        self.assertEqual(state["exit_reason"], "PARTIAL+TRAIL_STOP")
        self.assertEqual(state["partial_price"], 106.0)
        # Rest exits at highest close 130 - 3 * ATR 2 = 124.
        self.assertEqual(state["exit_price"], 124.0)
        expected = (5 * 106 + 5 * 124 - 10 * ENTRY) / (3 * SHARES)
        self.assertAlmostEqual(state["r_multiple"], expected)

    def test_breakeven_stop_after_partial(self):
        state = run("HALF_2R_TRAIL", [100, 102, 104, 107, 105, 103, 101, 99], atr=4.0)
        self.assertEqual(state["exit_reason"], "PARTIAL+TRAIL_STOP")
        self.assertEqual(state["exit_price"], ENTRY)
        self.assertAlmostEqual(state["r_multiple"], 1.0)

    def test_loser_uses_recorded_stop(self):
        state = run("HALF_2R_TRAIL", [100, 99, 98, 96])
        self.assertEqual(state["exit_reason"], "STOP")
        self.assertIsNone(state["partial_price"])
        self.assertAlmostEqual(state["r_multiple"], -1.0)

    def test_no_time_limit(self):
        state = run("HALF_2R_TRAIL", [100 + day * 0.1 for day in range(40)])
        self.assertEqual(state["status"], "OPEN")


class ReplayWindowTests(unittest.TestCase):
    def test_bars_before_entry_are_ignored(self):
        history = bars([50, 60, 200, 100, 102, 104, 107], start=date(2025, 12, 31))
        state = simulate("RULE_2R_20D", ENTRY_DATE, ENTRY, STOP, SHARES, history)
        self.assertEqual(state["exit_reason"], "TARGET")

    def test_entry_day_range_is_skipped(self):
        # The entry day dips below the stop, but the entry time is unknown.
        history = bars([100, 101])
        history.iloc[0, history.columns.get_loc("Low")] = 90.0
        state = simulate("RULE_2R_20D", ENTRY_DATE, ENTRY, STOP, SHARES, history)
        self.assertEqual(state["status"], "OPEN")

    def test_gap_down_fills_at_open(self):
        history = bars([100, 101, 102])
        history.iloc[2, history.columns.get_loc("Open")] = 95.0
        history.iloc[2, history.columns.get_loc("Low")] = 95.0
        state = simulate("RULE_2R_20D", ENTRY_DATE, ENTRY, STOP, SHARES, history)
        self.assertEqual(state["exit_reason"], "GAP_STOP")
        self.assertEqual(state["exit_price"], 95.0)
        self.assertAlmostEqual(state["r_multiple"], -5 / 3)

    def test_entry_time_uses_new_york_date(self):
        # 02:00 UTC on Jan 6 is still Jan 5 in New York.
        self.assertEqual(entry_session_date("2026-01-06T02:00:00Z"), ENTRY_DATE)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "journal.sqlite3"
        self.fetch_calls = []

    def fetch(self, ticker, entry_date):
        self.fetch_calls.append(ticker)
        return bars(TREND)

    def enter(self, ticker):
        return record_entry(
            ticker, ENTRY, 2.0, SHARES, STOP, 106.0,
            entry_time="2026-01-05T15:00:00Z", database_path=self.database,
        )

    def test_records_both_rules_and_compares_with_actual_exit(self):
        trade = self.enter("AAA")
        record_exit("AAA", 103.0, database_path=self.database)

        result = update_shadow_exits(self.database, fetch=self.fetch)
        self.assertEqual(result, {"updated": 2, "failures": []})

        comparison = get_shadow_comparison(self.database)
        row = comparison["trades"][0]
        self.assertEqual(row["trade_id"], trade["id"])
        self.assertAlmostEqual(row["actual_r"], 1.0)
        self.assertAlmostEqual(row["RULE_2R_20D_r"], 2.0)
        self.assertGreater(row["HALF_2R_TRAIL_r"], 2.0)
        totals = comparison["totals"]
        self.assertEqual(totals["ACTUAL"]["closed"], 1)
        self.assertEqual(totals["RULE_2R_20D"]["closed"], 1)
        self.assertAlmostEqual(totals["RULE_2R_20D"]["total_r"], 2.0)

    def test_closed_shadows_are_not_fetched_again(self):
        self.enter("AAA")
        update_shadow_exits(self.database, fetch=self.fetch)
        update_shadow_exits(self.database, fetch=self.fetch)
        self.assertEqual(self.fetch_calls, ["AAA"])

    def test_open_shadow_is_refreshed(self):
        self.enter("AAA")
        update_shadow_exits(self.database, fetch=lambda ticker, day: bars([100, 101]))
        update_shadow_exits(self.database, fetch=self.fetch)
        row = get_shadow_comparison(self.database)["trades"][0]
        self.assertEqual(row["HALF_2R_TRAIL_status"], "CLOSED")

    def test_price_lookup_failure_is_reported_not_raised(self):
        self.enter("AAA")

        def failing(ticker, entry_date):
            raise ConnectionError("timeout")

        result = update_shadow_exits(self.database, fetch=failing)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["failures"], ["AAA: timeout"])
        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM shadow_exits").fetchone()
        self.assertEqual(count[0], 0)


if __name__ == "__main__":
    unittest.main()
