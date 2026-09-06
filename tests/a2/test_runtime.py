import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from tests.a2.fakes import Client, Clock
from track_a_2.execution.store import Store
from track_a_2.market.feed import Market
from track_a_2.runtime import Runtime
from track_a_2.settings import load
from track_c.execution.coinone import EntryExpired


UNITS = [{"range_min": "0", "price_unit": "0.01"}]
CONTRACT = dict(
    quote_currency="KRW", target_currency="AAA", trade_status=1,
    maintenance_status=0, order_types=["limit", "market", "stop_limit"],
    qty_unit="0.1", min_qty="0.1", max_qty="100000",
    min_order_amount="5000", max_order_amount="1000000000",
)


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        parent = Path(self.folder.name)
        self.root = parent / "trading-room"
        (self.root / "config").mkdir(parents=True)
        self.directory = parent / "trading-room-state" / "track-a-2"
        self.config = copy.deepcopy(load())
        self.config.update(
            status="active", mode="live", execution_enabled=True,
            portfolio_isolation_confirmed=True,
            live_approval_id="a2-eval-runtime-v1",
            expected_egress_ip="203.0.113.7",
            universe=["AAA"], basket_size=1, max_open_books=1,
        )
        self.config_path = self.root / "track_a_2" / "config.json"
        self.config_path.parent.mkdir()
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        (self.root / "config" / "tracks.json").write_text(json.dumps(dict(
            tracks={"A-2": {"status": "active", "execution_enabled": True}}
        )), encoding="utf-8")
        self.clock = Clock()
        self.client = Client()
        self.store = Store(self.directory)
        self.addCleanup(self.store.close)
        self.runtime = Runtime(
            self.config, config_path=self.config_path, root=self.root,
            client=self.client, store=self.store, clock=self.clock,
        )
        market = Market("AAA", self.config, CONTRACT, UNITS, {"maker": "0", "taker": "0"})
        market.book = dict(
            bids=[{"price": "100", "qty": "1000"}],
            asks=[{"price": "100.01", "qty": "1000"}],
        )
        now = time.time_ns() // 1_000_000
        market.book_received = market.book_exchange = now
        self.runtime.markets = {"AAA": market}
        self.runtime.oms.sync_account(self.client.balances(), [], {"AAA": "0.1"})
        self.runtime.oms.set_selection(dict(selected=["AAA"], wind_down=[], watch=["AAA"]), {})
        self.runtime.oms.set_strategy_params("AAA", dict(max_notional=50000, qstep=0.1))
        self.runtime.submission_guard = lambda *args, **kwargs: (lambda: None)

    @staticmethod
    def desired(*, buy=None, trim=None, trigger=None, limit=None, no_stop=False):
        return dict(buy=buy, trim=trim, trigger=trigger, limit=limit, no_stop=no_stop)

    def buy_and_fill(self, qty=100):
        self.runtime.drive("AAA", self.desired(buy=(100, qty)), fresh=True)
        order = self.runtime.oms.active("AAA", "buy")[0]
        self.client.fill(order["cid"], str(qty), "100")
        fills, _ = self.runtime.reconcile(force=True)
        self.runtime.notify_fills(fills)
        self.client.balance_rows = [
            dict(currency="KRW", available=str(100000 - qty * 100), limit="0"),
            dict(currency="AAA", available=str(qty), limit="0"),
        ]
        self.runtime.oms.sync_account(self.client.balances(), self.client.active_orders(), {"AAA": "0.1"})

    def protect(self):
        self.runtime.drive("AAA", self.desired(trigger=90, limit=89), fresh=True)
        return self.runtime.oms.active("AAA", "protect")[0]

    def test_buy_fill_is_protected_before_another_entry(self):
        self.buy_and_fill()
        protect = self.protect()
        self.assertEqual((protect["side"], protect["type"]), ("SELL", "STOP_LIMIT"))
        self.assertEqual((protect["trigger_price"], protect["price"]), ("90", "89"))
        self.runtime.drive("AAA", self.desired(buy=(99, 100), trigger=90, limit=89), fresh=True)
        self.assertTrue(self.runtime.oms.active("AAA", "buy"))
        self.assertTrue(self.runtime.oms.active("AAA", "protect"))

    def test_trim_cancels_and_reconciles_protection_before_bounded_market_sell(self):
        self.buy_and_fill()
        protect = self.protect()
        trim = dict(price=101, qty=50, requested_scope="maker", lot=None, tag=None)
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        self.assertIn(protect["cid"], self.client.cancellations)
        self.assertFalse(self.runtime.oms.active("AAA", "trim"))
        self.client.balance_rows[1] = dict(currency="AAA", available="100", limit="0")
        self.runtime.oms.sync_account(self.client.balances(), self.client.active_orders(), {"AAA": "0.1"})
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        order = self.runtime.oms.active("AAA", "trim")[0]
        self.assertEqual(order["type"], "MARKET")
        self.assertEqual(order["limit_price"], "89")
        self.assertEqual(order["requested_scope"], "maker")

    def test_partial_trim_reprotects_only_the_remaining_inventory(self):
        self.buy_and_fill(200)
        self.protect()
        trim = dict(price=101, qty=100, requested_scope="taker", lot=None, tag=None)
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        self.runtime.oms.sync_account(self.client.balances(), [], {"AAA": "0.1"})
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        order = self.runtime.oms.active("AAA", "trim")[0]
        self.client.fill(order["cid"], "100", "100")
        self.runtime.reconcile(force=True)
        self.client.balance_rows = [
            dict(currency="KRW", available="90000", limit="0"),
            dict(currency="AAA", available="100", limit="0"),
        ]
        self.runtime.oms.sync_account(self.client.balances(), [], {"AAA": "0.1"})
        self.runtime.drive("AAA", self.desired(trigger=90, limit=89), fresh=True)
        protect = self.runtime.oms.active("AAA", "protect")[0]
        self.assertEqual(protect["qty"], "100")

    def test_triggered_stop_gap_cancels_then_market_exits_without_duplicate_sell(self):
        self.buy_and_fill()
        protect = self.protect()
        self.client.rows[protect["cid"]]["status"] = "TRIGGERED"
        self.runtime.reconcile(force=True)
        self.runtime.markets["AAA"].book = dict(
            bids=[{"price": "80", "qty": "1000"}],
            asks=[{"price": "80.01", "qty": "1000"}],
        )
        self.runtime.drive("AAA", self.desired(trigger=90, limit=89), fresh=True)
        self.assertFalse(self.runtime.oms.active("AAA", "protect"))
        self.assertFalse(self.runtime.oms.active("AAA", "exit"))
        self.client.balance_rows[1] = dict(currency="AAA", available="100", limit="0")
        self.runtime.oms.sync_account(self.client.balances(), [], {"AAA": "0.1"})
        self.runtime.drive("AAA", self.desired(trigger=90, limit=89), fresh=True)
        exit_order = self.runtime.oms.active("AAA", "exit")[0]
        self.assertEqual(exit_order["type"], "MARKET")
        self.assertNotIn("limit_price", exit_order)

    def test_shutdown_cancels_entry_and_leaves_native_protection(self):
        self.buy_and_fill()
        protect = self.protect()
        self.runtime.drive("AAA", self.desired(buy=(99, 100), trigger=90, limit=89), fresh=True)
        buy = self.runtime.oms.active("AAA", "buy")[0]
        self.runtime.drive("AAA", None, fresh=False, stopping=True)
        self.assertIn(buy["cid"], self.client.cancellations)
        self.assertEqual(self.runtime.oms.active("AAA", "protect")[0]["cid"], protect["cid"])
        self.assertTrue(self.runtime.shutdown_ready())

    def test_entry_guard_rechecks_dynamic_controls_before_send(self):
        async def exercise():
            self.runtime.submission_guard = Runtime.submission_guard.__get__(self.runtime, Runtime)
            self.runtime.loop = asyncio.get_running_loop()
            self.runtime.connected = self.runtime.private_connected = self.runtime.storage_ok = True
            self.runtime.oms.state["orders"]["ta2-buy-guard1234"] = dict(
                cid="ta2-buy-guard1234", coin="AAA", role="buy", side="BUY", type="LIMIT",
                qty="100", price="100", fee_rate="0", status="INTENT", filled="0",
                gross="0", fee="0", created=self.clock(),
            )
            guard = self.runtime.submission_guard("AAA", "buy", "BUY", "100", price="100")
            await asyncio.to_thread(guard)
            (self.root / "PAUSE").touch()
            with self.assertRaisesRegex(EntryExpired, "repository_pause"):
                await asyncio.to_thread(guard)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
