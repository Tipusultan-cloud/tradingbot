import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cash_parking import plan, rebalance
from paper_journal import add_broker_order, record_entry, record_partial_exit


class FakeAlpaca:
    def __init__(self, spy_qty=0.0, spy_price=500.0, market_open=True):
        self.market_open = market_open
        self.spy_qty = spy_qty
        self.spy_price = spy_price
        self.submitted = []
        self.closed = []

    def get_clock(self):
        return SimpleNamespace(is_open=self.market_open)

    def get_all_positions(self):
        positions = [SimpleNamespace(symbol="MPC", qty="3", market_value="1333", unrealized_pl="0.2")]
        if self.spy_qty:
            positions.append(SimpleNamespace(
                symbol="SPY", qty=str(self.spy_qty),
                market_value=str(self.spy_qty * self.spy_price), unrealized_pl="12.5",
            ))
        return positions

    def submit_order(self, request):
        self.submitted.append(request.to_request_fields())

    def close_position(self, symbol):
        self.closed.append(symbol)


class CashParkingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Path(self.folder.name) / "paper.sqlite3"
        self.env = patch.dict(os.environ, {
            "PAPER_ACCOUNT_BALANCE": "10000", "SPY_PARK_FRACTION": "0.5",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def run_rebalance(self, client):
        return rebalance(client, lambda ticker: 500.0, database_path=self.database)

    def test_half_of_idle_cash_goes_to_spy(self):
        client = FakeAlpaca()

        state = self.run_rebalance(client)

        self.assertEqual((state["idle"], state["target"]), (10000, 5000))
        order = client.submitted[0]
        self.assertEqual((order["symbol"], order["side"].value, order["notional"]), ("SPY", "buy", 5000))

    def test_money_in_trades_is_not_idle(self):
        record_entry("MPC", 444.58, 10, 3, 415.84, 500.3, database_path=self.database)
        record_entry("FTNT", 136.88, 5, 6, 120.94, 168.12, database_path=self.database)
        add_broker_order("o-1", "ABC", 10, 2.0, 97.0, 106.0, database_path=self.database)

        state = plan(FakeAlpaca(), lambda ticker: 100.0, database_path=self.database)

        in_trades = 444.58 * 3 + 136.88 * 6 + 10 * 100.0
        self.assertAlmostEqual(state["in_trades"], round(in_trades, 2))
        self.assertAlmostEqual(state["target"], round((10000 - in_trades) / 2, 2))

    def test_shares_sold_at_2r_free_cash(self):
        trade = record_entry("ABC", 100, 2, 40, 97, 106, target2_price=107.5, database_path=self.database)
        record_partial_exit(trade["id"], 20, 106, "2026-10-30T15:00:00Z", database_path=self.database)

        state = plan(FakeAlpaca(), lambda ticker: 100.0, database_path=self.database)

        self.assertEqual(state["in_trades"], 2000)

    def test_small_change_is_skipped(self):
        client = FakeAlpaca(spy_qty=9.8)  # $4,900 held, target $5,000

        state = self.run_rebalance(client)

        self.assertEqual(state["action"], "none")
        self.assertEqual(client.submitted, [])

    def test_extra_spy_is_sold_when_a_trade_uses_the_cash(self):
        record_entry("MPC", 500, 10, 8, 470, 560, database_path=self.database)  # $4,000 in trade
        client = FakeAlpaca(spy_qty=10)  # $5,000 held, target $3,000

        self.run_rebalance(client)

        order = client.submitted[0]
        self.assertEqual((order["symbol"], order["side"].value, order["qty"]), ("SPY", "sell", 4))

    def test_market_closed_sends_nothing(self):
        client = FakeAlpaca(market_open=False)

        state = self.run_rebalance(client)

        self.assertEqual(state["action"], "market closed")
        self.assertEqual(client.submitted, [])

    def test_fraction_zero_sells_all_parked_spy(self):
        client = FakeAlpaca(spy_qty=10)
        with patch.dict(os.environ, {"SPY_PARK_FRACTION": "0"}):
            self.run_rebalance(client)
        self.assertEqual(client.closed, ["SPY"])

    def test_other_positions_are_not_counted_as_spy(self):
        state = plan(FakeAlpaca(), lambda ticker: 500.0, database_path=self.database)
        self.assertEqual((state["shares"], state["value"]), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
