import base64
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
        self.oms.set_strategy_params("AAA", dict(max_notional=50000, qstep=0.1))

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

    def test_fee_above_declared_ceiling_halts_accounting(self):
        order = self.submit_buy()
        self.client.fill(order["cid"], "100", "100", fee="100")
        self.assertEqual(self.oms.reconcile(force=True), [])
        self.assertEqual(self.oms.state["halt"], "FEE_MISMATCH")

    def test_missing_or_foreign_inventory_and_orders_halt_after_reconciliation(self):
        order = self.submit_buy()
        self.client.fill(order["cid"], "100", "100")
        self.oms.reconcile(force=True)
        self.oms.sync_account([dict(currency="KRW", available="90000", limit="0")], [], {"AAA": "0.1"})
        self.clock.advance(self.config["account_mismatch_grace_s"] + 1)
        self.oms.sync_account([dict(currency="KRW", available="90000", limit="0")], [], {"AAA": "0.1"})
        self.assertEqual(self.oms.state["halt"], "INVENTORY_SHORTFALL")

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


if __name__ == "__main__":
    unittest.main()
