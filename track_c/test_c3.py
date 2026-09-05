"""C3 fair value, rule policy, resting-sale OMS flow, residual carry-over and replay exchange."""
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from .fair import FairValue, microprice
from .oms import OMS
from .portfolio import Portfolio
from . import rule
from .settings import load
from .simulation import Exchange, MemoryStore
from .store import Store
from .test_runtime import FakeExchange, cfg as base_cfg

UNITS = [dict(range_min='0', price_unit='1'), dict(range_min='1000', price_unit='5')]
CONTRACT = dict(qty_unit='0.01', min_order_amount='5000', max_qty='1000000', max_order_amount='100000000')


def rule_cfg(**over):
    c = base_cfg()
    c.update(policy='rule', coins=['BTC'], notional_krw=10000, entry_ticks=0.0, cancel_ticks=-0.5, defend_ticks=-0.5, stop_ticks=3, hold_s=180,
             target_ticks=1, max_spread_ticks=2, entry_ttl_s=60, ratio_window_s=300, ratio_min_samples=60, leader_max_age_ms=30000,
             decision_ms=500, liveness_ms=60000, mode='live', funding_confirmed=True, capital_mode='account_equity',
             momentum_window_s=30, momentum_recent_s=10, momentum_veto_ticks=1, momentum_decel_share=.3333, flow_gate=True)
    c.update(over)
    return c


class FairValueTests(unittest.TestCase):
    def test_median_uses_past_samples_only_and_needs_history(self):
        f = FairValue(window_s=300, min_samples=3)
        f.leader_quote('U', 0, 99, 101)
        self.assertIsNone(f.evaluate(1000, 100, 1))   # 1 sample
        self.assertIsNone(f.evaluate(2000, 100, 1))   # 2
        self.assertIsNone(f.evaluate(3000, 100, 1))   # median needs 3 past samples before this one
        out = f.evaluate(4000, 100, 1)
        self.assertAlmostEqual(out['fair'], 100.0)
        self.assertAlmostEqual(out['dev_ticks'], 0.0)
        # A sudden leader move shows up as a deviation; the constant (median) lags.
        f.leader_quote('U', 4500, 101, 103)
        out = f.evaluate(5000, 100, 1)
        self.assertAlmostEqual(out['dev_ticks'], 2.0)
        # Same second: no extra sample is taken.
        f.evaluate(5400, 100, 1)
        self.assertEqual(len(f.leaders['U'].ratios), 5)

    def test_stale_leader_and_flip_and_weights(self):
        f = FairValue(window_s=300, min_samples=2, leader_max_age_ms=1000, weights={'U': 3, 'B': 1})
        for t in range(0, 4000, 1000):
            f.leader_quote('U', t, 99, 101); f.leader_quote('B', t, 99, 101)
            f.evaluate(t, 100, 1)
        f.leader_quote('U', 4000, 103, 105); f.leader_quote('B', 4000, 99, 101)
        out = f.evaluate(4000, 100, 1)
        self.assertAlmostEqual(out['dev_ticks'], 3.0)  # (3*104 + 1*100)/4 - 100
        self.assertEqual(out['leaders'], 2)
        self.assertIsNone(f.evaluate(6000, 100, 1))    # both leaders older than 1 s
        g = FairValue(window_s=300, min_samples=2, flip=True)
        for t in range(0, 3000, 1000):
            g.leader_quote('U', t, 99, 101); g.evaluate(t, 100, 1)
        g.leader_quote('U', 3000, 101, 103)
        self.assertAlmostEqual(g.evaluate(3000, 100, 1)['dev_ticks'], -2.0)

    def test_microprice(self):
        self.assertAlmostEqual(microprice(100, 102, 1, 3), 100.5)
        self.assertAlmostEqual(microprice(100, 102), 101)
        self.assertAlmostEqual(microprice(100, 102, 0, 0), 101)

    def moving_reference(self):
        f = FairValue(min_samples=1)
        for t in range(32):
            f.leader_quote('U', t*1000, 99+t, 101+t)
            result = f.evaluate(t*1000, 100+t, 2)
        return f, result

    def test_momentum_is_in_ticks_with_thirty_second_history(self):
        f, result = self.moving_reference()
        self.assertEqual((result['m30'], result['m10']), (15., 5.))
        count = len(f.history)
        f.evaluate(31500, 131, 2)
        self.assertEqual(len(f.history), count)
        self.assertEqual(f.evaluate(31500, 131, 1)['m30'], 30.)

    def test_momentum_resets_on_contributing_venue_change_and_disconnect(self):
        f, _ = self.moving_reference()
        for t in (32000, 33000):
            f.leader_quote('B', t, 199, 201)
            result = f.evaluate(t, 131, 2)
        self.assertEqual(result['leaders'], 2)
        self.assertIsNone(result['m30']); self.assertIsNone(result['m10'])
        f.disconnect('U'); f.leader_quote('U', 33500, 130, 132)
        self.assertIsNone(f.evaluate(33500, 131, 2)['m30'])

    def test_no_momentum_across_gap_or_from_a_later_reference_sample(self):
        f, _ = self.moving_reference()
        self.assertIsNone(f.evaluate(34000, 131, 2)['m30'])
        g = FairValue(min_samples=1)
        # Irregular timestamps: F(30,100-30,000) must not use the sample at 900ms.
        g.momentum(900, 100, 1, ('U',))
        for t in range(1,31):
            result = g.momentum(t*1000+100, 100+t, 1, ('U',))
        self.assertIsNone(result['m30'])
        self.assertEqual(g.momentum(30900, 130, 1, ('U',))['m30'], 30)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = rule_cfg()
        self.kw = dict(coin='BTC', bid=990.0, ask=991.0, tick=1.0, contract=CONTRACT, units=UNITS, cash=D(500000), risk_remaining=D(1000),
                       flow32=0., m30=0., m10=0.)

    def test_refusals(self):
        self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=None))['reason'], 'fair_unavailable')
        self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=-0.1))['reason'], 'rich_vs_fair')
        self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=1.0, ask=993.0))['reason'], 'wide_spread')
        self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=1.0, cash=D(1000)))['reason'], 'cash')
        self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=1.0, risk_remaining=D(1)))['reason'], 'risk_budget')

    def test_plan_prices_follow_ladder_and_size(self):
        r = rule.assess(self.cfg, **dict(self.kw, dev=0.5))
        self.assertTrue(r['accepted'])
        p = r['plan']
        self.assertEqual((p['entry'], p['take_profit'], p['stop'], p['stop_limit']), ('990.0', '991', '987', '986'))
        self.assertEqual(p['take_mode'], 'resting')
        self.assertGreaterEqual(D(p['qty']) * D(p['entry']), D(10000))
        self.assertLess(D(p['qty']) * D(p['entry']), D(10000) + D('990'))
        # Ladder boundary: entry 999 -> take at 1000 (unit 5 above 1000 does not apply below it)
        r = rule.assess(self.cfg, **dict(self.kw, dev=0.5, bid=999.0, ask=1000.0))
        self.assertEqual(r['plan']['take_profit'], '1000')
        r = rule.assess(self.cfg, **dict(self.kw, dev=0.5, bid=1000.0, ask=1005.0, tick=5.0))
        self.assertEqual((r['plan']['take_profit'], r['plan']['stop']), ('1005', '997'))

    def test_hold_and_cancel(self):
        c = self.cfg
        self.assertEqual(rule.hold(c, dev=0.2, bid=990, entry=990, tick=1, age_s=10)['hold'], True)
        self.assertEqual(rule.hold(c, dev=-0.6, bid=990, entry=990, tick=1, age_s=10)['reason'], 'defend')
        self.assertEqual(rule.hold(c, dev=0.5, bid=987, entry=990, tick=1, age_s=10)['reason'], 'stop')
        self.assertEqual(rule.hold(c, dev=0.5, bid=990, entry=990, tick=1, age_s=180)['reason'], 'time')
        self.assertEqual(rule.hold(c, dev=None, bid=989, entry=990, tick=1, age_s=10)['hold'], True)
        self.assertTrue(rule.cancel_entry(c, dev=None))
        self.assertTrue(rule.cancel_entry(c, dev=-0.6))
        self.assertFalse(rule.cancel_entry(c, dev=-0.4, m30=0, m10=0))

    def test_fair_must_reach_take_but_entry_floor_is_preserved(self):
        self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=.499))['reason'], 'fair_below_take')
        self.assertTrue(rule.assess(self.cfg, **dict(self.kw, dev=.5))['accepted'])
        self.assertTrue(rule.assess(self.cfg, **dict(self.kw, dev=0, ask=992))['accepted'])
        self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=-.01, ask=992))['reason'], 'rich_vs_fair')
        # At a price-band boundary, nominal k*tick cannot understate the real take.
        self.assertEqual(rule.assess(rule_cfg(target_ticks=2), **dict(self.kw, dev=2, bid=999, ask=1000))['reason'], 'fair_below_take')

    def test_falling_veto_and_deceleration_exception(self):
        falling = dict(self.kw, dev=2., m30=-3., m10=-1.)
        self.assertEqual(rule.assess(self.cfg, **falling)['reason'], 'leader_falling')
        self.assertTrue(rule.assess(self.cfg, **dict(falling, m10=-.5))['accepted'])
        self.assertTrue(rule.assess(self.cfg, **dict(falling, m30=-.99))['accepted'])
        self.assertTrue(rule.leader_falling(self.cfg, -1, -.3333))
        self.assertFalse(rule.leader_falling(self.cfg, -1, -.3332))
        self.assertTrue(rule.cancel_entry(self.cfg, dev=2, m30=-3, m10=-1))
        self.assertFalse(rule.cancel_entry(self.cfg, dev=2, m30=-3, m10=-.5))

    def test_missing_momentum_refuses_entry_and_cancels_pending(self):
        for missing in (None, float('nan'), float('inf')):
            self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=1, m30=missing))['reason'], 'momentum_unavailable')
            self.assertTrue(rule.cancel_entry(self.cfg, dev=1, m30=missing, m10=0))

    def test_brake_keeps_existing_stop_defend_time_protection(self):
        kw = dict(dev=2, bid=990, entry=990, tick=1, stop=987, age_s=10, m30=-3, m10=-1)
        self.assertEqual(rule.hold(self.cfg, **kw)['reason'], 'brake')
        self.assertTrue(rule.hold(self.cfg, **dict(kw, m10=-.5))['hold'])
        self.assertEqual(rule.hold(self.cfg, **dict(kw, bid=987))['reason'], 'stop')
        self.assertEqual(rule.hold(self.cfg, **dict(kw, m10=-.5, dev=-.6))['reason'], 'defend')
        self.assertEqual(rule.hold(self.cfg, **dict(kw, m10=-.5, age_s=180))['reason'], 'time')
        self.assertTrue(rule.hold(self.cfg, **dict(kw, m30=None))['hold'])

    def test_flow_sign_and_missing_flow(self):
        for flow in (-1., -.1, 0.):
            self.assertTrue(rule.assess(self.cfg, **dict(self.kw, dev=1, flow32=flow))['accepted'])
        self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=1, flow32=.0001))['reason'], 'buy_dominant_flow')
        for flow in (None, float('nan'), 2.):
            self.assertEqual(rule.assess(self.cfg, **dict(self.kw, dev=1, flow32=flow))['reason'], 'flow_unavailable')
        self.assertTrue(rule.assess(rule_cfg(flow_gate=False), **dict(self.kw, dev=1, flow32=.5))['accepted'])

    def test_unconditional_counterfactual_removes_all_three_new_gates(self):
        c = rule_cfg(entry_ticks=-1e9, cancel_ticks=-1e9, defend_ticks=-1e9)
        self.assertTrue(rule.assess(c, **dict(self.kw, dev=-2, m30=-3, m10=-1, flow32=1), unconditional=True)['accepted'])
        self.assertFalse(rule.cancel_entry(c, dev=-2, m30=-3, m10=-1, unconditional=True))
        self.assertTrue(rule.hold(c, dev=-2, bid=990, entry=990, tick=1, age_s=10, m30=-3, m10=-1, unconditional=True)['hold'])


class RestingExchange(FakeExchange):
    def __init__(self):
        super().__init__()
        self.balance_coin = 'BTC'

    def balances(self):
        return [dict(currency=self.balance_coin, available=str(self.inventory), limit='0')]


class RestingFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name); self.addCleanup(lambda: self.store.close())
        self.client = RestingExchange()
        self.now = [1788595000.0]
        self.cfg = rule_cfg()
        self.pf = Portfolio(self.cfg, self.client, self.store, clock=lambda: self.now[0])
        self.pf.sync_cash(D(300000))
        self.plan = rule.assess(self.cfg, coin='BTC', bid=990.0, ask=991.0, tick=1.0, dev=0.5, contract=CONTRACT, units=UNITS,
                                cash=D(300000), risk_remaining=D(1000), flow32=0, m30=0, m10=0)['plan']

    def drive(self, **kw):
        kw.setdefault('bid', 990.0); kw.setdefault('fresh', True)
        self.pf.book('BTC').drive(**kw)

    def entry_cid(self):
        return next(o['cid'] for o in self.client.submissions if o['role'] == 'entry')

    def test_take_profit_closes_campaign(self):
        self.assertTrue(self.pf.enter('BTC', self.plan, {}, '5000'))
        self.client.fill(self.entry_cid(), self.plan['qty'], '990')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        take = [o for o in self.client.submissions if o['role'] == 'take']
        self.assertEqual(len(take), 1)
        self.assertEqual((take[0]['side'], take[0]['type'], take[0]['price']), ('SELL', 'LIMIT', '991'))
        self.client.fill(take[0]['cid'], self.plan['qty'], '991')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        self.assertEqual(self.pf.campaigns, {})
        close = [b for _, k, b in self.events() if k == 'CLOSE'][0]
        self.assertEqual(close['campaign']['exit_reason'], 'take_profit')
        self.assertEqual(D(close['campaign']['net']), D(self.plan['qty']) * 1)
        self.assertEqual(D(self.pf.state['realized']), D(self.plan['qty']))

    def test_defend_cancels_take_then_sells_at_market(self):
        self.assertTrue(self.pf.enter('BTC', self.plan, {}, '5000'))
        self.client.fill(self.entry_cid(), self.plan['qty'], '990')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=False, reason='defend', cancel_entry=False))
        take = [o for o in self.client.submissions if o['role'] == 'take'][0]
        self.assertIn(take['cid'], self.client.cancels)
        exits = [o for o in self.client.submissions if o['role'] == 'exit']
        self.assertEqual(len(exits), 1)
        self.assertNotIn('limit_price', exits[0])
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=False, reason='defend', cancel_entry=False))
        close = [b for _, k, b in self.events() if k == 'CLOSE'][0]
        self.assertEqual(close['campaign']['exit_reason'], 'defend')
        self.assertEqual(self.pf.campaigns, {})

    def test_partial_take_fill_then_stop_sells_remainder_only(self):
        # 20,000 KRW so that half of it is still a sellable (>= 5,000 KRW) remainder.
        plan = rule.assess(rule_cfg(notional_krw=20000), coin='BTC', bid=990.0, ask=991.0, tick=1.0, dev=0.5, contract=CONTRACT, units=UNITS,
                           cash=D(300000), risk_remaining=D(1000), flow32=0, m30=0, m10=0)['plan']
        self.assertTrue(self.pf.enter('BTC', plan, {}, '5000'))
        self.client.fill(self.entry_cid(), plan['qty'], '990')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        take = [o for o in self.client.submissions if o['role'] == 'take'][0]
        half = str(D(plan['qty']) / 2)
        self.client.fill(take['cid'], half, '991', 'PARTIALLY_FILLED')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=False, reason='stop', cancel_entry=False), bid=987.0)
        exits = [o for o in self.client.submissions if o['role'] == 'exit']
        self.assertEqual(D(exits[0]['qty']), D(plan['qty']) - D(half))

    def test_partial_take_fill_leaving_dust_carries_residual_on_exit(self):
        self.assertTrue(self.pf.enter('BTC', self.plan, {}, '5000'))
        self.client.fill(self.entry_cid(), self.plan['qty'], '990')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        take = [o for o in self.client.submissions if o['role'] == 'take'][0]
        left = '1'   # 990 KRW remains: below the exchange minimum
        self.client.fill(take['cid'], str(D(self.plan['qty']) - 1), '991', 'PARTIALLY_FILLED')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        self.assertIn('BTC', self.pf.campaigns)            # resting sale may still finish the remainder
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=False, reason='defend', cancel_entry=False))
        self.assertEqual(self.pf.campaigns, {})
        self.assertEqual(D(self.pf.state['residuals']['BTC']['qty']), D(left))
        self.assertFalse([o for o in self.client.submissions if o['role'] == 'exit'])

    def test_dust_after_partial_entry_is_carried_into_next_campaign(self):
        self.assertTrue(self.pf.enter('BTC', self.plan, {}, '5000'))
        cid = self.entry_cid()
        self.client.fill(cid, '2', '990', 'PARTIALLY_FILLED')   # 1,980 KRW worth: below the 5,000 minimum
        self.now[0] += 61                                        # TTL passes, entry canceled
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        self.assertEqual(self.pf.campaigns, {})
        self.assertEqual(self.pf.state['residuals']['BTC']['qty'], '2')
        self.assertEqual(D(self.pf.state['residuals']['BTC']['cost']), D(1980))
        close = [b for _, k, b in self.events() if k == 'CLOSE'][0]
        self.assertEqual(close['campaign']['exit_reason'], 'dust')
        self.assertFalse([o for o in self.client.submissions if o['role'] == 'take'])
        # Next campaign merges the residual and sells everything with one resting order.
        self.pf.state['capital_at'] = self.now[0]
        plan = rule.assess(self.cfg, coin='BTC', bid=990.0, ask=991.0, tick=1.0, dev=0.5, contract=CONTRACT, units=UNITS,
                           cash=D(300000), risk_remaining=D(1000), flow32=0, m30=0, m10=0)['plan']
        self.assertTrue(self.pf.enter('BTC', plan, {}, '5000'))
        c = self.pf.campaigns['BTC']
        self.assertEqual(c['qty'], '2'); self.assertEqual(self.pf.state['residuals'], {})
        cid2 = [o for o in self.client.submissions if o['role'] == 'entry'][-1]['cid']
        self.client.fill(cid2, plan['qty'], '990')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        take = [o for o in self.client.submissions if o['role'] == 'take'][0]
        self.assertEqual(D(take['qty']), D(plan['qty']) + 2)

    def test_no_fill_returns_merged_residual(self):
        self.pf.state['residuals']['BTC'] = dict(qty='2', cost='1980', t=0)
        self.assertTrue(self.pf.enter('BTC', self.plan, {}, '5000'))
        self.now[0] += 61
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        self.assertEqual(self.pf.campaigns, {})
        self.assertEqual(self.pf.state['residuals']['BTC']['qty'], '2')
        self.assertTrue([k for _, k, _ in self.events() if k == 'NO_FILL'])

    def test_restart_recovers_resting_take_and_fill(self):
        self.assertTrue(self.pf.enter('BTC', self.plan, {}, '5000'))
        self.client.fill(self.entry_cid(), self.plan['qty'], '990')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        take = [o for o in self.client.submissions if o['role'] == 'take'][0]
        self.client.fill(take['cid'], self.plan['qty'], '991')
        again = Portfolio(self.cfg, self.client, self.store, clock=lambda: self.now[0])
        self.assertIn('BTC', again.campaigns)
        again.book('BTC').drive(bid=991.0, fresh=True, quantitative_decision=dict(hold=True, cancel_entry=False))
        self.assertEqual(again.campaigns, {})
        self.assertEqual(D(again.state['realized']), D(self.plan['qty']))

    def test_poll_interval_limits_rest_reads_unless_forced(self):
        self.pf.poll_interval = 1.0
        self.assertTrue(self.pf.enter('BTC', self.plan, {}, '5000'))
        book = self.pf.book('BTC')
        calls = [0]
        original = self.client.detail
        def counting(coin, cid):
            calls[0] += 1
            return original(coin, cid)
        self.client.detail = counting
        for _ in range(5):
            self.now[0] += .2
            book.drive(bid=990.0, fresh=True, quantitative_decision=dict(hold=True, cancel_entry=False))
        self.assertEqual(calls[0], 1)          # five drives inside one second: one REST read
        self.now[0] += .2
        book.drive(bid=990.0, fresh=True, quantitative_decision=dict(hold=True, cancel_entry=False), force_reconcile=True)
        self.assertEqual(calls[0], 2)          # a private-stream event forces a read
        self.now[0] += 1.0
        book.drive(bid=990.0, fresh=True, quantitative_decision=dict(hold=True, cancel_entry=False))
        self.assertEqual(calls[0], 3)

    def test_take_rejection_falls_back_to_market_exit(self):
        self.assertTrue(self.pf.enter('BTC', self.plan, {}, '5000'))
        self.client.fill(self.entry_cid(), self.plan['qty'], '990')
        self.client.fail = 'reject'
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        self.client.fail = None
        self.assertEqual(self.pf.campaigns['BTC']['exit_reason'], 'take_rejected')
        self.now[0] += 1
        self.drive(quantitative_decision=dict(hold=True, cancel_entry=False))
        self.assertTrue([o for o in self.client.submissions if o['role'] == 'exit'])

    def events(self):
        import sqlite3
        db = sqlite3.connect(Path(self.tmp.name) / 'ledger.sqlite')
        rows = db.execute('SELECT t_ms,kind,body FROM events ORDER BY seq').fetchall()
        db.close()
        return [(t, k, json.loads(b)) for t, k, b in rows]


class ReplayExchangeTests(unittest.TestCase):
    def test_resting_sale_fills_behind_visible_ask_queue(self):
        from .outcomes import Path as Tape
        book = dict(t=1000, exchange_t=1000, kind='book', bids=[(990.0, 5.0)], asks=[(991.0, 2.0), (992.0, 3.0)])
        events = [book, dict(t=1500, exchange_t=1500, kind='trade', price=991.0, qty=1.0, buy=True),
                  dict(t=1600, exchange_t=1600, kind='trade', price=991.0, qty=2.0, buy=True),
                  dict(t=1700, exchange_t=1700, kind='trade', price=990.0, qty=5.0, buy=False)]
        clock = [0.0]
        ex = Exchange({'BTC': Tape(events)}, lambda: clock[0], latency=250)
        clock[0] = 1.0
        ex.submit(dict(cid='tc-take-1', coin='BTC', role='take', side='SELL', type='LIMIT', qty='1', price='991'))
        for e in events[1:]:
            ex.event('BTC', e)
        o = ex.orders['tc-take-1']
        self.assertEqual(o['status'], 'FILLED')            # 2 ahead consumed by 1+1, then our 1 fills
        self.assertEqual(D(o['average_executed_price']), D(991))
        ex.submit(dict(cid='tc-take-2', coin='BTC', role='take', side='SELL', type='LIMIT', qty='1', price='990'))
        ex.settle(1300)
        self.assertEqual(ex.orders['tc-take-2']['status'], 'REJECTED')  # would cross the bid


class ConfigTests(unittest.TestCase):
    def test_c3_config_loads(self):
        cfg = load(Path(__file__).with_name('config-c3.json'))
        self.assertEqual(cfg['policy'], 'rule')
        self.assertEqual(cfg['coins'], ['BTC', 'ETH', 'XRP', 'SOL'])

    def test_invalid_v3_config_is_rejected(self):
        current=json.loads(Path(__file__).with_name('config-c3.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json'
            for over in (dict(momentum_recent_s=30),dict(momentum_window_s=30.5),dict(momentum_veto_ticks=float('nan')),
                         dict(momentum_decel_share=0),dict(momentum_decel_share=1),dict(flow_gate='true')):
                path.write_text(json.dumps(dict(current,**over)))
                with self.assertRaises(ValueError): load(path)

    def test_upgrade_preserves_live_size_stops_symbols_and_other_settings(self):
        from .c3_upgrade import upgraded_config,V3_CONFIG_KEYS
        live=rule_cfg()
        desired=dict(live,mode='observe',funding_confirmed=False,coins=['ETH'],notional_krw=999999,stop_ticks=15)
        updated=upgraded_config(live,desired)
        self.assertEqual({k:v for k,v in updated.items() if k not in V3_CONFIG_KEYS},
                         {k:v for k,v in live.items() if k not in V3_CONFIG_KEYS})


if __name__ == '__main__':
    unittest.main()
