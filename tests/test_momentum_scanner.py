import unittest

import numpy as np
import pandas as pd

import slot_selector
from scanner_momentum import find_momentum_setups, latest_metrics


def metric(ticker, jump=8.0, volume=4.0, close_position=0.9, price=120.0, sma200=100.0):
    return {
        "ticker": ticker, "signal_date": "2026-10-30", "price": price,
        "sma200": sma200, "atr": 3.0, "jump_pct": jump,
        "volume_ratio": volume, "close_position": close_position,
        "sector": "Information Technology", "in_sp500": True,
    }


class FindMomentumSetupsTests(unittest.TestCase):
    def test_rules_and_ranking(self):
        setups = find_momentum_setups([
            metric("BIG", volume=9.0),
            metric("OK", volume=3.5),
            metric("SMALLJUMP", jump=4.0),
            metric("LOWVOL", volume=2.0),
            metric("FADED", close_position=0.5),
            metric("DOWNTREND", price=90.0),
        ])
        self.assertEqual([s["ticker"] for s in setups], ["BIG", "OK"])
        self.assertEqual(setups[0]["momentum_rank"], 1)
        self.assertEqual(setups[0]["setup_type"], "MOMENTUM_BREAKOUT")

    def test_no_metrics(self):
        self.assertEqual(find_momentum_setups([]), [])


class LatestMetricsTests(unittest.TestCase):
    def frame(self, last_close=110.0, last_volume=5_000_000.0):
        closes = np.full(220, 100.0)
        closes[-1] = last_close
        volume = np.full(220, 1_000_000.0)
        volume[-1] = last_volume
        return pd.DataFrame(
            {"Close": closes, "High": closes + 1, "Low": closes - 1, "Volume": volume},
            index=pd.bdate_range("2025-12-01", periods=220),
        )

    def test_jump_and_volume_against_the_previous_20_days(self):
        row = latest_metrics("ABC", self.frame())
        self.assertAlmostEqual(row["jump_pct"], 10.0)
        self.assertAlmostEqual(row["volume_ratio"], 5.0)
        self.assertAlmostEqual(row["close_position"], 0.5)

    def test_short_history_is_skipped(self):
        self.assertIsNone(latest_metrics("ABC", self.frame().tail(100)))


class MomentumSlotTests(unittest.TestCase):
    def test_busy_split_by_bot(self):
        main, momentum = slot_selector.busy_by_bot(
            [{"ticker": "mpc", "strategy": "PULLBACK"}, {"ticker": "NVDA", "strategy": "MOMENTUM"}],
            [{"ticker": "HCA", "strategy": "A_PLUS"}],
        )
        self.assertEqual((main, momentum), (["MPC", "HCA"], ["NVDA"]))

    def test_momentum_fills_its_own_free_slot(self):
        payload = {"qualified": [metric("HELD"), metric("NEW"), metric("NEXT")]}

        result = slot_selector.build_momentum_selection(
            payload, held_tickers=["HELD"], busy_count=0, slots=1
        )

        picks = result["momentum_selections"]
        self.assertEqual([p["ticker"] for p in picks], ["NEW"])
        self.assertEqual(picks[0]["source"], "MOMENTUM")
        self.assertIn("stop", picks[0]["risk_preview"])

    def test_full_momentum_slot_picks_nothing(self):
        result = slot_selector.build_momentum_selection(
            {"qualified": [metric("NEW")]}, held_tickers=["X"], busy_count=1, slots=1
        )
        self.assertEqual(result["momentum_selections"], [])


if __name__ == "__main__":
    unittest.main()
