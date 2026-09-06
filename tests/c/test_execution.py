from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from track_c.execution.coinone import CoinoneError, CoinoneReadOnly, Credentials
from track_c.execution.client import CoinoneExecution
from track_c.market.metadata import Market
from track_c.execution.oms import OMS
from track_c.settings import load
from track_c.execution.sizing import size_order
from track_c.ops.store import Store

CONFIG = Path(__file__).parents[1] / "fixtures" / "coinone.json"


def cfg():
    c = load(CONFIG)
    c.update(mode="live", funding_confirmed=True)
    return c


class SizingTests(unittest.TestCase):
    def setUp(self):
        self.config = cfg()
        self.inputs = dict(market=dict(qty_unit="0.1", min_qty="0.1", min_order_amount="5000", max_qty="999999", max_order_amount="999999999"),
                           units=[dict(range_min="0", price_unit="1")],
                           book=dict(bids=[dict(price="1000", qty="10000"), dict(price="999", qty="10000")], asks=[dict(price="1001", qty="10000")]),
                           feature=dict(atr="10", sigma="0.0001", dip_low="999"), fees=dict(maker="0", taker="0"),
                           equity=D(300000), cash=D(300000), daily_remaining=D(4500), volume_10s=D(100000))

    def test_all_simultaneous_caps_are_honored_and_quantity_is_floored(self):
        r = size_order(self.config, **self.inputs)
        self.assertIsNone(r["reason"])
        q = D(r["qty"])
        self.assertTrue(all(q <= D(v) for v in r["caps"].values()))
        self.assertLessEqual(D(r["nominal_loss_krw"]), D(750))
        self.assertLessEqual(D(r["notional_krw"]), D(285000))
        self.assertEqual(q % D("0.1"), 0)

    def test_cash_risk_and_liquidity_can_each_determine_size(self):
        reference = size_order(self.config, **self.inputs)
        self.assertEqual(reference["binding"], "risk")
        self.inputs["cash"] = D(6000)
        self.assertEqual(size_order(self.config, **self.inputs)["binding"], "cash")
        self.inputs["cash"] = D(300000)
        self.inputs["book"]["asks"][0]["qty"] = "60"
        self.assertEqual(size_order(self.config, **self.inputs)["binding"], "depth")
        self.inputs["book"]["asks"][0]["qty"] = "10000"
        self.inputs["volume_10s"] = D(30)
        self.assertEqual(size_order(self.config, **self.inputs)["binding"], "volume")

    def test_less_capital_or_wider_stop_does_not_increase_quantity(self):
        base = D(size_order(self.config, **self.inputs)["qty"])
        self.inputs["equity"] = D(150000)
        smaller = D(size_order(self.config, **self.inputs)["qty"])
        self.assertLessEqual(smaller, base/2)
        self.inputs["feature"]["dip_low"] = "950"
        wider = size_order(self.config, **self.inputs)
        self.assertTrue(wider["reason"] or D(wider["qty"]) < smaller)

    def test_minimum_does_not_override_insufficient_risk_or_flow(self):
        self.inputs["daily_remaining"] = D(1)
        self.assertEqual(size_order(self.config, **self.inputs)["reason"], "minimum_order_exceeds_size")
        self.inputs["daily_remaining"] = D(4500)
        self.inputs["volume_10s"] = D(0)
        self.assertEqual(size_order(self.config, **self.inputs)["reason"], "minimum_order_exceeds_size")

    def test_fees_can_remove_an_apparently_cheap_opportunity(self):
        self.inputs["fees"] = dict(maker="0.01", taker="0.01")
        self.assertEqual(size_order(self.config, **self.inputs)["reason"], "cost_exceeds_movement")


class FakeExchange:
    def __init__(self):
        self.rows, self.submissions, self.cancels = {}, [], []
        self.fail = None
        self.cancel_race = False
        self.inventory = D(0)

    def submit(self, order, *, before_send=None):
        if before_send is not None: before_send()
        self.submissions.append(dict(order))
        if self.fail == "reject":
            raise CoinoneError("rejected", code=103)
        self.rows[order["cid"]] = dict(order_id=order["cid"], quote_currency="KRW", target_currency=order["coin"],
                                      side=order["side"], status="NOT_TRIGGERED" if order["role"] == "protect" else "LIVE",
                                      executed_qty="0", average_executed_price="0", fee="0", remain_qty=order["qty"])
        if order["role"] == "exit":
            self.fill(order["cid"], order["qty"], "1001", "FILLED")
        if self.fail == "lost_response":
            self.fail = None
            raise CoinoneError("network failure")
        return dict(order_id=order["cid"])

    def fill(self, cid, qty, px, status="FILLED"):
        r = self.rows[cid]
        delta = D(qty)-D(r["executed_qty"])
        self.inventory += delta if r["side"] == "BUY" else -delta
        original = next(o["qty"] for o in self.submissions if o["cid"] == cid)
        r.update(executed_qty=qty, average_executed_price=px, status=status,
                 remain_qty="0" if status in ("FILLED", "CANCELED") else str(D(original)-D(qty)))

    def detail(self, coin, cid):
        if cid not in self.rows:
            raise CoinoneError("missing", code=104)
        return deepcopy(self.rows[cid])

    def cancel(self, coin, cid):
        self.cancels.append(cid)
        if cid not in self.rows:
            raise CoinoneError("missing", code=104)
        if self.cancel_race and self.rows[cid]["side"] == "SELL":
            self.fill(cid, "40", "999", "CANCELED")
            self.cancel_race = False
        self.rows[cid].update(status="CANCELED", remain_qty="0")
        return dict(result="success")

    def balances(self):
        return [dict(currency="BTC", available=str(self.inventory), limit="0")]


class OMSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(lambda: self.store.close())
        self.client = FakeExchange()
        self.now = [1788595000.0]
        self.config = cfg()
        self.oms = OMS(self.config, self.client, self.store, clock=lambda:self.now[0])
        self.oms.sync_cash(D(300000))
        self.plan = dict(reason=None, qty="100", entry="1000", stop="999", stop_limit="998", nominal_loss_krw="200", maker="0", taker="0")

    def enter(self):
        self.assertTrue(self.oms.enter("BTC", self.plan, {}, "5000"))
        return self.client.submissions[-1]["cid"]

    def held(self):
        cid = self.enter()
        self.client.fill(cid, "100", "1000")
        self.oms.drive(bid=1001, fresh=True)
        return self.oms.active("protect")[0]["cid"]

    def test_funding_and_mode_gates_block_real_requests(self):
        for key, value in (("mode", "observe"), ("funding_confirmed", False)):
            prior = self.config[key]
            self.config[key] = value
            self.assertFalse(self.oms.enter("BTC", self.plan, {}, "5000"))
            self.config[key] = prior
        self.assertEqual(self.client.submissions, [])

    def test_actual_entire_balance_and_external_flows_are_not_profit(self):
        self.oms.sync_cash(D('522751.8273'))
        self.assertEqual(self.oms.equity, D('522751.8273'))
        self.assertEqual(D(self.oms.state['realized']), 0)
        self.assertEqual(D(self.oms.state['external_flows']), D('222751.8273'))
        self.oms.sync_cash(D('400000'))
        self.assertEqual(self.oms.equity, D('400000'))
        self.assertEqual(D(self.oms.state['external_flows']), D('100000'))
        self.assertEqual(D(self.oms.state['day_realized']), 0)

    def test_compounding_counts_realized_profit_once_and_preserves_reserved_cash(self):
        cid = self.enter()
        self.assertEqual(self.oms.equity, D(300000))
        self.assertFalse(self.oms.sync_cash(D(200000)))
        self.client.fill(cid, '100', '1000')
        self.oms.drive(bid=1001, fresh=True)
        self.assertEqual(D(self.oms.state['cash_krw']), D(200000))
        self.assertEqual(self.oms.equity, D(300100))
        self.oms.drive(bid=1001, fresh=True, opposite=True)
        self.oms.drive(bid=1001, fresh=True)
        self.assertIsNone(self.oms.campaign)
        self.assertEqual(D(self.oms.state['realized']), D(100))
        self.oms.sync_cash(D(300100))
        self.assertEqual(self.oms.equity, D(300100))
        self.assertEqual(D(self.oms.state['external_flows']), 0)
        restarted = OMS(self.config, self.client, self.store, clock=lambda:self.now[0])
        self.assertEqual(restarted.equity, D(300100))

    def test_deposit_does_not_reset_daily_loss_or_halt(self):
        self.oms.state['day_realized'] = '-4000'
        self.oms.halt('DAILY_LOSS')
        self.oms.sync_cash(D(600000))
        self.assertEqual(self.oms.state['day_realized'], '-4000')
        self.assertEqual(self.oms.state['halt'], 'DAILY_LOSS')
        self.assertEqual(self.oms.remaining_risk(), D(5000))

    def test_balance_must_be_rechecked_before_a_later_entry(self):
        self.now[0] += 6
        self.assertFalse(self.oms.enter('BTC', self.plan, {}, '5000'))
        self.oms.sync_cash(D(500000))
        self.assertTrue(self.oms.enter('BTC', self.plan, {}, '5000'))

    def test_oms_rechecks_budget_instead_of_trusting_a_forged_size_plan(self):
        self.plan["qty"] = "10000"
        self.assertFalse(self.oms.enter("BTC", self.plan, {}, "5000"))
        self.assertEqual(self.client.submissions, [])

    def test_lost_response_is_recovered_by_identifier_without_duplicate_submission(self):
        self.client.fail = "lost_response"
        cid = self.enter()
        self.assertEqual(self.oms.active()[0]["status"], "UNKNOWN")
        self.oms = OMS(self.config, self.client, self.store, clock=lambda:self.now[0])
        self.oms.drive(bid=1001, fresh=True)
        self.assertEqual(len(self.client.submissions), 1)
        self.assertEqual(self.oms.active()[0]["cid"], cid)

    def test_an_unresolved_not_found_never_releases_the_capital_slot(self):
        cid = self.enter()
        del self.client.rows[cid]
        self.now[0] += 31
        self.oms.drive(bid=1001, fresh=True)
        self.assertTrue(self.oms.active("entry"))
        self.assertEqual(self.oms.state["halt"], "ORDER_RECONCILIATION")
        self.assertEqual(len(self.client.submissions), 1)

    def test_cumulative_partial_fill_is_counted_once(self):
        cid = self.enter()
        self.client.fill(cid, "40", "1000", "PARTIALLY_FILLED")
        order = self.oms.state["orders"][cid]
        self.oms.apply(order, self.client.detail("BTC", cid))
        self.oms.apply(order, self.client.detail("BTC", cid))
        self.assertEqual(D(self.oms.campaign["qty"]), 40)
        self.client.fill(cid, "100", "1000")
        self.oms.drive(bid=1001, fresh=True)
        self.assertEqual(D(self.oms.campaign["qty"]), 100)
        self.assertEqual(len(self.oms.active("protect")), 1)

    def test_stop_fill_prevents_a_duplicate_market_sale(self):
        cid = self.held()
        self.client.fill(cid, "100", "999")
        self.oms.drive(bid=998, fresh=True)
        self.assertIsNone(self.oms.campaign)
        self.assertEqual(D(self.oms.state["realized"]), -100)
        self.assertFalse(any(o["role"] == "exit" for o in self.client.submissions))

    def test_racing_stop_fill_is_subtracted_before_market_exit(self):
        self.held()
        self.client.cancel_race = True
        self.oms.drive(bid=1001, fresh=True, opposite=True)
        sale = next(o for o in self.client.submissions if o["role"] == "exit")
        self.assertEqual(D(sale["qty"]), 60)
        self.oms.drive(bid=1001, fresh=True)
        self.assertIsNone(self.oms.campaign)
        self.assertEqual(D(self.oms.state["realized"]), 20)

    def test_cancel_ack_alone_does_not_allow_selling_reserved_quantity(self):
        self.held()
        self.client.cancel = lambda *args: dict(result="success")
        self.oms.drive(bid=1001, fresh=True, opposite=True)
        self.assertFalse(any(o["role"] == "exit" for o in self.client.submissions))

    def test_tiny_partial_is_retained_and_halted_after_ttl(self):
        cid = self.enter()
        self.client.fill(cid, "1", "1000", "PARTIALLY_FILLED")
        self.now[0] += 5
        self.oms.drive(bid=1000, fresh=True)
        self.assertEqual(D(self.oms.campaign["qty"]), 1)
        self.assertEqual(self.oms.state["halt"], "UNTRADEABLE_PARTIAL")
        self.assertFalse(self.oms.active("entry"))

    def test_time_exit_uses_first_fill_time(self):
        self.held()
        self.now[0] += 33
        self.oms.drive(bid=1001, fresh=True)
        self.assertEqual(self.oms.campaign["exit_reason"], "time")
        self.assertTrue(any(o["role"] == "exit" for o in self.client.submissions))

    def test_filled_order_requires_price_and_no_terminal_remainder(self):
        cid = self.enter()
        row = self.client.detail("BTC", cid)
        row.update(executed_qty="10", status="FILLED", remain_qty="0")
        with self.assertRaises(CoinoneError):
            self.oms.apply(self.oms.active()[0], row)
        row.update(average_executed_price="1000", remain_qty="90")
        with self.assertRaises(CoinoneError):
            self.oms.apply(self.oms.active()[0], row)
        self.assertEqual(D(self.oms.campaign["qty"]), 0)

    def test_new_day_preserves_campaign_and_only_resets_daily_halt(self):
        self.held()
        self.oms.halt("DAILY_LOSS")
        self.now[0] += 86400
        self.oms.roll_day()
        self.assertIsNone(self.oms.state["halt"])
        self.assertEqual(D(self.oms.campaign["qty"]), 100)
        self.oms.halt("ORDER_RECONCILIATION")
        self.now[0] += 86400
        self.oms.roll_day()
        self.assertEqual(self.oms.state["halt"], "ORDER_RECONCILIATION")


class InfrastructureTests(unittest.TestCase):
    def test_actual_lowercase_tickers_join_uppercase_market_contracts(self):
        def transport(request, timeout):
            if '/markets/' in request.full_url:
                return dict(markets=[dict(quote_currency='KRW', target_currency='SOL')])
            return dict(tickers=[dict(quote_currency='krw', target_currency='sol', quote_volume='123456789')])
        contracts, tickers = CoinoneReadOnly(transport=transport).universe()
        self.assertEqual(contracts[0]['target_currency'], tickers[0]['target_currency'])
        self.assertEqual(tickers[0]['quote_volume'], '123456789')
        from track_c.execution.coinone import symbol
        self.assertEqual(symbol('W'), 'W')


    def test_second_writer_is_refused_and_release_allows_restart(self):
        with tempfile.TemporaryDirectory() as d:
            first = Store(d)
            with self.assertRaisesRegex(RuntimeError, "another Track C writer"):
                Store(d)
            first.close()
            second = Store(d)
            second.close()


    def test_live_configuration_requires_funding_binding(self):
        c = json.loads(CONFIG.read_text())
        c["mode"] = "live"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"config.json"
            p.write_text(json.dumps(c))
            with self.assertRaises(ValueError):
                load(p)

    def test_execution_identifiers_cannot_address_foreign_orders(self):
        calls = []
        client = CoinoneExecution(Credentials("a", "s"), transport=lambda r,t:calls.append(r))
        with self.assertRaises(CoinoneError):
            client.cancel("BTC", "s1-existing-order")
        with self.assertRaises(CoinoneError):
            client.submit(dict(cid="tc-exit-12345678", coin="BTC", side="BUY", type="MARKET", qty="1"))
        self.assertEqual(calls, [])

    def test_trade_dedupe_sides_and_book_age_are_independent(self):
        c = cfg()
        m = Market("BTC", c, {}, [], {}, [])
        book = dict(quote_currency="KRW", target_currency="BTC", timestamp=1000, id="1", bids=[dict(price="100", qty="10")], asks=[dict(price="101", qty="10")])
        m.feed("ORDERBOOK", book, 1000)
        trade = dict(quote_currency="KRW", target_currency="BTC", timestamp=2000, id="2", price="101", qty="3", is_seller_maker=True)
        m.feed("TRADE", trade, 2000)
        m.feed("TRADE", trade, 2000)
        self.assertEqual(m.volume(2000), (D(3), 1))
        self.assertEqual(m.features.b, 3)
        self.assertFalse(m.fresh(3000))
        stale = dict(book, id="3")
        m.feed("ORDERBOOK", stale, 4000)
        self.assertFalse(m.fresh(4000))


if __name__ == "__main__":
    unittest.main()
