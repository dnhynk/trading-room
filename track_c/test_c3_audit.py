"""Accounting conservation, frozen risk and execution regressions from the C3 audit."""
from decimal import Decimal as D
import math
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from . import rule
from .fair import FairValue
from .leaders import parse
from . import test_c3 as fixture
from .test_c3 import rule_cfg, UNITS, CONTRACT


class AuditOMSTests(unittest.TestCase):
    setUp = fixture.RestingFlowTests.setUp
    drive = fixture.RestingFlowTests.drive
    entry_cid = fixture.RestingFlowTests.entry_cid
    events = fixture.RestingFlowTests.events

    def test_sellable_partial_enters_exit_queue_before_entry_ttl(self):
        self.pf.enter('BTC', self.plan, {}, '5000')
        self.client.fill(self.entry_cid(), '6', '990', 'PARTIALLY_FILLED')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        self.assertIn(self.entry_cid(), self.client.cancels)
        take = [o for o in self.client.submissions if o['role'] == 'take']
        self.assertEqual(len(take), 1)
        self.assertEqual(D(take[0]['qty']), D(6))

    def test_dust_transfer_preserves_equity_and_loss(self):
        self.pf.enter('BTC', self.plan, {}, '5000')
        self.client.fill(self.entry_cid(), '2', '990', 'PARTIALLY_FILLED')
        self.now[0] += 61
        self.drive(bid=989.0, quantitative_decision=dict(hold=False, reason='time', cancel_entry=True))
        self.assertEqual(self.pf.campaigns, {})
        self.assertEqual(self.pf.equity, D(300000) - 2)
        self.assertEqual(D(self.pf.state['realized']), 0)
        self.assertEqual(self.pf.daily_remaining(), self.pf.equity * D(self.cfg['daily_loss_fraction']) - 2)
        # Unprotected dust consumes risk, but can be merged into a sellable order.
        self.assertEqual(self.pf.remaining_risk(), 0)
        self.pf.state['capital_at'] = self.now[0]
        self.assertTrue(self.pf.enter('BTC', self.plan, {}, '5000'))
        self.assertEqual(self.pf.equity, D(300000) - 2)

    def test_cancel_race_to_dust_does_not_submit_below_minimum_market_order(self):
        self.pf.enter('BTC', self.plan, {}, '5000')
        self.client.fill(self.entry_cid(), self.plan['qty'], '990')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        original = self.client.cancel
        def race(coin, cid):
            order = next(o for o in self.client.submissions if o['cid'] == cid)
            if order['role'] == 'take':
                self.client.fill(cid, str(D(order['qty'])-1), '991', 'PARTIALLY_FILLED')
            return original(coin, cid)
        self.client.cancel = race
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=False, reason='defend', cancel_entry=True))
        self.assertFalse([o for o in self.client.submissions if o['role'] == 'exit'])
        self.assertEqual(D(self.pf.state['residuals']['BTC']['qty']), 1)


class AuditMathTests(unittest.TestCase):
    def assess(self, **over):
        args = dict(coin='BTC', bid=1000., ask=1005., tick=5., dev=0., contract=CONTRACT,
                    units=UNITS, cash=D(300000), risk_remaining=D(1000))
        args.update(over)
        return rule.assess(rule_cfg(), **args)

    def test_downward_stop_walks_valid_ladder(self):
        plan = self.assess()['plan']
        self.assertEqual((D(plan['stop']), D(plan['stop_limit'])), (D(997), D(996)))

    def test_nonfinite_signal_cannot_enter(self):
        for x in (math.nan, math.inf, -math.inf):
            self.assertFalse(self.assess(dev=x)['accepted'])

    def test_exchange_max_notional_is_enforced(self):
        self.assertFalse(self.assess(contract=dict(CONTRACT, max_order_amount='9000'))['accepted'])

    def test_nonfinite_leader_size_rejected(self):
        import json
        msg = dict(type='orderbook', code='KRW-BTC', timestamp=1000,
                   orderbook_units=[dict(bid_price=99, ask_price=101, bid_size=math.nan, ask_size=1)])
        self.assertIsNone(parse('upbit', 1000, json.dumps(msg)))

    def test_old_leader_quote_cannot_replace_latest(self):
        f = FairValue(min_samples=1)
        f.leader_quote('U', 1000, 99, 101)
        f.evaluate(1000, 100, 1)
        f.leader_quote('U', 2000, 101, 103)
        f.leader_quote('U', 1500, 79, 81)
        self.assertAlmostEqual(f.evaluate(2000, 100, 1)['fair'], 102)

    def test_known_disconnect_invalidates_reference_until_new_quote(self):
        f = FairValue(min_samples=1)
        f.leader_quote('U', 1000, 99, 101)
        f.evaluate(1000, 100, 1)
        self.assertIsNotNone(f.evaluate(2000, 100, 1))
        f.disconnect('U')
        self.assertIsNone(f.evaluate(2100, 100, 1))
        f.leader_quote('U', 2200, 99, 101)
        self.assertIsNotNone(f.evaluate(3000, 100, 1))

    def test_fixed_stop_does_not_move_when_tick_band_changes(self):
        self.assertTrue(rule.hold(rule_cfg(), dev=1, bid=998, entry=1000, tick=5, stop=997, age_s=1)['hold'])
        self.assertEqual(rule.hold(rule_cfg(), dev=1, bid=997, entry=1000, tick=1, stop=997, age_s=1)['reason'], 'stop')

    def test_four_of_five_days_is_not_ninety_percent_sign_evidence(self):
        from .c3_evidence import sign_tail
        self.assertEqual(sign_tail(4, 5), .1875)

    def test_uncompleted_evaluation_window_cannot_promote(self):
        from .c3_evidence import assess
        from copy import deepcopy
        prototype = dict(schema=2, rule='test', identity=dict(source={}, config={}, data={}), scenario={},
                         start=0, end=100, accounting_error_krw=0, daily_wealth={})
        reports = [dict(deepcopy(prototype), control=c) for c in ('none','flip','unconditional')]
        reports[2]['identity']['config'] = dict(entry_ticks=-1e9, cancel_ticks=-1e9, defend_ticks=-1e9)
        protocol = dict(source={}, config={}, scenario={}, rule='test', start_kst='2026-09-06', days=10)
        result = assess(protocol, *reports)
        self.assertEqual(result['verdict'], 'HOLD')
        self.assertIn('fixed_prospective_window_incomplete', result['issues'])


class RunnerAuditTests(unittest.IsolatedAsyncioTestCase):
    def runner(self):
        from .c3_runner import RuleRunner
        r = object.__new__(RuleRunner)
        r.cfg = rule_cfg()
        r.account_at = 99.
        r.refresh_account = AsyncMock()
        r.fair_for = lambda *args: None
        r.last_fair = {}
        r.snapshot = lambda *args: None
        return r

    async def test_recent_account_snapshot_skips_redundant_rest_roundtrips(self):
        r = self.runner()
        with patch('track_c.c3_runner.time.time', return_value=100.):
            await r.submit_candidate('BTC')
        r.refresh_account.assert_not_awaited()
        r.account_at = 90.
        with patch('track_c.c3_runner.time.time', return_value=100.):
            await r.submit_candidate('BTC')
        r.refresh_account.assert_awaited_once()

    async def test_connection_break_disables_recent_coinone_book(self):
        r = self.runner()
        r.markets = dict(BTC=SimpleNamespace(micro=SimpleNamespace(bids=[(990,1)], asks=[(991,1)], book_ms=1000), units=UNITS))
        r.connected = False
        self.assertIsNone(r.live_book('BTC', 1100))
        r.connected = True
        self.assertEqual(r.live_book('BTC', 1100)['bid'], 990)

    async def test_defend_is_ready_when_reconcile_discovers_first_fill(self):
        r = self.runner()
        r.last_fair = dict(BTC=dict(dev_ticks=-1))
        c = dict(coin='BTC', first_fill=None, stop='987', plan=dict(entry='990', tick='1'))
        self.assertEqual(r.holding_decision(c, dict(bid=990, tick=1))['reason'], 'defend')


class QueueAuditTests(unittest.TestCase):
    def test_better_levels_do_not_create_phantom_same_price_queue(self):
        from .simulation import Exchange
        from .outcomes import Path
        events = [dict(t=1000, kind='book', bids=[(990, 10)], asks=[(991, 100), (992, 2)]),
                  dict(t=1200, kind='trade', price=992, qty=3, buy=True)]
        ex = Exchange({'BTC': Path(events)}, lambda: 1., latency=0)
        ex.inventory['BTC'] = D(1)
        ex.submit(dict(cid='tc-take-test', coin='BTC', role='take', side='SELL', type='LIMIT', qty='1', price='992'))
        ex.event('BTC', events[-1])
        self.assertEqual(ex.orders['tc-take-test']['executed_qty'], '1')


if __name__ == '__main__':
    unittest.main()
