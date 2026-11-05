import unittest
from datetime import datetime, timezone

from buy_plan import build_buy_plan, plan_pick, review_list


def stock(ticker, source, sector, warnings=(), **extra):
    return {
        "ticker": ticker, "source": source, "sector": sector,
        "warnings": [{"label": label} for label in warnings],
        "risk_preview": {"shares": 10, "entry_reference": 100, "stop": 95, "target": 110},
        **extra,
    }


def slots(selections=(), a_plus=(), pullback=(), momentum=(), free=2, momentum_free=1):
    return {
        "generated_at": "2026-11-05T00:05:00+00:00",
        "free_slots": free, "momentum_free": momentum_free,
        "selections": list(selections),
        "bots": {"A_PLUS": list(a_plus), "PULLBACK": list(pullback), "MOMENTUM": list(momentum)},
    }


NOW = datetime(2026, 11, 5, 0, 10, tzinfo=timezone.utc)


class BuyPlanTests(unittest.TestCase):
    def plan(self, data, open_trades=(), auto_bots=("A_PLUS",), **kwargs):
        return build_buy_plan(data, list(open_trades), list(auto_bots), now=NOW, **kwargs)

    def test_bot1_pick_is_bought_automatically(self):
        kmb = stock("KMB", "A_PLUS", "Consumer Staples")
        subject, body = self.plan(slots([kmb], [kmb]))
        self.assertEqual(subject, "SultanQuant buy plan: Bot 1 buys KMB · 0 plan picks to buy by hand")
        self.assertIn("scan of Wed Nov 4, 6:05 PM Central", body)
        self.assertIn("will be bought unless the price opens", body)

    def test_skip_reason_is_shown(self):
        kmb = stock("KMB", "A_PLUS", "Consumer Staples")
        subject, body = self.plan(
            slots([kmb], [kmb]), auto_check=lambda s: "earnings in 0 trading day(s)",
        )
        self.assertIn("no automatic buy (KMB skipped)", subject)
        self.assertIn("-> WILL BE SKIPPED: earnings in 0 trading day(s)", body)

    def test_paused_buying_skips_everything(self):
        kmb = stock("KMB", "A_PLUS", "Consumer Staples")
        _, body = self.plan(slots([kmb], [kmb]), pause="6% loss in 30 days")
        self.assertIn("Buying is paused: 6% loss in 30 days", body)
        self.assertIn("WILL BE SKIPPED: 6% loss in 30 days", body)

    def test_one_plan_pick_per_bot(self):
        data = slots(pullback=[
            stock("OKTA", "PULLBACK", "Information Technology"),
            stock("PANW", "PULLBACK", "Industrials", ["Earnings in 9d"]),
            stock("VLO", "PULLBACK", "Energy"),
            stock("DG", "PULLBACK", "Consumer Staples"),
        ], free=1)
        subject, body = self.plan(
            data, held_sectors={"Information Technology": ["FTNT"]},
        )
        self.assertIn("1 plan pick to buy by hand", subject)
        self.assertIn("PLAN PICK 👑 VLO (Energy)", body)
        self.assertIn("OKTA (Information Technology already held (FTNT))", body)
        self.assertIn("PANW (Earnings in 9d)", body)
        self.assertIn("DG (the plan pick is VLO)", body)

    def test_no_plan_pick_when_every_stock_is_flagged(self):
        data = slots(pullback=[stock("PANW", "PULLBACK", "Industrials", ["Bad news"])], free=1)
        _, body = self.plan(data)
        self.assertIn("No plan pick: every stock has a warning or its sector is held.", body)

    def test_full_slot_lists_no_picks(self):
        data = slots(momentum=[stock("XYZ", "MOMENTUM", "Financials")], momentum_free=0)
        subject, body = self.plan(data)
        self.assertIn("Bot 3 · Momentum Rider · 1 listed · 0 free slots", body)
        self.assertIn("No free slot: a trade has to close first.", body)
        self.assertIn("0 plan picks", subject)

    def test_bot1_buy_uses_a_shared_slot(self):
        kmb = stock("KMB", "A_PLUS", "Consumer Staples")
        data = slots([kmb], [kmb], pullback=[stock("VLO", "PULLBACK", "Energy")], free=1)
        _, body = self.plan(data)
        self.assertIn("Bot 2 · Dip Buyer · 1 listed · 0 free slots", body)

    def test_open_trades_and_held_stocks(self):
        trade = {"ticker": "REGN", "strategy": "MOMENTUM", "shares": 2,
                 "entry_price": 950, "stop_price": 907.69, "active_stop_price": 920}
        data = slots(momentum=[stock("REGN", "MOMENTUM", "Health Care")])
        _, body = self.plan(data, [trade])
        self.assertIn("REGN · Bot 3 · Momentum Rider · 2 shares at $950.00 · stop $920.00", body)
        self.assertIn("Bot 3 · Momentum Rider · 0 listed", body)

    def test_missing_scan(self):
        subject, body = self.plan(None)
        self.assertEqual(subject, "SultanQuant buy plan: scan results missing")


class ReviewListTests(unittest.TestCase):
    def test_reasons_in_rank_order(self):
        rows = [
            stock("FTNT", "PULLBACK", "Information Technology"),
            stock("OKTA", "PULLBACK", "Information Technology"),
            stock("MPC", "PULLBACK", "Energy", ["Fell after earnings"]),
            stock("VLO", "PULLBACK", "Energy"),
            stock("DG", "PULLBACK", "Consumer Staples"),
        ]
        reviewed = review_list(rows, {"FTNT"}, {"Information Technology": ["FTNT"]})
        self.assertEqual(
            [(s["ticker"], r) for s, r in reviewed],
            [
                ("FTNT", ["already held"]),
                ("OKTA", ["Information Technology already held (FTNT)"]),
                ("MPC", ["Fell after earnings"]),
                ("VLO", []),
                ("DG", ["the plan pick is VLO"]),
            ],
        )
        self.assertEqual(plan_pick(rows, {"FTNT"}, {"Information Technology": ["FTNT"]})["ticker"], "VLO")

    def test_no_pick(self):
        self.assertIsNone(plan_pick([stock("A", "PULLBACK", "Energy", ["Bad news"])]))


if __name__ == "__main__":
    unittest.main()
