import base64
import copy
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from tests.a2.fakes import Client, Clock
from track_a_2.execution.client import CoinoneA2, read_credentials
from track_a_2.execution.oms import OMS
from track_a_2.execution.store import Store
from track_a_2.settings import load
from track_c.execution.coinone import CoinoneError, Credentials, EntryExpired


class ClientTests(unittest.TestCase):
    def test_credentials_use_only_dedicated_a2_names(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / ".env"
        path.write_text("COINONE_ACCESS_TOKEN=c-token\nCOINONE_SECRET_KEY=c-secret\n", encoding="utf-8")
        with self.assertRaisesRegex(CoinoneError, "dedicated"):
            read_credentials(path, environ={})
        credentials = read_credentials(path, environ={
            "COINONE_A2_ACCESS_TOKEN": "a2-token",
            "COINONE_A2_SECRET_KEY": "a2-secret",
        })
        self.assertEqual(credentials.access_token, "a2-token")

    def test_shared_credential_profile_is_explicit_and_never_a_fallback(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / ".env"
        path.write_text(
            "COINONE_ACCESS_TOKEN=c-token\nCOINONE_SECRET_KEY=c-secret\n",
            encoding="utf-8",
        )
        credentials = read_credentials(path, environ={}, profile="coinone_default")
        self.assertEqual(credentials.access_token, "c-token")
        with self.assertRaisesRegex(CoinoneError, "dedicated"):
            read_credentials(path, environ={}, profile="a2")

    def test_order_allowlist_and_post_only_payload(self):
        requests = []

        def transport(request, timeout):
            requests.append(request)
            return {"result": "success", "error_code": "0", "order_id": "one"}

        client = CoinoneA2(Credentials("token", "secret"), transport=transport)
        called = []
        client.submit(dict(
            coin="BTC", cid="ta2-buy-12345678", side="BUY", type="LIMIT",
            qty="0.001", price="100000000",
        ), before_send=lambda: called.append(True))
        payload = json.loads(requests[0].data)
        self.assertTrue(payload["post_only"])
        self.assertEqual(payload["price"], "100000000")
        self.assertEqual(called, [True])
        decoded = json.loads(base64.b64decode(requests[0].headers["X-coinone-payload"]))
        self.assertEqual(decoded["user_order_id"], "ta2-buy-12345678")

    def test_foreign_identifier_and_buy_market_are_rejected_before_transport(self):
        client = CoinoneA2(Credentials("token", "secret"), transport=lambda *_: self.fail("transport called"))
        with self.assertRaisesRegex(CoinoneError, "identifier"):
            client.submit(dict(coin="BTC", cid="tc-entry-12345678", side="BUY", type="LIMIT", qty="1", price="1"))
        with self.assertRaisesRegex(CoinoneError, "unsupported"):
            client.submit(dict(coin="BTC", cid="ta2-buy-12345678", side="BUY", type="MARKET", qty="1"))


class OMSTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock = Clock()
        self.client = Client()
        self.config = {
            **load(), "status": "active", "mode": "live", "execution_enabled": True,
            "portfolio_isolation_confirmed": True,
        }
        self.store = Store(self.folder.name)
        self.addCleanup(self.store.close)
        self.oms = OMS(self.config, self.client, self.store, clock=self.clock)
        self.oms.sync_account(self.client.balances(), [], {"AAA": "0.1"})
        self.oms.set_selection(dict(selected=["AAA"], wind_down=[], watch=["AAA"]), {})
        self.oms.set_strategy_params("AAA", dict(
            max_notional=50000, qstep=0.1, unit_qty=100,
            campaign_loss_budget_krw=500,
        ))

    def submit_buy(self):
        return self.oms.submit("AAA", "buy", "BUY", "LIMIT", "100", fee_rate="0.001", price="100")

    def test_partial_fill_is_idempotent_and_cancel_releases_only_after_detail(self):
        order = self.submit_buy()
        self.client.fill(order["cid"], "60", "100", fee="6", status="PARTIALLY_FILLED")
        fills = self.oms.reconcile(force=True)
        self.assertEqual(fills[0]["qty"], "60")
        self.assertEqual(self.oms.quantity("AAA"), 60)
        self.assertEqual(self.oms.state["cash_krw"], "93994")
        self.assertEqual(self.oms.reconcile(force=True), [])
        self.oms.cancel(order)
        self.assertFalse(self.oms.active("AAA", "buy"))
        self.assertEqual(self.oms.quantity("AAA"), 60)

    def test_initial_campaign_budget_tracks_actual_partial_fill(self):
        order = self.oms.submit(
            "AAA", "buy", "BUY", "LIMIT", "10", fee_rate="0", price="100"
        )
        self.client.fill(order["cid"], "5", "100", status="PARTIALLY_FILLED")
        self.oms.reconcile(force=True)
        self.assertEqual(D(self.oms.book("AAA")["campaign_budget"]), D("25"))
        self.client.fill(order["cid"], "10", "100")
        self.oms.reconcile(force=True)
        self.assertEqual(D(self.oms.book("AAA")["campaign_budget"]), D("50"))

    def test_uncertain_submission_is_owned_and_reconciled_by_user_order_id(self):
        self.client.submit_error = CoinoneError("network or response decoding failure")
        self.client.accept_before_error = True
        order = self.submit_buy()
        self.assertEqual(order["status"], "UNKNOWN")
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            self.submit_buy()
        self.client.submit_error = None
        self.client.fill(order["cid"], "100", "100")
        self.oms.reconcile(force=True)
        self.assertEqual(self.oms.quantity("AAA"), 100)

    def test_pre_send_expiry_is_known_not_transmitted(self):
        order = self.oms.submit(
            "AAA", "buy", "BUY", "LIMIT", "100", fee_rate="0",
            price="100", before_send=lambda: (_ for _ in ()).throw(EntryExpired("market_age")),
        )
        self.assertTrue(order["not_sent"])
        self.assertEqual(order["status"], "REJECTED")
        self.assertFalse(self.oms.active("AAA"))
        self.assertEqual(self.client.submissions, [])

    def test_fee_above_declared_ceiling_records_fill_then_halts_entries(self):
        order = self.submit_buy()
        self.client.fill(order["cid"], "100", "100", fee="100")
        fills = self.oms.reconcile(force=True)
        self.assertEqual(fills[0]["qty"], "100")
        self.assertEqual(self.oms.quantity("AAA"), 100)
        self.assertEqual(self.oms.state["cash_krw"], "89900")
        self.assertEqual(self.oms.state["halt"], "FEE_MISMATCH")

    def test_fee_only_cumulative_correction_updates_cash_and_pnl(self):
        order = self.submit_buy()
        self.client.fill(order["cid"], "100", "100", fee="5", status="PARTIALLY_FILLED")
        self.oms.reconcile(force=True)
        before_cash = D(self.oms.state["cash_krw"])
        before_pnl = D(self.oms.state["realized"])
        self.client.fill(order["cid"], "100", "100", fee="6", status="FILLED")
        corrections = self.oms.reconcile(force=True)
        self.assertEqual(corrections[0]["qty"], "0")
        self.assertEqual(D(self.oms.state["cash_krw"]), before_cash - 1)
        self.assertEqual(D(self.oms.state["realized"]), before_pnl - 1)

    def test_late_fee_correction_stays_with_original_campaign(self):
        first = self.submit_buy()
        self.client.fill(first["cid"], "100", "100")
        self.oms.reconcile(force=True)
        old_campaign = first["campaign_id"]
        sale = self.oms.submit(
            "AAA", "trim", "SELL", "MARKET", "100", fee_rate="0",
            reason="take_profit",
        )
        self.client.fill(sale["cid"], "100", "100", fee="0")
        self.oms.reconcile(force=True)
        self.assertTrue(self.oms.finish_flat("AAA"))
        self.oms.set_strategy_params("AAA", dict(
            max_notional=50000, qstep=0.1, unit_qty=100,
            campaign_loss_budget_krw=500,
        ))
        second = self.submit_buy()
        self.client.fill(second["cid"], "100", "90")
        self.oms.reconcile(force=True)
        self.assertNotEqual(second["campaign_id"], old_campaign)
        before = D(self.oms.book("AAA")["campaign_realized"])
        self.client.fill(sale["cid"], "100", "100", fee="1")
        self.oms.reconcile(force=True)
        self.assertEqual(D(self.oms.book("AAA")["campaign_realized"]), before)
        late = [
            json.loads(body)
            for kind, body in self.store.db.execute(
                "SELECT kind,body FROM events WHERE kind='CAMPAIGN_LATE_PNL'"
            )
        ]
        self.assertEqual(late[-1]["campaign_id"], old_campaign)
        self.assertEqual(late[-1]["pnl"], "-1")

    def test_terminal_order_is_rechecked_for_late_fee_correction(self):
        order = self.submit_buy()
        self.client.fill(order["cid"], "100", "100", fee="5", status="FILLED")
        self.oms.reconcile(force=True)
        before_cash = D(self.oms.state["cash_krw"])
        self.client.fill(order["cid"], "100", "100", fee="6", status="FILLED")
        corrections = self.oms.reconcile(force=True)
        self.assertEqual(corrections[0]["accounting"], "correction")
        self.assertEqual(D(self.oms.state["cash_krw"]), before_cash - 1)

    def test_terminal_order_leaves_active_state_after_settlement_window(self):
        order = self.submit_buy()
        self.client.fill(order["cid"], "100", "100", status="FILLED")
        self.oms.reconcile(force=True)
        self.clock.advance(self.config["settlement_reconcile_s"] + 0.01)
        self.oms.reconcile(force=True)
        self.assertNotIn(order["cid"], self.oms.state["orders"])
        self.assertEqual(self.oms.quantity("AAA"), 100)

    def test_missing_or_foreign_inventory_and_orders_halt_after_reconciliation(self):
        order = self.submit_buy()
        self.client.fill(order["cid"], "100", "100")
        self.oms.reconcile(force=True)
        self.oms.sync_account([dict(currency="KRW", available="90000", limit="0")], [], {"AAA": "0.1"})
        self.clock.advance(self.config["account_mismatch_grace_s"] + 1)
        self.oms.sync_account([dict(currency="KRW", available="90000", limit="0")], [], {"AAA": "0.1"})
        self.assertEqual(self.oms.state["halt"], "INVENTORY_SHORTFALL")

    def test_shared_account_owns_fixed_capital_and_ignores_reserved_track_c_symbol(self):
        config = copy.deepcopy(load())
        config.update(
            status="active", mode="live", execution_enabled=True,
            portfolio_isolation_required=False,
            portfolio_isolation_confirmed=False,
            shared_portfolio_approved=True,
            credential_profile="coinone_default",
            capital_allocation_krw=300000,
            cash_reserve_krw=20000,
            shared_reserved_symbols=["BTC"],
            universe=["ETH"],
        )
        client = Client()
        client.balance_rows = [
            dict(currency="KRW", available="320457", limit="0"),
            dict(currency="BTC", available="0.01", limit="0"),
        ]
        store = Store(Path(self.folder.name) / "shared")
        self.addCleanup(store.close)
        oms = OMS(config, client, store, clock=self.clock)
        foreign = [dict(
            user_order_id="tc-entry-12345678", order_id="c-order",
            target_currency="BTC",
        )]
        oms.sync_account(client.balances(), foreign, {"BTC": "0.00000001"})
        self.assertIsNone(oms.state["halt"])
        self.assertEqual(D(oms.state["cash_krw"]), D("300000"))
        self.assertEqual(oms.free_cash(), D("300000"))
        self.assertEqual(oms.state["foreign_order_coins"], ["BTC"])
        self.assertEqual(oms.state["foreign_assets"], ["BTC"])

    def test_available_inventory_is_bounded_by_owned_unreserved_quantity(self):
        order = self.submit_buy()
        self.client.fill(order["cid"], "100", "100")
        self.oms.reconcile(force=True)
        self.client.balance_rows = [
            dict(currency="KRW", available="90000", limit="0"),
            dict(currency="AAA", available="150", limit="0"),
        ]
        self.oms.sync_account(self.client.balances(), [], {"AAA": "0.1"})
        self.assertEqual(self.oms.available_asset("AAA"), D("100"))

    def test_reduction_keeps_campaign_average_while_lots_remain(self):
        first = self.submit_buy()
        self.client.fill(first["cid"], "100", "100")
        self.oms.reconcile(force=True)
        second = self.oms.submit(
            "AAA", "buy", "BUY", "LIMIT", "100", fee_rate="0", price="80"
        )
        self.client.fill(second["cid"], "100", "80")
        self.oms.reconcile(force=True)
        self.assertEqual(self.oms.book("AAA")["avg"], "90")
        sale = self.oms.submit("AAA", "trim", "SELL", "MARKET", "100", fee_rate="0")
        self.client.fill(sale["cid"], "100", "100")
        self.oms.reconcile(force=True)
        self.assertEqual(self.oms.quantity("AAA"), 100)
        self.assertEqual(self.oms.book("AAA")["avg"], "90")
        self.assertEqual(self.oms.state["realized"], "1000")

    def test_later_order_partial_fills_preserve_whole_book_average(self):
        first = self.oms.submit(
            "AAA", "buy", "BUY", "LIMIT", "10", fee_rate="0", price="100"
        )
        self.client.fill(first["cid"], "10", "100")
        self.oms.reconcile(force=True)
        second = self.oms.submit(
            "AAA", "buy", "BUY", "LIMIT", "4", fee_rate="0", price="90"
        )
        self.client.fill(second["cid"], "2", "90", status="PARTIALLY_FILLED")
        self.oms.reconcile(force=True)
        self.client.fill(second["cid"], "4", "90")
        self.oms.reconcile(force=True)
        expected = (D(10) * D(100) + D(4) * D(90)) / D(14)
        self.assertEqual(D(self.oms.book("AAA")["avg"]), expected)

    def test_failed_cancel_is_retried_after_bounded_interval(self):
        order = self.submit_buy()
        self.client.cancel_error = CoinoneError("temporary cancel failure")
        self.oms.cancel(order)
        self.assertEqual(self.client.cancellations, [order["cid"]])
        self.oms.cancel(order)
        self.assertEqual(self.client.cancellations, [order["cid"]])
        self.clock.advance(self.config["cancel_retry_s"] + 0.01)
        self.client.cancel_error = None
        self.oms.cancel(order)
        self.assertEqual(self.client.cancellations, [order["cid"], order["cid"]])
        self.assertEqual(order["status"], "CANCELED")

    def test_stop_flat_completion_uses_one_clock_type_and_sets_cooldown(self):
        buy = self.oms.submit(
            "AAA", "buy", "BUY", "LIMIT", "100", fee_rate="0", price="100"
        )
        self.client.fill(buy["cid"], "100", "100")
        self.oms.reconcile(force=True)
        sale = self.oms.submit(
            "AAA", "exit", "SELL", "MARKET", "100", fee_rate="0",
            reason="stop_limit_gap",
        )
        self.client.fill(sale["cid"], "100", "90")
        self.oms.reconcile(force=True)
        self.assertTrue(self.oms.finish_flat("AAA"))
        self.assertEqual(self.oms.state["day_stops"], 1)
        self.assertEqual(
            self.oms.book("AAA")["cooldown_until"],
            self.clock() + self.config["strategy"]["stop_cooldown_s"],
        )

    def test_daily_limit_uses_day_open_equity_not_purchase_cost(self):
        buy = self.oms.submit(
            "AAA", "buy", "BUY", "LIMIT", "100", fee_rate="0", price="80"
        )
        self.client.fill(buy["cid"], "100", "80")
        self.oms.reconcile(force=True)
        self.oms.mark("AAA", "100")
        self.clock.advance(86_400)
        self.oms.roll_day({"AAA": "100"})
        self.assertEqual(D(self.oms.state["day_start"]), D("102000"))
        self.assertTrue(self.oms.daily_blocked({"AAA": "79"}))

    def test_same_day_external_capital_is_not_counted_as_trading_pnl(self):
        self.client.balance_rows = [dict(currency="KRW", available="120000", limit="0")]
        self.oms.sync_account(self.client.balances(), [], {"AAA": "0.1"})
        self.assertEqual(self.oms.day_pnl(), 0)
        self.assertEqual(self.oms.state["day_external_flows"], "20000")

    def test_campaign_stop_floor_includes_prior_realized_loss_and_exit_fee(self):
        buy = self.oms.submit(
            "AAA", "buy", "BUY", "LIMIT", "10", fee_rate="0", price="100"
        )
        self.client.fill(buy["cid"], "10", "100")
        self.oms.reconcile(force=True)
        self.assertEqual(self.oms.book("AAA")["campaign_budget"], "50.0")
        sale = self.oms.submit(
            "AAA", "trim", "SELL", "MARKET", "2", fee_rate="0", reason="strategy_trim"
        )
        self.client.fill(sale["cid"], "2", "90")
        self.oms.reconcile(force=True)
        floor = self.oms.campaign_stop_floor("AAA", "0.001")
        book = self.oms.book("AAA")
        terminal = (
            D(book["campaign_realized"])
            + self.oms.quantity("AAA") * (floor - D(book["avg"]))
            - self.oms.quantity("AAA") * floor * D("0.001")
        )
        self.assertAlmostEqual(float(terminal), -float(book["campaign_budget"]), places=8)


if __name__ == "__main__":
    unittest.main()
