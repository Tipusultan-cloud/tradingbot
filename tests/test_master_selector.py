import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import master_selector
from master_selector import build_risk_preview

CLEAR_NEWS = {"status": "CLEAR", "detail": "No bad news."}
CLEAR_EARNINGS = {"status": "CLEAR", "detail": "Next earnings far away."}


def setup(ticker="TEST", **overrides):
    # Passes every strict rule unless overridden.
    stock = {
        "ticker": ticker,
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
    stock.update(overrides)
    return stock


def breakout(ticker="TEST", **overrides):
    values = {
        "setup_type": "MOMENTUM_BREAKOUT",
        "rsi": 60.0,
        "volume_ratio": 1.8,
    }
    values.update(overrides)
    return setup(ticker, **values)


class SelectorRun:
    """Runs master_selector.main() against temporary scanner files."""

    def __init__(
        self,
        base,
        topdown=None,
        market_bullish=True,
        base_generated_at="now",
        topdown_generated_at="now",
    ):
        now = datetime.now(timezone.utc).isoformat()
        self.base = {
            "generated_at": now if base_generated_at == "now" else base_generated_at,
            "qualified": base,
        }
        self.topdown = {
            "generated_at": (
                now if topdown_generated_at == "now" else topdown_generated_at
            ),
            "market": {"bullish": market_bullish},
            "qualified": base if topdown is None else topdown,
        }
        self.earnings_calls = []
        self.news_calls = []

    def run(self):
        def earnings(ticker):
            self.earnings_calls.append(ticker)
            return CLEAR_EARNINGS

        def news(ticker):
            self.news_calls.append(ticker)
            return CLEAR_NEWS

        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            base_path = folder / "base.json"
            topdown_path = folder / "topdown.json"
            master_path = folder / "master.json"
            best_path = folder / "best.json"
            base_path.write_text(json.dumps(self.base))
            topdown_path.write_text(json.dumps(self.topdown))
            with (
                patch.object(master_selector, "QUALIFIED_SETUPS_JSON", base_path),
                patch.object(master_selector, "QUALIFIED_TOPDOWN_JSON", topdown_path),
                patch.object(master_selector, "MASTER_CANDIDATES_JSON", master_path),
                patch.object(master_selector, "BEST_CURRENT_CANDIDATE_JSON", best_path),
                patch.object(master_selector, "check_earnings", side_effect=earnings),
                patch.object(master_selector, "check_news", side_effect=news),
                patch("builtins.print"),
            ):
                master_selector.main()
            self.master = json.loads(master_path.read_text())
            self.best = json.loads(best_path.read_text())
        return self

    @property
    def approved(self):
        return [stock["ticker"] for stock in self.master["approved"]]

    def reasons(self, ticker="TEST"):
        for stock in self.master["rejected"]:
            if stock["ticker"] == ticker:
                return stock["reasons"]
        raise AssertionError(f"{ticker} was not rejected")


def select(*stocks):
    return SelectorRun(list(stocks)).run()


class ApprovalTests(unittest.TestCase):
    def test_a_plus_reclaim_is_selected(self):
        run = select(setup())
        self.assertEqual(run.approved, ["TEST"])
        self.assertEqual(run.master["rejected"], [])
        self.assertEqual(run.best["status"], "A+ CURRENT CANDIDATE")
        self.assertEqual(run.best["selected"]["ticker"], "TEST")

    def test_a_plus_breakout_is_selected(self):
        self.assertEqual(select(breakout()).approved, ["TEST"])

    def test_no_candidates_means_wait(self):
        run = select()
        self.assertIsNone(run.master["best_current"])
        self.assertEqual(run.best["status"], "WAIT")
        self.assertEqual(
            run.best["reason"],
            "No stock currently passes all strict A+ master rules.",
        )


class ScannerAgreementTests(unittest.TestCase):
    def test_stock_in_only_one_scanner_is_ignored(self):
        run = SelectorRun(
            base=[setup("BASEONLY"), setup("BOTH")],
            topdown=[setup("TOPONLY"), setup("BOTH")],
        ).run()
        self.assertEqual(run.approved, ["BOTH"])
        self.assertEqual(run.master["rejected"], [])

    def test_scanners_disagreeing_on_setup_type_is_rejected(self):
        run = SelectorRun(base=[setup()], topdown=[breakout()]).run()
        self.assertIn("Scanners disagree on setup type", run.reasons())

    def test_unknown_setup_type_is_rejected(self):
        run = select(setup(setup_type="GAP_UP"))
        self.assertIn("Unknown or missing setup type", run.reasons())

    def test_invalid_setup_in_either_scanner_is_rejected(self):
        run = SelectorRun(
            base=[setup(setup_valid=False)], topdown=[setup()]
        ).run()
        self.assertEqual(run.reasons(), ["Base scanner setup is not valid"])

        run = SelectorRun(
            base=[setup()], topdown=[setup(setup_valid=False)]
        ).run()
        self.assertEqual(run.reasons(), ["Top-down scanner setup is not valid"])

    def test_lower_of_the_two_scores_is_used(self):
        run = SelectorRun(
            base=[setup(setup_score=99)], topdown=[setup(setup_score=94)]
        ).run()
        self.assertIn("Setup score below 95", run.reasons())


class MarketAndSectorTests(unittest.TestCase):
    def test_spy_not_bullish_means_wait_without_evaluating(self):
        run = SelectorRun([setup()], market_bullish=False).run()
        self.assertEqual(run.approved, [])
        self.assertEqual(run.master["rejected"], [])
        self.assertEqual(run.best["status"], "WAIT")
        self.assertEqual(run.best["reason"], "SPY market regime is not bullish.")

    def test_stock_level_market_flag_is_required(self):
        run = select(setup(market_bullish=False))
        self.assertEqual(run.reasons(), ["Market is not bullish"])

    def test_sector_not_bullish_is_rejected(self):
        run = select(setup(sector_bullish=False))
        self.assertEqual(run.reasons(), ["Sector is not bullish"])


class ThresholdTests(unittest.TestCase):
    """Each rule is checked just inside and just outside its limit."""

    def assertApproved(self, stock):
        run = select(stock)
        self.assertEqual(run.approved, ["TEST"], run.master["rejected"])

    def assertRejected(self, stock, reason):
        self.assertEqual(select(stock).reasons(), [reason])

    def test_setup_score(self):
        self.assertApproved(setup(setup_score=95))
        self.assertRejected(setup(setup_score=94), "Setup score below 95")

    def test_reclaim_volume(self):
        self.assertApproved(setup(volume_ratio=1.20))
        self.assertRejected(
            setup(volume_ratio=1.19), "Reclaim volume below 1.2x average"
        )

    def test_reclaim_rsi(self):
        self.assertApproved(setup(rsi=50.0))
        self.assertApproved(setup(rsi=65.0))
        self.assertRejected(setup(rsi=49.9), "Reclaim RSI not in 50-65")
        self.assertRejected(setup(rsi=65.1), "Reclaim RSI not in 50-65")

    def test_breakout_volume(self):
        self.assertApproved(breakout(volume_ratio=1.50))
        self.assertRejected(
            breakout(volume_ratio=1.49), "Breakout volume below 1.5x average"
        )

    def test_breakout_rsi(self):
        self.assertApproved(breakout(rsi=55.0))
        self.assertApproved(breakout(rsi=65.0))
        self.assertRejected(breakout(rsi=54.9), "Breakout RSI not in 55-65")
        self.assertRejected(breakout(rsi=65.1), "Breakout RSI not in 55-65")

    def test_stock_must_beat_sector(self):
        self.assertApproved(setup(stock_vs_sector=0.01))
        self.assertRejected(
            setup(stock_vs_sector=0.0), "Stock is not outperforming sector"
        )

    def test_sector_must_beat_spy(self):
        self.assertApproved(setup(sector_vs_spy=0.01))
        self.assertRejected(
            setup(sector_vs_spy=0.0), "Sector is not outperforming SPY"
        )

    def test_stock_must_beat_spy(self):
        self.assertApproved(setup(stock_vs_spy=0.01))
        self.assertRejected(
            setup(stock_vs_spy=0.0), "Stock is not outperforming SPY"
        )

    def test_every_failed_rule_is_reported(self):
        run = select(setup(setup_score=90, rsi=70.0, sector_bullish=False))
        self.assertEqual(
            run.reasons(),
            [
                "Setup score below 95",
                "Sector is not bullish",
                "Reclaim RSI not in 50-65",
            ],
        )


class LookupTests(unittest.TestCase):
    def test_news_and_earnings_only_checked_for_technical_passes(self):
        run = select(setup("GOOD"), setup("WEAK", rsi=70.0))
        self.assertEqual(run.news_calls, ["GOOD"])
        self.assertEqual(run.earnings_calls, ["GOOD"])
        weak = run.master["rejected"][0]
        self.assertIsNone(weak["news"])
        self.assertIsNone(weak["earnings"])


class RankingTests(unittest.TestCase):
    def test_higher_score_ranks_first(self):
        run = select(setup("LOW", setup_score=96), setup("HIGH", setup_score=99))
        self.assertEqual(run.approved, ["HIGH", "LOW"])
        self.assertEqual(run.best["selected"]["ticker"], "HIGH")
        self.assertEqual(
            [stock["current_rank"] for stock in run.master["approved"]], [1, 2]
        )

    def test_reclaim_beats_breakout_on_equal_score(self):
        run = select(breakout("BRK"), setup("RCL"))
        self.assertEqual(run.approved, ["RCL", "BRK"])

    def test_tie_broken_by_sector_strength(self):
        run = select(
            setup("WEAKSEC", sector_vs_spy=0.5),
            setup("STRONGSEC", sector_vs_spy=3.0),
        )
        self.assertEqual(run.approved, ["STRONGSEC", "WEAKSEC"])

    def test_approved_candidates_get_risk_preview(self):
        run = select(setup())
        self.assertEqual(
            run.master["approved"][0]["risk_preview"],
            build_risk_preview(price=100.0, atr=2.0),
        )


class FreshnessTests(unittest.TestCase):
    def test_stale_scanner_output_is_refused(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
        with self.assertRaisesRegex(RuntimeError, "qualified_setups.json is stale"):
            SelectorRun([setup()], base_generated_at=old).run()
        with self.assertRaisesRegex(
            RuntimeError, "qualified_setups_topdown.json is stale"
        ):
            SelectorRun([setup()], topdown_generated_at=old).run()

    def test_output_just_inside_age_limit_is_accepted(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=71)).isoformat()
        run = SelectorRun([setup()], base_generated_at=recent).run()
        self.assertEqual(run.approved, ["TEST"])

    def test_missing_timestamp_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "no generated_at timestamp"):
            SelectorRun([setup()], base_generated_at=None).run()


class RiskPreviewTests(unittest.TestCase):
    def test_one_percent_risk_with_1_5_atr_stop_and_2r_target(self):
        risk = build_risk_preview(price=100.0, atr=2.0)
        # Stop distance 3.00; $100 max risk -> 33 shares.
        self.assertEqual(risk["stop"], 97.0)
        self.assertEqual(risk["target"], 106.0)
        self.assertEqual(risk["shares"], 33)
        self.assertEqual(risk["max_risk"], 100.0)
        self.assertEqual(risk["planned_risk"], 99.0)
        self.assertEqual(risk["position_value"], 3300.0)
        self.assertEqual(risk["reward_risk"], 2.0)

    def test_shares_capped_by_account_cash(self):
        # Risk alone would allow 133 shares ($66,500), more than the account.
        risk = build_risk_preview(price=500.0, atr=0.5)
        self.assertEqual(risk["shares"], 20)
        self.assertLessEqual(risk["position_value"], 10000.0)

    def test_invalid_inputs_return_none(self):
        self.assertIsNone(build_risk_preview(price=0.0, atr=2.0))
        self.assertIsNone(build_risk_preview(price=100.0, atr=0.0))
        # Stop would fall below zero.
        self.assertIsNone(build_risk_preview(price=1.0, atr=1.0))


if __name__ == "__main__":
    unittest.main()
