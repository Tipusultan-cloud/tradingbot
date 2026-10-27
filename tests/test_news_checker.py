import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from news_checker import (
    STATUS_BAD_NEWS,
    STATUS_CLEAR,
    STATUS_UNKNOWN,
    check_news,
    classify_headline,
    lookback_start,
)
import tests.test_earnings_calendar as earnings_tests

CLEAR_EARNINGS = earnings_tests.CLEAR_EARNINGS

NEW_YORK = ZoneInfo("America/New_York")
# Wednesday 8:30 AM, when the scheduled scan runs.
AS_OF = datetime(2026, 10, 21, 8, 30, tzinfo=NEW_YORK)


def article(headline, symbols=("TEST",)):
    return {
        "headline": headline,
        "created_at": AS_OF,
        "symbols": list(symbols),
        "source": "benzinga",
        "url": "https://example.com/news",
    }


def articles(*items):
    return lambda ticker, start, end: list(items)


class ClassifyHeadlineTests(unittest.TestCase):
    def test_negative_headlines(self):
        cases = {
            "Morgan Stanley Downgrades Test Corp To Underweight": "downgrade",
            "Test Corp Q3 EPS $1.20 Misses $1.31 Estimate": "earnings_miss",
            "Test Corp Lowers FY26 Guidance": "guidance_cut",
            "SEC Opens Probe Into Test Corp Accounting": "legal_regulatory",
            "Test Corp Announces $500M Secondary Offering": "offering_dilution",
            "Test Corp CEO Steps Down Effective Immediately": "business_event",
            "Test Corp Shares Plunge After Report": "price_drop",
        }
        for headline, category in cases.items():
            with self.subTest(headline=headline):
                self.assertEqual(classify_headline(headline), category)

    def test_positive_and_neutral_headlines(self):
        for headline in (
            "Goldman Sachs Upgrades Test Corp To Buy",
            "Test Corp Q3 EPS $1.40 Beats $1.31 Estimate",
            "Test Corp Raises FY26 Guidance",
            "Test Corp Announces New Product Line",
            "",
            None,
        ):
            with self.subTest(headline=headline):
                self.assertIsNone(classify_headline(headline))

    def test_law_firm_solicitations_are_ignored(self):
        for headline in (
            "SHAREHOLDER ALERT: Pomerantz Law Firm Investigates Claims "
            "On Behalf of Investors of Test Corp",
            "Rosen Law Firm Reminds Investors of Class Action Deadline",
        ):
            with self.subTest(headline=headline):
                self.assertIsNone(classify_headline(headline))


class LookbackTests(unittest.TestCase):
    def test_three_trading_days_back_from_wednesday(self):
        self.assertEqual(
            lookback_start(AS_OF), datetime(2026, 10, 16, tzinfo=NEW_YORK)
        )

    def test_monday_lookback_skips_weekend(self):
        monday = datetime(2026, 10, 19, 8, 30, tzinfo=NEW_YORK)
        self.assertEqual(
            lookback_start(monday), datetime(2026, 10, 14, tzinfo=NEW_YORK)
        )


class CheckNewsTests(unittest.TestCase):
    def test_bad_headline_blocks(self):
        result = check_news("TEST", as_of=AS_OF, fetch=articles(
            article("Test Corp Announces New Product Line"),
            article("Barclays Downgrades Test Corp To Underweight"),
        ))
        self.assertEqual(result["status"], STATUS_BAD_NEWS)
        self.assertEqual(result["bad_headlines"][0]["category"], "downgrade")
        self.assertIn("Downgrades", result["detail"])

    def test_clean_news_is_clear(self):
        result = check_news("TEST", as_of=AS_OF, fetch=articles(
            article("Test Corp Raises FY26 Guidance"),
        ))
        self.assertEqual(result["status"], STATUS_CLEAR)
        self.assertEqual(result["articles_checked"], 1)

    def test_no_news_is_clear(self):
        result = check_news("TEST", as_of=AS_OF, fetch=articles())
        self.assertEqual(result["status"], STATUS_CLEAR)

    def test_market_roundups_are_ignored(self):
        result = check_news("TEST", as_of=AS_OF, fetch=articles(
            article(
                "Stocks Tumble As Yields Rise",
                symbols=("TEST", "SPY", "AAPL", "MSFT"),
            ),
        ))
        self.assertEqual(result["status"], STATUS_CLEAR)
        self.assertEqual(result["articles_checked"], 0)

    def test_lookup_failure_is_unknown_not_blocked(self):
        def failing_fetch(ticker, start, end):
            raise RuntimeError("Alpaca API keys are not configured")

        result = check_news("TEST", as_of=AS_OF, fetch=failing_fetch)
        self.assertEqual(result["status"], STATUS_UNKNOWN)
        self.assertIn("not configured", result["detail"])


class MasterSelectorNewsTests(unittest.TestCase):
    run_selector = earnings_tests.MasterSelectorEarningsTests.run_selector

    def test_bad_news_rejects_a_plus_stock(self):
        detail = "Bad news (downgrade): Barclays Downgrades Test Corp"
        master = self.run_selector(
            CLEAR_EARNINGS, {"status": STATUS_BAD_NEWS, "detail": detail}
        )
        self.assertEqual(master["approved"], [])
        self.assertIsNone(master["best_current"])
        self.assertIn(detail, master["rejected"][0]["reasons"])

    def test_unknown_news_still_approves_with_warning(self):
        master = self.run_selector(
            CLEAR_EARNINGS,
            {"status": STATUS_UNKNOWN, "detail": "News lookup failed"},
        )
        self.assertEqual(master["best_current"]["ticker"], "TEST")
        self.assertEqual(master["best_current"]["news"]["status"], STATUS_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
