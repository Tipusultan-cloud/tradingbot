import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import master_selector
from earnings_calendar import (
    STATUS_SOON,
    STATUS_CLEAR,
    STATUS_UNKNOWN,
    check_earnings,
    trading_days_until,
)

# Wednesday
AS_OF = date(2026, 10, 21)


def dates(*values):
    return lambda ticker: list(values)


class TradingDaysTests(unittest.TestCase):
    def test_same_day_is_zero(self):
        self.assertEqual(trading_days_until(AS_OF, AS_OF), 0)

    def test_weekend_is_skipped(self):
        # Wednesday -> next Monday is Thu, Fri, Mon = 3 trading days.
        self.assertEqual(trading_days_until(AS_OF, date(2026, 10, 26)), 3)


class CheckEarningsTests(unittest.TestCase):
    def test_blocks_earnings_inside_window(self):
        result = check_earnings(
            "XOM", as_of=AS_OF, fetch=dates(date(2026, 10, 27))
        )
        self.assertEqual(result["status"], STATUS_SOON)
        self.assertEqual(result["next_earnings_date"], "2026-10-27")
        self.assertEqual(result["trading_days_until"], 4)

    def test_blocks_on_last_day_of_window(self):
        result = check_earnings(
            "XOM", as_of=AS_OF, fetch=dates(date(2026, 10, 28))
        )
        self.assertEqual(result["trading_days_until"], 5)
        self.assertEqual(result["status"], STATUS_SOON)

    def test_blocks_earnings_today(self):
        result = check_earnings("XOM", as_of=AS_OF, fetch=dates(AS_OF))
        self.assertEqual(result["status"], STATUS_SOON)

    def test_clear_outside_window(self):
        result = check_earnings(
            "AAPL", as_of=AS_OF, fetch=dates(date(2026, 11, 2))
        )
        self.assertEqual(result["status"], STATUS_CLEAR)
        self.assertEqual(result["trading_days_until"], 8)

    def test_ignores_past_dates_and_uses_next_one(self):
        result = check_earnings(
            "AAPL",
            as_of=AS_OF,
            fetch=dates(
                date(2026, 7, 30), date(2027, 1, 28), date(2026, 11, 2)
            ),
        )
        self.assertEqual(result["next_earnings_date"], "2026-11-02")

    def test_no_upcoming_date_is_unknown(self):
        result = check_earnings(
            "AAPL", as_of=AS_OF, fetch=dates(date(2026, 7, 30))
        )
        self.assertEqual(result["status"], STATUS_UNKNOWN)

    def test_lookup_failure_is_unknown_not_blocked(self):
        def failing_fetch(ticker):
            raise ConnectionError("network down")

        result = check_earnings("AAPL", as_of=AS_OF, fetch=failing_fetch)
        self.assertEqual(result["status"], STATUS_UNKNOWN)
        self.assertIn("network down", result["detail"])


def a_plus_setup():
    return {
        "ticker": "TEST",
        "sector": "Energy",
        "sector_etf": "XLE",
        "setup_type": "EMA20_RECLAIM",
        "setup_valid": True,
        "setup_score": 98,
        "price": 100.0,
        "atr": 2.0,
        "rsi": 58.0,
        "volume_ratio": 1.5,
        "market_bullish": True,
        "sector_bullish": True,
        "stock_vs_sector": 1.0,
        "sector_vs_spy": 1.0,
        "stock_vs_spy": 2.0,
    }


CLEAR_NEWS = {"status": "CLEAR", "detail": "No bad news."}
CLEAR_EARNINGS = {"status": STATUS_CLEAR, "detail": "Next earnings far away."}


class MasterSelectorEarningsTests(unittest.TestCase):
    def run_selector(self, earnings_result=CLEAR_EARNINGS, news_result=CLEAR_NEWS):
        now = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            base_path = folder / "base.json"
            topdown_path = folder / "topdown.json"
            master_path = folder / "master.json"
            best_path = folder / "best.json"
            base_path.write_text(json.dumps({
                "generated_at": now, "qualified": [a_plus_setup()],
            }))
            topdown_path.write_text(json.dumps({
                "generated_at": now,
                "market": {"bullish": True},
                "qualified": [a_plus_setup()],
            }))
            with (
                patch.object(master_selector, "QUALIFIED_SETUPS_JSON", base_path),
                patch.object(master_selector, "QUALIFIED_TOPDOWN_JSON", topdown_path),
                patch.object(master_selector, "MASTER_CANDIDATES_JSON", master_path),
                patch.object(master_selector, "BEST_CURRENT_CANDIDATE_JSON", best_path),
                patch.object(
                    master_selector, "check_earnings", return_value=earnings_result
                ),
                patch.object(
                    master_selector, "check_news", return_value=news_result
                ),
                patch("builtins.print"),
            ):
                master_selector.main()
            return json.loads(master_path.read_text())

    def test_earnings_soon_warns_but_does_not_reject(self):
        master = self.run_selector({
            "status": STATUS_SOON,
            "detail": "Earnings on 2026-10-23 in 2 trading day(s).",
        })
        self.assertEqual(master["rejected"], [])
        self.assertEqual(master["best_current"]["ticker"], "TEST")
        self.assertEqual(
            master["best_current"]["earnings"]["status"], STATUS_SOON
        )

    def test_unknown_earnings_still_approves_with_warning(self):
        master = self.run_selector({
            "status": STATUS_UNKNOWN,
            "detail": "Earnings lookup failed: timeout",
        })
        self.assertEqual(master["best_current"]["ticker"], "TEST")
        self.assertEqual(
            master["best_current"]["earnings"]["status"], STATUS_UNKNOWN
        )


if __name__ == "__main__":
    unittest.main()
