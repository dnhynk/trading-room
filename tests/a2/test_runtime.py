import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from tests.a2.fakes import Client, Clock, approved_config
from track_a_2.execution.store import Store
from track_a_2.market.feed import Market
from track_a_2.runtime import Runtime
from track_a_2.settings import load
from track_c.execution.coinone import CoinoneError, EntryExpired


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
        self.config = approved_config(self.root, self.config)
        self.config_path = self.root / "track_a_2" / "config.json"
        self.config_path.parent.mkdir(exist_ok=True)
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
            bids=[{"price": "100", "qty": "5000"}],
            asks=[{"price": "100.01", "qty": "5000"}],
        )
        now = time.time_ns() // 1_000_000
        market.book_received = market.book_exchange = now
        self.runtime.markets = {"AAA": market}
        self.runtime.oms.sync_account(self.client.balances(), [], {"AAA": "0.1"})
        self.runtime.oms.set_selection(dict(selected=["AAA"], wind_down=[], watch=["AAA"]), {})
        self.runtime.oms.set_strategy_params("AAA", dict(
            max_notional=50000, qstep=0.1, unit_qty=100,
            campaign_loss_budget_krw=500,
        ))
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

    def protect(self, *, confirm=True):
        self.runtime.drive("AAA", self.desired(trigger=90, limit=89), fresh=True)
        order = self.runtime.oms.active("AAA", "protect")[0]
        if confirm:
            self.runtime.reconcile(force=True)
            self.runtime.oms.sync_account(
                self.client.balances(), self.client.active_orders(), {"AAA": "0.1"}
            )
        return order

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
        self.runtime.markets["AAA"].book = dict(
            bids=[{"price": "102", "qty": "1000"}],
            asks=[{"price": "102.01", "qty": "1000"}],
        )
        trim = dict(price=101, qty=50, requested_scope="maker", lot=None, tag=None)
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        self.assertIn(protect["cid"], self.client.cancellations)
        self.assertFalse(self.runtime.oms.active("AAA", "trim"))
        self.client.balance_rows[1] = dict(currency="AAA", available="100", limit="0")
        self.runtime.oms.sync_account(self.client.balances(), self.client.active_orders(), {"AAA": "0.1"})
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        order = self.runtime.oms.active("AAA", "trim")[0]
        self.assertEqual(order["type"], "MARKET")
        self.assertEqual(order["limit_price"], "101")
        self.assertEqual(order["requested_scope"], "maker")

    def test_partial_trim_reprotects_only_the_remaining_inventory(self):
        self.buy_and_fill(200)
        self.protect()
        self.runtime.markets["AAA"].book = dict(
            bids=[{"price": "102", "qty": "2000"}],
            asks=[{"price": "102.01", "qty": "2000"}],
        )
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

    def test_stale_market_keeps_protection_until_exit_can_replace_it(self):
        self.buy_and_fill()
        protect = self.protect()
        self.runtime.oms.book("AAA")["exit_reason"] = "stop_limit_gap"
        self.runtime.drive("AAA", None, fresh=False)
        self.assertNotIn(protect["cid"], self.client.cancellations)
        self.runtime.drive("AAA", None, fresh=True)
        self.assertIn(protect["cid"], self.client.cancellations)

    def test_shutdown_cancels_entry_and_leaves_native_protection(self):
        self.buy_and_fill()
        protect = self.protect()
        self.runtime.drive("AAA", self.desired(buy=(99, 100), trigger=90, limit=89), fresh=True)
        buy = self.runtime.oms.active("AAA", "buy")[0]
        self.runtime.drive("AAA", None, fresh=False, stopping=True)
        self.assertIn(buy["cid"], self.client.cancellations)
        self.assertEqual(self.runtime.oms.active("AAA", "protect")[0]["cid"], protect["cid"])
        self.assertTrue(self.runtime.shutdown_ready())

    def test_unconfirmed_protection_neither_allows_entry_nor_shutdown(self):
        self.buy_and_fill()
        protect = self.protect(confirm=False)
        self.assertEqual(protect["status"], "SUBMITTED")
        before = len(self.client.submissions)
        self.runtime.drive(
            "AAA", self.desired(buy=(99, 100), trigger=90, limit=89), fresh=True
        )
        self.assertEqual(len(self.client.submissions), before)
        self.assertFalse(self.runtime.shutdown_ready())
        self.runtime.reconcile(force=True)
        self.assertEqual(protect["status"], "NOT_TRIGGERED")
        self.assertTrue(self.runtime.shutdown_ready())

    def test_undersized_trim_does_not_cancel_existing_protection(self):
        self.buy_and_fill()
        protect = self.protect()
        trim = dict(price=100, qty=10, requested_scope="taker", lot=None, tag=None)
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        self.assertNotIn(protect["cid"], self.client.cancellations)
        self.assertEqual(self.runtime.oms.active("AAA", "protect")[0]["cid"], protect["cid"])

    def test_profit_trim_waits_for_executable_bid_depth_above_its_gate(self):
        self.buy_and_fill()
        protect = self.protect()
        trim = dict(
            price=100, qty=50, requested_scope="taker", lot=None, tag=None,
            purpose="profit", ref=100, gate_pct=0.15,
        )
        self.runtime.markets["AAA"].book = dict(
            bids=[{"price": "100.10", "qty": "1000"}],
            asks=[{"price": "100.11", "qty": "1000"}],
        )
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        self.assertNotIn(protect["cid"], self.client.cancellations)
        self.runtime.markets["AAA"].book = dict(
            bids=[{"price": "100.20", "qty": "1000"}],
            asks=[{"price": "100.21", "qty": "1000"}],
        )
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        self.assertIn(protect["cid"], self.client.cancellations)

    def test_deferred_profit_after_protection_cancel_reprotects_inventory(self):
        self.buy_and_fill()
        old = self.protect()
        trim = dict(
            price=100, qty=50, requested_scope="taker", lot=None, tag=None,
            purpose="profit", ref=100, gate_pct=0.15,
        )
        self.runtime.markets["AAA"].book = dict(
            bids=[{"price": "100.20", "qty": "1000"}],
            asks=[{"price": "100.21", "qty": "1000"}],
        )
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        self.assertIn(old["cid"], self.client.cancellations)
        self.runtime.oms.sync_account(
            self.client.balances(), self.client.active_orders(), {"AAA": "0.1"}
        )
        self.runtime.markets["AAA"].book = dict(
            bids=[{"price": "100.10", "qty": "1000"}],
            asks=[{"price": "100.22", "qty": "1000"}],
        )
        self.runtime.drive("AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True)
        protects = self.runtime.oms.active("AAA", "protect")
        self.assertEqual(len(protects), 1)
        self.assertNotEqual(protects[0]["cid"], old["cid"])
        self.assertEqual(self.runtime.oms.book("AAA")["inventory_phase"], "reprotecting")

    def test_undersized_partial_trim_remainder_returns_to_full_protection(self):
        self.buy_and_fill()
        self.protect()
        trim = dict(
            price=100, qty=50, requested_scope="taker", lot=None, tag=None,
            purpose="profit", ref=100, gate_pct=0.1,
        )
        self.runtime.markets["AAA"].book = dict(
            bids=[{"price": "101", "qty": "1000"}],
            asks=[{"price": "101.01", "qty": "1000"}],
        )
        desired = self.desired(trim=trim, trigger=90, limit=89)
        self.runtime.drive("AAA", desired, fresh=True)
        self.runtime.oms.sync_account(
            self.client.balances(), self.client.active_orders(), {"AAA": "0.1"}
        )
        self.runtime.drive("AAA", desired, fresh=True)
        sale = self.runtime.oms.active("AAA", "trim")[0]
        self.client.fill(sale["cid"], "40", "101", status="PARTIALLY_FILLED")
        self.runtime.reconcile(force=True)
        self.client.balance_rows = [
            dict(currency="KRW", available="94040", limit="0"),
            dict(currency="AAA", available="60", limit="0"),
        ]
        self.runtime.oms.sync_account(
            self.client.balances(), self.client.active_orders(), {"AAA": "0.1"}
        )
        small = dict(trim, qty=10)
        small_desired = self.desired(trim=small, trigger=90, limit=89)
        self.runtime.drive("AAA", small_desired, fresh=True)
        self.assertFalse(self.runtime.oms.active("AAA", "trim"))
        self.runtime.oms.sync_account(
            self.client.balances(), self.client.active_orders(), {"AAA": "0.1"}
        )
        self.runtime.drive("AAA", small_desired, fresh=True)
        protect = self.runtime.oms.active("AAA", "protect")[0]
        self.assertEqual(protect["qty"], "60")

    def test_resting_buy_does_not_cancel_itself_at_portfolio_ceiling(self):
        market = self.runtime.markets["AAA"]
        market.book = dict(
            bids=[{"price": "100", "qty": "10000"}],
            asks=[{"price": "100.01", "qty": "10000"}],
        )
        self.runtime.oms.set_strategy_params("AAA", dict(
            max_notional=60000, qstep=0.1, unit_qty=550,
            campaign_loss_budget_krw=500,
        ))
        self.buy_and_fill(550)
        self.protect()
        self.runtime.connected = self.runtime.private_connected = True
        desired = self.desired(buy=(100, 50), trigger=90, limit=89)
        self.runtime.drive("AAA", desired, fresh=True)
        order = self.runtime.oms.active("AAA", "buy")[0]
        self.runtime.drive("AAA", desired, fresh=True)
        self.assertEqual(self.runtime.oms.active("AAA", "buy")[0]["cid"], order["cid"])

    def test_resting_buy_survives_missing_feature_decision_with_fresh_book(self):
        self.runtime.connected = self.runtime.private_connected = True
        desired = self.desired(buy=(100, 100))
        self.runtime.drive("AAA", desired, fresh=True)
        order = self.runtime.oms.active("AAA", "buy")[0]

        self.runtime.drive("AAA", None, fresh=True)

        self.assertEqual(self.runtime.oms.active("AAA", "buy")[0]["cid"], order["cid"])
        self.assertNotIn(order["cid"], self.client.cancellations)
        self.assertEqual(self.runtime.counts["resting_buy_held_without_decision"], 1)

    def test_resting_buy_survives_static_book_until_signal_ttl(self):
        self.runtime.connected = self.runtime.private_connected = True
        self.runtime.drive("AAA", self.desired(buy=(100, 100)), fresh=True)
        order = self.runtime.oms.active("AAA", "buy")[0]
        self.runtime.markets["AAA"].book_received = 0
        self.runtime.markets["AAA"].book_exchange = 0

        self.runtime.drive("AAA", None, fresh=False)

        self.assertNotIn(order["cid"], self.client.cancellations)
        self.assertEqual(self.runtime.counts["resting_buy_held_without_decision"], 1)

    def test_resting_buy_without_decision_cancels_at_signal_ttl(self):
        self.runtime.connected = self.runtime.private_connected = True
        self.runtime.drive("AAA", self.desired(buy=(100, 100)), fresh=True)
        order = self.runtime.oms.active("AAA", "buy")[0]
        self.clock.advance(self.config["strategy"]["buy_ttl_s"] + 0.01)

        self.runtime.drive("AAA", None, fresh=False)

        self.assertIn(order["cid"], self.client.cancellations)
        self.assertEqual(
            self.runtime.counts["resting_buy_rejected_entry_signal_expired"], 1,
        )

    def test_resting_buy_without_decision_cancels_on_disconnect(self):
        self.runtime.connected = self.runtime.private_connected = True
        self.runtime.drive("AAA", self.desired(buy=(100, 100)), fresh=True)
        order = self.runtime.oms.active("AAA", "buy")[0]
        self.runtime.connected = False

        self.runtime.drive("AAA", None, fresh=False)

        self.assertIn(order["cid"], self.client.cancellations)
        self.assertEqual(
            self.runtime.counts["resting_buy_rejected_runtime_unavailable"], 1,
        )

    def test_new_buy_intent_still_requires_a_fresh_book(self):
        self.runtime.connected = self.runtime.private_connected = True
        self.runtime.markets["AAA"].book_received = 0
        self.runtime.markets["AAA"].book_exchange = 0

        reason = self.runtime.validate_intent(
            "AAA", "buy", "BUY", 100, fee_rate=0, price=100,
        )

        self.assertEqual(reason, "market_age")

    def test_explicit_strategy_disarm_still_cancels_resting_buy(self):
        self.runtime.connected = self.runtime.private_connected = True
        self.runtime.drive("AAA", self.desired(buy=(100, 100)), fresh=True)
        order = self.runtime.oms.active("AAA", "buy")[0]

        self.runtime.drive("AAA", self.desired(), fresh=True)

        self.assertIn(order["cid"], self.client.cancellations)

    def test_missing_feature_decision_cannot_hold_buy_through_pause(self):
        self.runtime.connected = self.runtime.private_connected = True
        self.runtime.drive("AAA", self.desired(buy=(100, 100)), fresh=True)
        order = self.runtime.oms.active("AAA", "buy")[0]
        (self.root / "PAUSE").touch()

        self.runtime.drive("AAA", None, fresh=True)

        self.assertIn(order["cid"], self.client.cancellations)
        self.assertEqual(
            self.runtime.counts["resting_buy_rejected_repository_pause"], 1,
        )

    def test_empty_initial_feature_is_not_passed_to_strategy(self):
        self.runtime.markets["AAA"].features.f = {}
        self.assertIsNone(self.runtime.desired("AAA", time.time_ns() // 1_000_000))

    def test_recovery_only_runtime_cannot_submit_a_buy(self):
        self.runtime.recovery_only = True
        self.runtime.drive("AAA", self.desired(buy=(100, 100)), fresh=True)
        self.assertEqual(self.runtime.oms.active("AAA", "buy"), [])

    def test_recovery_owner_ignores_repository_stop_but_honors_its_own_stop(self):
        self.runtime.recovery_only = True
        (self.root / "STOP").touch()
        self.assertFalse(self.runtime.controls()["stop"])
        (self.directory / "STOP").touch()
        self.assertTrue(self.runtime.controls()["stop"])

    def test_dynamic_fee_is_a_true_floor_for_all_profit_gates(self):
        market = self.runtime.markets["AAA"]
        market.fees = {"maker": "0.001", "taker": "0.001"}
        params = {
            **self.config["strategy"],
            "side": "long", "unit_qty": 100, "max_notional": 50000,
            "cap_usdt": 500, "campaign_loss_budget_krw": 500,
            "tick": 0.01, "qstep": 0.1, "fee_rt_pct": 0.1,
            "lever": 0, "margin_mode": None, "wallet_frac": 1.0,
            "wind_down": False, "unit_frac": 0, "cap_frac": 0,
            "daily_loss_frac": 0, "notional_frac": 0,
            "sizing_policy_digest": self.runtime.sizing_policy_digest,
        }
        self.runtime.oms.set_strategy_params("AAA", params)
        strategy = self.runtime._strategy("AAA")
        self.assertEqual(strategy.p["fee_rt_pct"], 0.2)
        self.assertGreaterEqual(strategy.p["pop_min_pct"], 0.2)
        self.assertGreaterEqual(strategy.p["unit_min_pct"], 0.2)
        self.assertGreaterEqual(strategy.p["gate_floor_unit_pct"], 0.2)

    def test_flat_book_recomputes_sizing_from_the_active_release_policy(self):
        stale = dict(self.runtime.oms.book("AAA")["strategy_params"])
        self.assertEqual(stale["unit_qty"], 100)
        self.assertNotIn("sizing_policy_digest", stale)

        strategy = self.runtime._strategy("AAA")
        params = self.runtime.oms.book("AAA")["strategy_params"]

        self.assertEqual(params["sizing_policy_digest"], self.runtime.sizing_policy_digest)
        self.assertEqual(params["max_units"], 4)
        self.assertEqual(params["campaign_loss_budget_krw"], 2000.0)
        self.assertEqual(params["max_notional"], 60000.0)
        self.assertEqual(strategy.p["unit_qty"], params["unit_qty"])

    def test_positioned_book_keeps_campaign_sizing_across_release_change(self):
        self.buy_and_fill()
        before = dict(self.runtime.oms.book("AAA")["strategy_params"])

        strategy = self.runtime._strategy("AAA")

        self.assertEqual(self.runtime.oms.book("AAA")["strategy_params"], before)
        self.assertEqual(strategy.p["unit_qty"], before["unit_qty"])

    def test_expired_signal_is_not_paired_with_current_features(self):
        now_ms = time.time_ns() // 1_000_000
        market = self.runtime.markets["AAA"]
        market.features.f = dict(
            t=now_ms // 1000 - 1, mid=100.005, bid=100, ask=100.01,
            v=0, brk=False, bko=False,
        )
        market.signals.append(dict(sig="DIP_SLOWING", t=now_ms // 1000 - 60))
        seen = []

        class StrategyStub:
            pull = None

            def step(self, feature, signals, position, working):
                seen.extend(signals)
                return dict(buy=None, trim=None, stop=None, no_stop=False, events=[])

        self.runtime._strategy = lambda coin: StrategyStub()
        self.runtime.desired("AAA", now_ms)
        self.assertEqual(seen, [])
        self.assertEqual(self.runtime.counts["expired_signals"], 1)

    def test_unconfirmed_protection_times_out_into_recovery_exit(self):
        self.buy_and_fill()
        protect = self.protect(confirm=False)
        self.clock.advance(self.config["reconcile_halt_s"])
        self.runtime.drive("AAA", self.desired(trigger=90, limit=89), fresh=True)
        self.assertIn(protect["cid"], self.client.cancellations)
        self.assertEqual(
            self.runtime.oms.book("AAA")["exit_reason"], "protection_unconfirmed"
        )

    def test_cancel_requested_protection_is_not_treated_as_confirmed(self):
        self.buy_and_fill()
        protect = self.protect()
        self.client.cancel_error = CoinoneError("cancel response lost")
        self.runtime.markets["AAA"].book = dict(
            bids=[{"price": "102", "qty": "1000"}],
            asks=[{"price": "102.01", "qty": "1000"}],
        )
        trim = dict(
            price=101, qty=50, requested_scope="taker", lot=None, tag=None,
            purpose="profit", ref=100, gate_pct=0.1,
        )
        self.runtime.drive(
            "AAA", self.desired(trim=trim, trigger=90, limit=89), fresh=True
        )
        self.assertTrue(protect.get("cancel_requested"))
        self.assertFalse(self.runtime.shutdown_ready())
        self.clock.advance(self.config["cancel_retry_s"] + 0.01)
        self.client.cancel_error = None
        self.runtime.drive("AAA", self.desired(trigger=90, limit=89), fresh=True)
        self.assertEqual(len(self.client.cancellations), 2)

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

    def test_entry_guard_rechecks_current_two_sided_depth_and_quantity(self):
        async def exercise():
            self.runtime.submission_guard = Runtime.submission_guard.__get__(self.runtime, Runtime)
            self.runtime.loop = asyncio.get_running_loop()
            self.runtime.connected = self.runtime.private_connected = self.runtime.storage_ok = True
            order = dict(
                cid="ta2-buy-depth1234", coin="AAA", role="buy", side="BUY", type="LIMIT",
                qty="100", price="100", fee_rate="0", status="INTENT", filled="0",
                gross="0", fee="0", created=self.clock(),
            )
            self.runtime.oms.state["orders"][order["cid"]] = order
            self.runtime.markets["AAA"].book = dict(
                bids=[{"price": "100", "qty": "1"}],
                asks=[{"price": "100.01", "qty": "1"}],
            )
            now = time.time_ns() // 1_000_000
            self.runtime.markets["AAA"].book_received = now
            self.runtime.markets["AAA"].book_exchange = now
            guard = self.runtime.submission_guard(
                "AAA", "buy", "BUY", "100", fee_rate="0", price="100",
            )
            with self.assertRaisesRegex(EntryExpired, "entry_size_changed"):
                await asyncio.to_thread(guard)
        asyncio.run(exercise())

    def test_metadata_discovery_does_not_mutate_until_owner_applies_it(self):
        class ScanClient(Client):
            def universe(inner):
                ticker = dict(
                    quote_currency="KRW", target_currency="AAA",
                    quote_volume="10000000000", high="110", low="100", last="105",
                    best_bids=[{"price": "104.9", "qty": "1000"}],
                    best_asks=[{"price": "105", "qty": "1000"}],
                )
                return [dict(CONTRACT)], [ticker]

            def fees(inner, coin):
                return {"maker": "0.001", "taker": "0.002"}

            def price_units(inner, coin):
                return [{"range_min": "0", "price_unit": "0.1"}]

        market = self.runtime.markets["AAA"]
        old_units = market.units
        self.runtime.client = ScanClient()
        snapshot = asyncio.run(self.runtime._discover())
        self.assertIs(market.units, old_units)
        self.runtime._apply_scan(snapshot)
        self.assertIs(self.runtime.markets["AAA"], market)
        self.assertEqual(market.fees, {"maker": "0.001", "taker": "0.002"})


if __name__ == "__main__":
    unittest.main()
