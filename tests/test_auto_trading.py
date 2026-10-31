import asyncio
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException

import app
import auto_trader
import broker
import trade_manager
from paper_exits import close_paper_trades
from paper_journal import get_paper_trades, get_pending_broker_orders, record_entry
from trade_manager import TradeError, open_trade, plan_order, sync

SETTINGS = {
    "account": 10000, "risk_pct": 1, "atr_multiplier": 1.5,
    "reward_risk": 2, "second_reward_risk": 2.5,
}
STOCK = {"ticker": "ABC", "atr": 2.0, "setup_score": 98}
# A buy at 100 with ATR 2: stop 97, 33 shares; 16 sell at 106 (2R),
# the other 17 at 107.5 (2.5R).


def leg(leg_id, kind, status="new", price=None, filled_at=None):
    return SimpleNamespace(
        id=leg_id, type=kind, status=status,
        filled_avg_price=price, filled_at=filled_at,
    )


class FakeAlpaca:
    """Just enough of alpaca TradingClient for the trade manager."""

    def __init__(self, market_open=True):
        self.market_open = market_open
        self.orders = {}
        self.submitted = []
        self.canceled = []
        self.closed = []
        self.replaced = []
        self.reject_oco = False

    def get_clock(self):
        return SimpleNamespace(is_open=self.market_open)

    def submit_order(self, request):
        order_class = getattr(getattr(request, "order_class", None), "value", None)
        if order_class == "oco" and self.reject_oco:
            raise ConnectionError("Alpaca unavailable")
        self.submitted.append(request)
        number = len(self.submitted)
        if order_class == "bracket":
            legs = [leg(f"tp-{number}", "limit"), leg(f"sl-{number}", "stop")]
        elif order_class == "oco":
            legs = [leg(f"sl-{number}", "stop")]
        else:
            legs = []
        order = SimpleNamespace(
            id=f"order-{number}", status="accepted",
            filled_avg_price=None, filled_qty=0, filled_at=None, legs=legs,
        )
        self.orders[order.id] = order
        return order

    def stop_out(self, order_id, price, when="2026-10-30T15:00:00Z"):
        order = self.orders[order_id]
        order.status = "canceled"
        order.legs[-1] = leg(order.legs[-1].id, "stop", "filled", price, when)

    def replace_order_by_id(self, order_id, request):
        self.replaced.append((order_id, request.stop_price))

    def fill(self, order_id, price, qty, when="2026-10-29T13:35:00Z"):
        order = self.orders[order_id]
        order.status, order.filled_avg_price = "filled", price
        order.filled_qty, order.filled_at = qty, when

    def get_order_by_id(self, order_id, filter=None):
        return self.orders[order_id]

    def get_calendar(self, request):
        return list(pd.bdate_range(request.start, request.end))

    def cancel_order_by_id(self, order_id):
        self.canceled.append(order_id)

    def close_position(self, ticker):
        self.closed.append(ticker)
        order = SimpleNamespace(
            id=f"close-{ticker}", status="filled", filled_avg_price=104.0,
            filled_qty=33, filled_at="2026-11-30T14:31:00Z", legs=[],
        )
        self.orders[order.id] = order
        return order


class JournalCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Path(self.folder.name) / "paper.sqlite3"
        self.env = patch.dict(os.environ, {
            "PAPER_ACCOUNT_BALANCE": "10000", "RISK_PER_TRADE_PCT": "1",
            "ATR_MULTIPLIER": "1.5", "REWARD_RISK_RATIO": "2",
            "MAX_OPEN_TRADES": "2",
        })
        self.env.start()
        self.client = FakeAlpaca()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def buy(self, stock=STOCK, strategy="PULLBACK", source="AUTO", price=100.0):
        return open_trade(
            self.client, stock, strategy, source, lambda ticker: price,
            database_path=self.database,
        )

    def sync(self, today=date(2026, 10, 30)):
        return sync(
            self.client, lambda ticker: 104.0,
            database_path=self.database, today=today, delay=0,
        )


class PlanOrderTests(unittest.TestCase):
    def test_one_percent_risk_with_2r_and_2_5r_targets(self):
        plan = plan_order(100.0, 2.0, SETTINGS)
        # Stop distance 3 -> $100 risk buys 33 shares.
        self.assertEqual(
            plan, {"shares": 33, "stop": 97.0, "target": 106.0, "target2": 107.5}
        )

    def test_rejects_position_below_one_share(self):
        with self.assertRaisesRegex(TradeError, "below 1 share"):
            plan_order(5000.0, 200.0, SETTINGS)


class OpenTradeTests(JournalCase):
    def test_sends_market_buy_and_remembers_it(self):
        order = self.buy()

        request = self.client.submitted[0].to_request_fields()
        self.assertEqual(request["symbol"], "ABC")
        self.assertEqual(request["qty"], 33)
        self.assertEqual(request["side"].value, "buy")
        self.assertEqual(request["type"].value, "market")
        self.assertNotIn("order_class", request)
        self.assertEqual(order["order_id"], "order-1")
        self.assertEqual((order["target"], order["target2"]), (106.0, 107.5))
        pending = get_pending_broker_orders(self.database)
        self.assertEqual(
            (pending[0]["ticker"], pending[0]["strategy"], pending[0]["entry_source"]),
            ("ABC", "PULLBACK", "AUTO"),
        )
        self.assertEqual(
            (pending[0]["stop_price"], pending[0]["target_price"],
             pending[0]["target2_price"]),
            (97.0, 106.0, 107.5),
        )

    def test_market_closed_sends_nothing(self):
        self.client.market_open = False
        with self.assertRaisesRegex(TradeError, "market is closed"):
            self.buy()
        self.assertEqual(self.client.submitted, [])

    def test_waiting_order_blocks_same_stock(self):
        self.buy()
        with self.assertRaisesRegex(TradeError, "already held"):
            self.buy()

    def test_waiting_orders_and_open_trades_fill_the_slots(self):
        self.buy()
        record_entry("XYZ", 50, 1, 10, 48, 54, database_path=self.database)
        with self.assertRaisesRegex(TradeError, "All 2 slots are full"):
            self.buy({"ticker": "DEF", "atr": 1.0})


class SyncTests(JournalCase):
    def test_filled_entry_becomes_open_trade(self):
        self.buy(source="MANUAL")
        self.client.fill("order-1", 100.4, 33)

        result = self.sync()

        trade = result["filled"][0]
        self.assertEqual(trade["entry_price"], 100.4)
        self.assertEqual(trade["shares"], 33)
        self.assertEqual(
            (trade["stop_price"], trade["target_price"], trade["target2_price"]),
            (97.0, 106.0, 107.5),
        )
        self.assertEqual(trade["entry_source"], "MANUAL")
        self.assertEqual(trade["broker_order_id"], "order-1")
        self.assertEqual(get_pending_broker_orders(self.database), [])

    def test_fill_places_two_exit_orders(self):
        self.buy()
        self.client.fill("order-1", 100.4, 33)

        self.sync()

        half, rest = (r.to_request_fields() for r in self.client.submitted[1:])
        for request in (half, rest):
            self.assertEqual(request["side"].value, "sell")
            self.assertEqual(request["order_class"].value, "oco")
            self.assertEqual(request["time_in_force"].value, "gtc")
            self.assertEqual(request["stop_loss"], {"stop_price": 97.0})
        self.assertEqual((half["qty"], half["take_profit"]), (16, {"limit_price": 106.0}))
        self.assertEqual((rest["qty"], rest["take_profit"]), (17, {"limit_price": 107.5}))
        trade = get_paper_trades(database_path=self.database)["open"][0]
        self.assertEqual(
            (trade["half_order_id"], trade["rest_order_id"]), ("order-2", "order-3")
        )

    def test_failed_exit_orders_are_retried(self):
        self.buy()
        self.client.fill("order-1", 100.0, 33)
        self.client.reject_oco = True

        result = self.sync()

        self.assertIn("exit orders not placed yet", result["errors"][0])
        self.client.reject_oco = False
        self.sync()
        self.assertEqual(len(self.client.submitted), 3)
        self.sync()
        self.assertEqual(len(self.client.submitted), 3)

    def test_canceled_entry_frees_the_slot(self):
        self.buy()
        self.client.orders["order-1"].status = "canceled"

        result = self.sync()

        self.assertEqual(result["canceled"], ["ABC (canceled)"])
        self.assertEqual(get_pending_broker_orders(self.database), [])

    def test_unfilled_entry_stays_pending(self):
        self.buy()
        self.assertEqual(self.sync()["filled"], [])
        self.assertEqual(len(get_pending_broker_orders(self.database)), 1)

    def filled_trade(self):
        self.buy()
        self.client.fill("order-1", 100.0, 33)
        self.sync()

    def test_stop_on_both_parts_closes_trade(self):
        self.filled_trade()
        self.client.stop_out("order-2", 96.9)
        self.client.stop_out("order-3", 96.9)

        closed = self.sync()["closed"][0]

        self.assertEqual(closed["exit_reason"], "STOP")
        self.assertAlmostEqual(closed["exit_price"], 96.9)
        self.assertAlmostEqual(closed["realized_pnl"], -102.3)

    def test_waits_for_the_half_stop_before_closing(self):
        self.filled_trade()
        self.client.stop_out("order-3", 96.9)

        self.assertEqual(self.sync()["closed"], [])

    def test_2r_half_is_recorded_and_stop_moves_to_entry(self):
        self.filled_trade()
        self.client.fill("order-2", 106.0, 16, "2026-10-30T15:00:00Z")

        result = self.sync()

        self.assertEqual(result["closed"], [])
        self.assertEqual(result["halves"][0]["partial_shares"], 16)
        self.assertEqual(self.client.replaced, [("sl-3", 100.0)])
        trade = get_paper_trades(database_path=self.database)["open"][0]
        self.assertEqual(
            (trade["partial_exit_price"], trade["active_stop_price"]), (106.0, 100.0)
        )
        self.assertEqual((trade["shares_open"], trade["risk_at_stop"]), (17, 0))
        self.sync()
        self.assertEqual(len(self.client.replaced), 1)

    def test_rest_at_2_5r_closes_trade(self):
        self.filled_trade()
        self.client.fill("order-2", 106.0, 16)
        self.sync()
        self.client.fill("order-3", 107.5, 17)

        closed = self.sync()["closed"][0]

        self.assertEqual(closed["exit_reason"], "PARTIAL+TARGET")
        self.assertEqual(closed["exit_price"], 107.5)
        self.assertAlmostEqual(closed["realized_pnl"], 16 * 6 + 17 * 7.5)

    def test_rest_stopped_at_entry_after_half(self):
        self.filled_trade()
        self.client.fill("order-2", 106.0, 16)
        self.sync()
        self.client.stop_out("order-3", 100.0)

        closed = self.sync()["closed"][0]

        self.assertEqual(closed["exit_reason"], "PARTIAL+STOP")
        self.assertAlmostEqual(closed["realized_pnl"], 96.0)

    def test_both_targets_between_syncs(self):
        self.filled_trade()
        self.client.fill("order-2", 106.0, 16)
        self.client.fill("order-3", 107.5, 17)

        result = self.sync()

        self.assertEqual(len(result["halves"]), 1)
        self.assertAlmostEqual(result["closed"][0]["realized_pnl"], 223.5)

    def test_one_share_trade_sells_at_2_5r(self):
        self.buy({"ticker": "BIG", "atr": 40.0}, price=100.0)
        self.client.fill("order-1", 100.0, 1)
        self.sync()

        request = self.client.submitted[1].to_request_fields()
        self.assertEqual(len(self.client.submitted), 2)
        self.assertEqual((request["qty"], request["take_profit"]), (1, {"limit_price": 250.0}))

    def test_old_bracket_trade_still_closes_on_its_legs(self):
        record_entry(
            "OLD", 100.0, 2.0, 33, 97.0, 106.0, database_path=self.database,
            entry_time="2026-10-29T13:35:00Z", broker_order_id="bracket-1",
        )
        self.client.orders["bracket-1"] = SimpleNamespace(
            id="bracket-1", status="filled", legs=[
                leg("tp-old", "limit", "filled", 106.0, "2026-10-30T15:00:00Z"),
                leg("sl-old", "stop", "canceled"),
            ],
        )

        closed = self.sync()["closed"][0]

        self.assertEqual(closed["exit_reason"], "TARGET")
        self.assertAlmostEqual(closed["realized_pnl"], 198.0)

    def test_open_trade_inside_20_sessions_is_left_alone(self):
        self.filled_trade()
        result = self.sync(today=date(2026, 11, 20))
        self.assertEqual(result["closed"], [])
        self.assertEqual(self.client.closed, [])

    def test_20_sessions_sells_at_market(self):
        self.filled_trade()

        closed = self.sync(today=date(2026, 11, 30))["closed"][0]

        self.assertEqual(self.client.canceled, ["order-2", "order-3"])
        self.assertEqual(self.client.closed, ["ABC"])
        self.assertEqual(closed["exit_reason"], "TIME_EXIT")
        self.assertEqual(closed["exit_price"], 104.0)

    def test_20_sessions_after_half_sells_the_rest(self):
        self.filled_trade()
        self.client.fill("order-2", 106.0, 16)
        self.sync()

        closed = self.sync(today=date(2026, 11, 30))["closed"][0]

        self.assertEqual(self.client.canceled, ["order-3"])
        self.assertEqual(closed["exit_reason"], "PARTIAL+TIME_EXIT")
        self.assertAlmostEqual(closed["realized_pnl"], 16 * 6 + 17 * 4)

    def test_daily_paper_check_skips_alpaca_trades(self):
        self.filled_trade()

        result = close_paper_trades(
            database_path=self.database,
            fetch=lambda ticker, entry_date: self.fail("should not fetch"),
        )

        self.assertEqual(
            result, {"closed": [], "halves": [], "still_open": [], "failures": []}
        )


class SellTests(JournalCase):
    def filled_trade(self):
        self.buy()
        self.client.fill("order-1", 100.0, 33)
        self.sync()

    def sell(self, ticker="ABC"):
        return trade_manager.sell_trade(
            self.client, ticker, lambda ticker: 104.0,
            database_path=self.database, delay=0,
        )

    def test_sell_cancels_stop_and_target_and_sells_at_market(self):
        self.filled_trade()

        closed = self.sell()

        self.assertEqual(self.client.canceled, ["order-2", "order-3"])
        self.assertEqual(self.client.closed, ["ABC"])
        self.assertEqual(closed["exit_reason"], "MANUAL_SELL")
        self.assertAlmostEqual(closed["realized_pnl"], 132.0)
        self.assertEqual(get_paper_trades(database_path=self.database)["open"], [])

    def test_market_closed_sells_nothing(self):
        self.filled_trade()
        self.client.market_open = False
        with self.assertRaisesRegex(TradeError, "market is closed"):
            self.sell()
        self.assertEqual(self.client.closed, [])

    def test_stop_already_filled_is_recorded_not_sold_again(self):
        self.filled_trade()
        self.client.stop_out("order-2", 96.9)
        self.client.stop_out("order-3", 96.9)

        with self.assertRaisesRegex(TradeError, "already closed at its stop"):
            self.sell()

        self.assertEqual(self.client.closed, [])
        closed = get_paper_trades(database_path=self.database)["recent"][0]
        self.assertEqual(closed["exit_reason"], "STOP")

    def test_no_open_trade(self):
        with self.assertRaisesRegex(TradeError, "no open paper trade"):
            self.sell("XYZ")

    def test_journal_only_trade_is_refused(self):
        record_entry("JRN", 50, 1, 10, 48, 54, database_path=self.database)
        with self.assertRaisesRegex(TradeError, "journal-only"):
            self.sell("JRN")


class StatusTests(unittest.TestCase):
    def status(self, **env):
        values = {"AUTO_TRADING": "", "ALPACA_PAPER": "", "ALPACA_API_KEY": "",
                  "ALPACA_API_SECRET": "", **env}
        with patch.dict(os.environ, values), patch.object(broker, "load_dotenv"):
            return broker.auto_trading_status()

    def test_off_unless_switched_on(self):
        self.assertFalse(self.status(ALPACA_API_KEY="k", ALPACA_API_SECRET="s")[0])

    def test_live_account_is_refused(self):
        enabled, reason = self.status(
            AUTO_TRADING="true", ALPACA_PAPER="false",
            ALPACA_API_KEY="k", ALPACA_API_SECRET="s",
        )
        self.assertFalse(enabled)
        self.assertIn("paper", reason)

    def test_needs_keys(self):
        enabled, reason = self.status(AUTO_TRADING="true", ALPACA_PAPER="true")
        self.assertFalse(enabled)
        self.assertIn("keys", reason)

    def test_on_with_paper_keys(self):
        enabled, _ = self.status(
            AUTO_TRADING="true", ALPACA_PAPER="true",
            ALPACA_API_KEY="k", ALPACA_API_SECRET="s",
        )
        self.assertTrue(enabled)


class FakeRequest:
    def __init__(self, payload, headers):
        self.payload = payload
        self.headers = headers

    async def json(self):
        return self.payload


GOOD_HEADERS = {"origin": "http://127.0.0.1:8001", "x-tradingbot": "dashboard"}


class DashboardBuyTests(JournalCase):
    def setUp(self):
        super().setUp()
        self.slots = Path(self.folder.name) / "slots.json"
        self.slots.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "selections": [],
            "bots": {
                "A_PLUS": [{"ticker": "HCA", "atr": 10.0, "setup_score": 100}],
                "PULLBACK": [{"ticker": "VLO", "atr": 12.0}],
            },
        }))
        self.patches = [
            patch.object(app, "SLOT_SELECTION_JSON", self.slots),
            patch.object(app, "auto_trading_status", return_value=(True, "on")),
            patch.object(app, "get_trading_client", return_value=self.client),
            patch.object(app, "latest_price", lambda ticker: 400.0),
            patch.dict(os.environ, {"PAPER_JOURNAL_PATH": str(self.database)}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        super().tearDown()

    def post(self, ticker, headers=GOOD_HEADERS):
        return asyncio.run(app.dashboard_buy(FakeRequest({"ticker": ticker}, headers)))

    def test_hand_pick_from_bot_2_sends_order(self):
        result = self.post("vlo")

        self.assertEqual(result["status"], "order_sent")
        self.assertEqual((result["ticker"], result["strategy"]), ("VLO", "PULLBACK"))
        self.assertEqual(result["entry_source"], "MANUAL")
        self.assertEqual(len(self.client.submitted), 1)

    def test_hand_pick_from_bot_1_is_tagged_a_plus(self):
        self.assertEqual(self.post("HCA")["strategy"], "A_PLUS")

    def test_other_websites_are_refused(self):
        for headers in (
            {"origin": "https://evil.example", "x-tradingbot": "dashboard"},
            {"origin": "http://127.0.0.1:8001"},
        ):
            with self.assertRaises(HTTPException) as caught:
                self.post("VLO", headers)
            self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.client.submitted, [])

    def test_stock_outside_the_lists_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self.post("TSLA")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("not in the current Bot 1 or Bot 2 list", caught.exception.detail)

    def test_refused_when_auto_trading_is_off(self):
        with patch.object(app, "auto_trading_status", return_value=(False, "off")):
            with self.assertRaises(HTTPException) as caught:
                self.post("VLO")
        self.assertEqual(caught.exception.detail, "off")

    def test_market_closed_message_is_returned(self):
        self.client.market_open = False
        with self.assertRaises(HTTPException) as caught:
            self.post("VLO")
        self.assertIn("market is closed", caught.exception.detail)


class DashboardSellTests(JournalCase):
    def setUp(self):
        super().setUp()
        self.patches = [
            patch.object(app, "auto_trading_status", return_value=(True, "on")),
            patch.object(app, "get_trading_client", return_value=self.client),
            patch.object(app, "latest_price", lambda ticker: 104.0),
            patch.dict(os.environ, {"PAPER_JOURNAL_PATH": str(self.database)}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        super().tearDown()

    def post(self, ticker, headers=GOOD_HEADERS):
        return asyncio.run(app.dashboard_sell(FakeRequest({"ticker": ticker}, headers)))

    def test_sell_button_sells_open_trade(self):
        self.buy()
        self.client.fill("order-1", 100.0, 33)
        self.sync()

        result = self.post("abc")

        self.assertEqual(result["status"], "sold")
        self.assertEqual((result["ticker"], result["shares"]), ("ABC", 33))
        self.assertAlmostEqual(result["realized_pnl"], 132.0)

    def test_other_websites_are_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self.post("ABC", {"origin": "https://evil.example", "x-tradingbot": "dashboard"})
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.client.closed, [])

    def test_unknown_ticker_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self.post("XYZ")
        self.assertEqual(caught.exception.status_code, 409)


class WebhookWhileAutoTradingTests(unittest.TestCase):
    def test_tradingview_alerts_are_ignored(self):
        request = FakeRequest(
            {"secret": app.WEBHOOK_SECRET, "ticker": "ABC", "action": "BUY",
             "price": 50, "atr": 1},
            {},
        )
        with patch.object(app, "auto_trading_status", return_value=(True, "on")):
            result = asyncio.run(app.webhook(request))
        self.assertEqual(result["status"], "ignored")


class AutoTraderTests(JournalCase):
    def test_skips_when_auto_trading_is_off(self):
        with (
            patch.object(auto_trader, "auto_trading_status", return_value=(False, "off")),
            patch.object(auto_trader, "get_trading_client") as client,
            patch("builtins.print"),
        ):
            self.assertEqual(auto_trader.main([]), 0)
        client.assert_not_called()

    def test_buys_slot_picks(self):
        slots = Path(self.folder.name) / "slots.json"
        slots.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "selections": [
                {"ticker": "HCA", "source": "A_PLUS", "atr": 10.0},
                {"ticker": "MRNA", "source": "PULLBACK", "atr": 24.0},
            ],
        }))
        with (
            patch.object(auto_trader, "auto_trading_status", return_value=(True, "on")),
            patch.object(auto_trader, "get_trading_client", return_value=self.client),
            patch.object(auto_trader, "latest_price", lambda ticker: 200.0),
            patch.object(auto_trader, "SLOT_SELECTION_JSON", slots),
            patch.object(auto_trader, "FILL_WAIT_SECONDS", 0),
            patch.dict(os.environ, {"PAPER_JOURNAL_PATH": str(self.database)}),
            patch("builtins.print"),
        ):
            self.assertEqual(auto_trader.main([]), 0)

        self.assertEqual(
            [r.symbol for r in self.client.submitted], ["HCA", "MRNA"]
        )
        pending = get_pending_broker_orders(self.database)
        self.assertEqual([p["entry_source"] for p in pending], ["AUTO", "AUTO"])


if __name__ == "__main__":
    unittest.main()
