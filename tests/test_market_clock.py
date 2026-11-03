import unittest
from datetime import datetime

from market_clock import CENTRAL, is_current, previous_close


def central(*args):
    return datetime(*args, tzinfo=CENTRAL)


class PreviousCloseTests(unittest.TestCase):
    def test_monday_morning_uses_friday_close(self):
        self.assertEqual(
            previous_close(central(2026, 11, 2, 8, 45)), central(2026, 10, 30, 15, 0)
        )

    def test_weekday_uses_the_day_before(self):
        self.assertEqual(
            previous_close(central(2026, 11, 4, 8, 45)), central(2026, 11, 3, 15, 0)
        )


class IsCurrentTests(unittest.TestCase):
    def test_friday_evening_scan_is_current_on_monday_morning(self):
        self.assertTrue(is_current(central(2026, 10, 30, 18, 5), central(2026, 11, 2, 8, 45)))

    def test_missed_evening_scan_is_stale_next_morning(self):
        # Monday evening scan, Tuesday evening scan missed, Wednesday buy run.
        self.assertFalse(is_current(central(2026, 11, 2, 18, 5), central(2026, 11, 4, 8, 45)))

    def test_evening_scan_stays_current_through_the_next_day(self):
        self.assertTrue(is_current(central(2026, 11, 2, 18, 5), central(2026, 11, 3, 17, 0)))


if __name__ == "__main__":
    unittest.main()
