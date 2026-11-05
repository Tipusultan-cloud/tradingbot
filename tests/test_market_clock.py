import unittest
from datetime import date, datetime
from types import SimpleNamespace

import market_clock
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




class TradingDayTests(unittest.TestCase):
    class Calendar:
        def __init__(self, open_days):
            self.open_days = open_days

        def get_calendar(self, request):
            return [
                SimpleNamespace(date=request.start)
            ] if request.start in self.open_days else []

    def test_holiday_is_not_a_trading_day(self):
        client = self.Calendar({date(2026, 11, 25)})
        self.assertTrue(market_clock.is_trading_day(date(2026, 11, 25), client))
        self.assertFalse(market_clock.is_trading_day(date(2026, 11, 26), client))

    def test_weekdays_count_when_alpaca_fails(self):
        class Broken:
            def get_calendar(self, request):
                raise ConnectionError("offline")

        self.assertTrue(market_clock.is_trading_day(date(2026, 11, 26), Broken()))
        self.assertFalse(market_clock.is_trading_day(date(2026, 11, 28), Broken()))


if __name__ == "__main__":
    unittest.main()
