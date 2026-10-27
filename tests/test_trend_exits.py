import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backtests"))

import backtest_holdout_validation as hv
from backtest_trend_exits import CURRENT_EXIT, ExitRule, run_with_exit

ATR = 2.0  # Stop distance = 1.5 * ATR = 3.0; 2R = +6.0.


def prices(closes, ema20=None):
    """Daily bars where each day opens at the previous close."""
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    calendar = pd.bdate_range("2024-01-01", periods=len(closes))
    frame = pd.DataFrame(
        {
            "Open": opens,
            "High": np.maximum(opens, closes),
            "Low": np.minimum(opens, closes),
            "Close": closes,
            "ATR": ATR,
            "EMA20": closes - 1 if ema20 is None else ema20,
        },
        index=calendar,
    )
    return calendar, {"TEST": frame}


def signals(calendar, *days):
    return pd.DataFrame(
        [
            {"signal_date": calendar[day], "ticker": "TEST", "atr": ATR}
            for day in days
        ]
    )


def run(rule, closes, ema20=None, signal_days=(0,)):
    calendar, stock_dfs = prices(closes, ema20)
    result = run_with_exit(rule, signals(calendar, *signal_days), calendar, stock_dfs)
    return result["trades"]


ENTRY = 100.0 * (1 + hv.SLIPPAGE_PCT)
TRAIL = ExitRule("ATR_TRAIL_3X", trail_atr=3.0)


class MatchesBaseEngineTests(unittest.TestCase):
    def test_current_exit_reproduces_base_backtest(self):
        rng = np.random.default_rng(7)
        closes = 100 * np.cumprod(1 + rng.normal(0.002, 0.02, 400))
        calendar, stock_dfs = prices(closes)
        candidates = signals(calendar, *range(0, 390, 7))

        expected = hv.run_strategy(
            "CURRENT_CONTINUOUS", candidates, calendar, stock_dfs
        )
        actual = run_with_exit(CURRENT_EXIT, candidates, calendar, stock_dfs)

        self.assertGreater(len(expected["trades"]), 10)
        self.assertEqual(
            [(t["entry_date"], t["exit_reason"]) for t in expected["trades"]],
            [(t["entry_date"], t["exit_reason"]) for t in actual["trades"]],
        )
        for a, b in zip(expected["trades"], actual["trades"]):
            self.assertAlmostEqual(a["pnl"], b["pnl"], places=6)
        self.assertAlmostEqual(expected["ending_cash"], actual["ending_cash"], places=6)
        self.assertEqual(
            [row["equity"] for row in expected["equity"]],
            [row["equity"] for row in actual["equity"]],
        )


class AtrTrailTests(unittest.TestCase):
    # Rises 1/day to 130, then drops 2/day.
    CLOSES = [100] + list(range(101, 131)) + list(range(128, 100, -2))

    def test_winner_runs_past_2r(self):
        current = run(CURRENT_EXIT, self.CLOSES)[0]
        trail = run(TRAIL, self.CLOSES)[0]
        self.assertEqual(current["exit_reason"], "TARGET")
        self.assertAlmostEqual(current["r_multiple"], 2.0, places=1)
        self.assertEqual(trail["exit_reason"], "TRAIL_STOP")
        self.assertGreater(trail["r_multiple"], 6.0)

    def test_stop_trails_highest_close_by_3_atr(self):
        trade = run(TRAIL, self.CLOSES)[0]
        self.assertAlmostEqual(trade["final_stop"], 130 - 3 * ATR)
        self.assertAlmostEqual(
            trade["exit_price"], (130 - 3 * ATR) * (1 - hv.SLIPPAGE_PCT)
        )

    def test_stop_never_moves_down(self):
        # Higher ATR later would lower the trail; the stop must hold.
        calendar, stock_dfs = prices(self.CLOSES)
        frame = stock_dfs["TEST"]
        frame.loc[frame.index[25]:, "ATR"] = 10.0
        trade = run_with_exit(TRAIL, signals(calendar, 0), calendar, stock_dfs)[
            "trades"
        ][0]
        highest_before_atr_jump = self.CLOSES[24]
        self.assertAlmostEqual(trade["final_stop"], highest_before_atr_jump - 3 * ATR)

    def test_loser_still_stopped_at_initial_stop(self):
        trade = run(TRAIL, [100, 100, 99, 98, 96, 94])[0]
        self.assertEqual(trade["exit_reason"], "STOP")
        # Slightly worse than -1R because of entry and exit slippage.
        self.assertAlmostEqual(trade["r_multiple"], -1.0, delta=0.03)


class Ema20ExitTests(unittest.TestCase):
    def test_exits_next_open_after_close_below_ema20(self):
        closes = [100, 101, 102, 103, 104, 105, 106, 107]
        ema20 = [99, 100, 101, 102, 110, 104, 105, 106]  # Day 4 close < EMA20.
        trade = run(ExitRule("EMA20", ema20_exit=True), closes, ema20)[0]
        self.assertEqual(trade["exit_reason"], "EMA20_EXIT")
        self.assertEqual(trade["exit_date"], pd.bdate_range("2024-01-01", periods=8)[5])
        self.assertAlmostEqual(trade["exit_price"], 104 * (1 - hv.SLIPPAGE_PCT))

    def test_no_time_limit_while_trend_holds(self):
        closes = [100] + [100 + day * 0.2 for day in range(1, 80)]
        trade = run(ExitRule("EMA20", ema20_exit=True), closes)[0]
        self.assertEqual(trade["exit_reason"], "END_OF_TEST")
        self.assertEqual(trade["holding_days"], 79)


class HalfThenTrailTests(unittest.TestCase):
    RULE = ExitRule("HALF", partial_r=2.0, trail_atr=3.0)

    def test_half_at_2r_then_breakeven_stop(self):
        # Hits 2R (106) then falls back through entry. A wide trail keeps
        # the breakeven stop as the highest stop.
        closes = [100, 102, 104, 107, 105, 103, 101, 99]
        rule = ExitRule("HALF_WIDE", partial_r=2.0, trail_atr=6.0)
        trade = run(rule, closes)[0]
        self.assertEqual(trade["exit_reason"], "PARTIAL+TRAIL_STOP")
        self.assertAlmostEqual(trade["final_stop"], ENTRY)
        # Half sold at +2R, the rest at about breakeven -> about +1R.
        self.assertAlmostEqual(trade["r_multiple"], 1.0, delta=0.1)

    def test_rest_rides_the_trend(self):
        closes = AtrTrailTests.CLOSES
        trade = run(self.RULE, closes)[0]
        self.assertTrue(trade["exit_reason"].startswith("PARTIAL+"))
        full_trail = run(TRAIL, closes)[0]
        self.assertGreater(trade["r_multiple"], 3.0)
        self.assertLess(trade["r_multiple"], full_trail["r_multiple"])


class ExtendedHoldTests(unittest.TestCase):
    RULE = ExitRule(
        "EXT", target_r=50.0, max_hold_days=20, extended_hold_days=60
    )

    def test_holds_past_20_days_while_above_ema20(self):
        closes = [100] + [100 + day * 0.1 for day in range(1, 70)]
        trade = run(self.RULE, closes)[0]
        self.assertEqual(trade["exit_reason"], "TIME_EXIT")
        self.assertEqual(trade["holding_days"], 60)

    def test_exits_after_20_days_when_below_ema20(self):
        closes = [100] + [100 + day * 0.1 for day in range(1, 40)]
        ema20 = [c - 1 for c in closes]
        ema20[25] = closes[25] + 1
        trade = run(self.RULE, closes, ema20)[0]
        self.assertEqual(trade["exit_reason"], "TIME_EXIT")
        self.assertEqual(trade["holding_days"], 25)


if __name__ == "__main__":
    unittest.main()
