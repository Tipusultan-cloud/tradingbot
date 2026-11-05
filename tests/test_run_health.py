import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_health
from market_clock import CENTRAL


def central(text):
    return datetime.fromisoformat(text).replace(tzinfo=CENTRAL)


def order(id, symbol, qty, side="sell", order_type="stop", status="held", legs=None):
    return SimpleNamespace(
        id=id, symbol=symbol, qty=str(qty), side=side,
        order_type=order_type, status=status, legs=legs,
    )


class FakeClient:
    def __init__(self, positions, orders):
        self.positions = positions
        self.orders = orders

    def get_orders(self, filter=None):
        return [o for o in self.orders if o.symbol in filter.symbols]

    def get_all_positions(self):
        return [SimpleNamespace(symbol=s, qty=str(q)) for s, q in self.positions]


class RunHealthCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "run_status.json"
        # Watchdog installed on Monday 2026-11-02 at 7 AM Central.
        self.since = central("2026-11-02T07:00:00")
        run_health.record_finish("setup", "ok", now=self.since, path=self.path)

    def tearDown(self):
        self.folder.cleanup()

    def problems(self, now):
        return run_health.run_problems(run_health.load_status(self.path), now)

    def keys(self, now):
        return [key.split(":", 2)[2] for key, _ in self.problems(now)]


class LastDueTests(unittest.TestCase):
    def test_waits_for_the_grace_time(self):
        due = run_health.last_due("morning_buy", central("2026-11-04T09:00:00"))
        self.assertEqual(due, central("2026-11-03T08:45:00"))
        due = run_health.last_due("morning_buy", central("2026-11-04T09:30:00"))
        self.assertEqual(due, central("2026-11-04T08:45:00"))

    def test_skips_the_weekend(self):
        due = run_health.last_due("daily_scan", central("2026-11-09T09:30:00"))
        self.assertEqual(due, central("2026-11-06T17:30:00"))


class RunProblemTests(RunHealthCase):
    def test_runs_before_tracking_are_not_reported(self):
        # At 8:00 Monday the last due runs are Friday's (before 7:00 Monday).
        self.assertEqual(self.keys(central("2026-11-02T08:00:00")), [])

    def test_missed_run(self):
        self.assertEqual(self.keys(central("2026-11-02T09:30:00")), ["missed"])

    def test_finished_run_is_fine(self):
        run_health.record_start("morning_buy", now=central("2026-11-02T08:45:00"), path=self.path)
        run_health.record_finish("morning_buy", "ok", now=central("2026-11-02T08:47:00"), path=self.path)
        self.assertEqual(self.keys(central("2026-11-02T09:30:00")), [])

    def test_late_run_still_counts(self):
        run_health.record_start("morning_buy", now=central("2026-11-02T09:10:00"), path=self.path)
        run_health.record_finish("morning_buy", "ok", now=central("2026-11-02T09:11:00"), path=self.path)
        self.assertEqual(self.keys(central("2026-11-02T09:30:00")), [])

    def test_early_manual_run_does_not_count(self):
        run_health.record_start("morning_buy", now=central("2026-11-02T07:30:00"), path=self.path)
        run_health.record_finish("morning_buy", "ok", now=central("2026-11-02T07:31:00"), path=self.path)
        self.assertEqual(self.keys(central("2026-11-02T09:30:00")), ["missed"])

    def test_started_but_never_finished(self):
        # 2026-11-03: the task started at 9:10 and its window was closed.
        run_health.record_start("morning_buy", now=central("2026-11-02T09:10:00"), path=self.path)
        self.assertEqual(self.keys(central("2026-11-02T09:30:00")), ["unfinished"])

    def test_failed_run(self):
        run_health.record_start("daily_scan", now=central("2026-11-02T17:30:00"), path=self.path)
        run_health.record_finish("daily_scan", "failed", "scanner.py failed",
                                 now=central("2026-11-02T18:05:00"), path=self.path)
        problems = self.problems(central("2026-11-02T20:00:00"))
        self.assertEqual([k.split(":")[0] for k, _ in problems], ["morning_buy", "daily_scan"])
        self.assertIn("scanner.py failed", problems[1][1])


class CheckTests(RunHealthCase):
    def test_each_problem_is_emailed_once(self):
        now = central("2026-11-02T09:30:00")
        with patch.object(run_health, "_send", return_value=True) as send:
            run_health.check(now=now, path=self.path, include_stops=False)
            run_health.check(now=now, path=self.path, include_stops=False)
        self.assertEqual(send.call_count, 1)

    def test_retries_when_the_email_failed(self):
        now = central("2026-11-02T09:30:00")
        with patch.object(run_health, "_send", return_value=False) as send:
            run_health.check(now=now, path=self.path, include_stops=False)
            run_health.check(now=now, path=self.path, include_stops=False)
        self.assertEqual(send.call_count, 2)


class StopCoverageTests(unittest.TestCase):
    def test_bracket_oco_and_trailing_stops_cover_the_position(self):
        client = FakeClient(
            [("FTNT", 6), ("MPC", 3), ("REGN", 2), ("SPY", 5)],
            [
                # Old bracket: filled buy with a held stop leg (FTNT).
                order("buy-1", "FTNT", 6, side="buy", order_type="market", status="filled",
                      legs=[order("tp-1", "FTNT", 6, order_type="limit", status="new"),
                            order("sl-1", "FTNT", 6)]),
                order("oco-1", "MPC", 1, order_type="limit", status="new",
                      legs=[order("sl-2", "MPC", 1)]),
                order("oco-2", "MPC", 2, order_type="limit", status="new",
                      legs=[order("sl-3", "MPC", 2)]),
                # A raised trailing stop: the old order was replaced.
                order("trail-old", "REGN", 2, status="replaced"),
                order("trail", "REGN", 2, status="new"),
            ],
        )
        self.assertEqual(run_health.unprotected_positions(client), [])

    def test_position_without_stop_is_reported(self):
        client = FakeClient(
            [("REGN", 2), ("FTNT", 4)],
            [
                order("sl-1", "FTNT", 2),
                order("sl-0", "FTNT", 2, status="filled"),
                order("buy", "FTNT", 4, side="buy", order_type="limit"),
                order("sl-r", "REGN", 2, status="canceled"),
            ],
        )
        self.assertEqual(
            run_health.unprotected_positions(client),
            [("REGN", 2.0, 0), ("FTNT", 4.0, 2.0)],
        )


if __name__ == "__main__":
    unittest.main()
