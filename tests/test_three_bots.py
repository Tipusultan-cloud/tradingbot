import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "backtests"))

import backtest_three_bots as three

DAYS = pd.bdate_range("2026-01-05", periods=30)


def frame(rows):
    """rows of (open, high, low, close); the last row repeats to the end."""
    rows = rows + [rows[-1]] * (len(DAYS) - len(rows))
    return pd.DataFrame(rows, index=DAYS, columns=["Open", "High", "Low", "Close"])


def run(source, rows, slots=1, earnings=None, calendar=DAYS):
    # Signal on day 0, bought at day 1's open. ATR 2 -> stop distance 3.
    lookup = {DAYS[0]: [{"ticker": "ABC", "atr": 2.0, "source": source}]}
    return three.simulate(
        lookup, calendar, {"ABC": frame(rows)}, slots, earnings, slippage=0.0
    )


FLAT = (100, 100.5, 99.5, 100)


class SplitExitTests(unittest.TestCase):
    def test_half_at_2r_then_rest_at_target(self):
        trades, _, _ = run("PULLBACK", [FLAT, FLAT, (100, 108, 99.5, 107)])
        trade = trades.iloc[0]
        self.assertEqual(trade["exit_reason"], "PARTIAL+TARGET")
        self.assertEqual(trade["partial_price"], 106)
        self.assertEqual(trade["exit_price"], 107.5)
        self.assertAlmostEqual(trade["r_multiple"], 2.25, delta=0.02)

    def test_stop_moves_to_entry_after_the_half(self):
        trades, _, _ = run(
            "PULLBACK", [FLAT, FLAT, (100, 106.5, 99.5, 106), (105, 105, 99, 100)]
        )
        trade = trades.iloc[0]
        self.assertEqual(trade["exit_reason"], "PARTIAL+TRAIL_STOP")
        self.assertEqual(trade["exit_price"], 100)

    def test_time_exit_after_20_sessions(self):
        trades, _, _ = run("PULLBACK", [FLAT])
        self.assertEqual(trades.iloc[0]["exit_reason"], "TIME_EXIT")
        self.assertEqual(trades.iloc[0]["holding_days"], 20)

    def test_gap_past_the_stop_fills_at_the_open(self):
        trades, _, _ = run("PULLBACK", [FLAT, FLAT, (90, 91, 89, 90)])
        trade = trades.iloc[0]
        self.assertEqual(trade["exit_reason"], "GAP_STOP")
        self.assertAlmostEqual(trade["r_multiple"], -10 / 3, delta=0.01)


class BreakevenTests(unittest.TestCase):
    def tearDown(self):
        three.BREAKEVEN_R = None

    def test_stop_moves_to_entry_from_the_next_session(self):
        three.BREAKEVEN_R = 1.0
        trades, _, _ = run("PULLBACK", [FLAT, FLAT, (100, 103.5, 99.5, 103), (102, 102, 99, 100)])
        self.assertEqual(trades.iloc[0]["exit_price"], 100)
        self.assertEqual(trades.iloc[0]["exit_reason"], "TRAIL_STOP")

    def test_off_by_default(self):
        trades, _, _ = run("PULLBACK", [FLAT, FLAT, (100, 103.5, 99.5, 103), (102, 102, 99, 100)])
        self.assertEqual(trades.iloc[0]["exit_reason"], "TIME_EXIT")


class TrailingExitTests(unittest.TestCase):
    def test_stop_trails_the_highest_close_with_entry_atr(self):
        trades, _, _ = run(
            "MOMENTUM",
            [FLAT, FLAT, (100, 121, 100, 120), (120, 121, 119, 119), (119, 119, 113, 115)],
        )
        trade = trades.iloc[0]
        self.assertEqual(trade["exit_reason"], "TRAIL_STOP")
        self.assertEqual(trade["exit_price"], 114)

    def test_no_time_limit_or_target(self):
        trades, _, _ = run("MOMENTUM", [FLAT, FLAT, (100, 150, 100, 149), (149, 150, 148, 149)])
        self.assertEqual(trades.iloc[0]["exit_reason"], "END_OF_TEST")


class EarningsTests(unittest.TestCase):
    def test_days_until_next_report(self):
        calendar = three.EarningsCalendar({"ABC": [date(2026, 1, 9)]})
        self.assertEqual(calendar.days_until("ABC", DAYS[0]), 4)
        self.assertIsNone(calendar.days_until("ABC", DAYS[10]))
        self.assertIsNone(calendar.days_until("XYZ", DAYS[0]))

    def test_sold_at_the_open_before_the_report(self):
        report = three.EarningsCalendar({"ABC": [DAYS[6].date()]})
        trades, _, _ = run("PULLBACK", [FLAT], earnings=report)
        self.assertEqual(trades.iloc[0]["exit_reason"], "EARNINGS_EXIT")
        self.assertEqual(trades.iloc[0]["exit_date"], DAYS[5])

    def test_no_buy_within_two_sessions_of_the_report(self):
        report = three.EarningsCalendar({"ABC": [DAYS[2].date()]})
        trades, _, _ = run("PULLBACK", [FLAT], earnings=report)
        self.assertTrue(trades.empty)

    def test_bot_3_rides_through_earnings(self):
        report = three.EarningsCalendar({"ABC": [DAYS[2].date()]})
        trades, _, _ = run("MOMENTUM", [FLAT], earnings=report)
        self.assertEqual(trades.iloc[0]["exit_reason"], "END_OF_TEST")


class MomentumSignalTests(unittest.TestCase):
    def bars(self, last_volume=5000.0, last_close=110.0, last_low=100.0):
        days = pd.bdate_range("2025-01-01", periods=230)
        table = pd.DataFrame(
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000.0},
            index=days,
        )
        table.iloc[-1] = [100.0, 111.0, last_low, last_close, last_volume]
        return table

    def test_jump_on_big_volume_qualifies(self):
        self.assertEqual(len(three.momentum_signals("ABC", self.bars())), 1)

    def test_needs_big_volume(self):
        self.assertTrue(three.momentum_signals("ABC", self.bars(last_volume=2000.0)).empty)

    def test_needs_a_close_near_the_high(self):
        self.assertTrue(three.momentum_signals("ABC", self.bars(last_close=105.5, last_low=99.0)).empty)


if __name__ == "__main__":
    unittest.main()
