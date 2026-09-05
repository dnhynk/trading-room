"""Economic invariants and causal-data boundaries for adaptive C3 exits."""
from copy import deepcopy
from decimal import Decimal as D
import math
import statistics
import tempfile
import unittest

from .exit_model import DEFAULTS, ExitModel, asof_value, mean_uncertainty, validate_exit_settings
from .fair import FairValue
from .portfolio import Portfolio
from .store import Store
from . import rule
from .test_c3 import CONTRACT, UNITS, RestingExchange, rule_cfg


def config(**changes):
    c = dict(DEFAULTS, stop_mode='volatility', value_exit=True)
    c.update(changes)
    return rule_cfg(**c)


def observe(model, second, bid=10000., spread=1., gap=2., venues=('U',), tick=1.):
    return model.observe(second*1000, mid=bid+spread/2, fair=bid+spread/2+gap,
                         bid=bid, ask=bid+spread, tick=tick, m30=0., m10=0., venues=venues)


class VolatilityTests(unittest.TestCase):
    def test_reference_diagnostics_expose_basis_drift_without_external_move(self):
        f = FairValue(min_samples=1)
        f.leader_quote('U', 0, 99., 101.)
        f.evaluate(0, 100., 1.)
        before = f.evaluate(1000, 90., 1.)
        after = f.evaluate(2000, 90., 1.)
        self.assertEqual(before['reference_price'], after['reference_price'])
        self.assertEqual(after['reference_price'], 100.)
        self.assertEqual(before['fair'], 100.)
        self.assertEqual(after['fair'], 95.)
        self.assertEqual(after['basis_ratio'], .95)
        self.assertEqual(after['reference_components']['U']['recv_ms'], 0)
        self.assertEqual((before['evaluated_ms'], after['evaluated_ms']), (1000, 2000))
        self.assertEqual((after['coinone_mid'], after['tick']), (90., 1.))

    def test_reference_diagnostics_preserve_weights_and_remove_stale_venue(self):
        f = FairValue(min_samples=1, leader_max_age_ms=1000, weights={'U':3.,'B':1.})
        for venue,price in (('U',100.),('B',200.)):
            f.leader_quote(venue,0,price-1,price+1)
        f.evaluate(0,100.,1.)
        f.evaluate(1000,100.,1.)
        f.leader_quote('U',1000,103.,105.)
        out = f.evaluate(1001,100.,1.)
        self.assertEqual(set(out['reference_components']), {'U'})  # B has expired.
        self.assertEqual(out['reference_components']['U']['weight'], 1.)
        self.assertEqual(out['leader_disagreement_ticks'], 0.)
        f.leader_quote('B',1001,199.,201.)
        out = f.evaluate(1002,100.,1.)
        self.assertEqual(out['reference_components']['U']['weight'], .75)
        self.assertEqual(out['reference_components']['B']['weight'], .25)
        self.assertEqual(out['fair'], 103.)
        self.assertEqual(out['reference_price'], 128.)
        self.assertEqual(out['leader_disagreement_ticks'], 4.)

    def test_complete_window_sparse_return_count_and_no_duplicate_samples(self):
        m = ExitModel(config())
        for second in range(300):
            result = observe(m, second, bid=10000.+second)
        self.assertFalse(result['ready'])
        result = observe(m, 300, bid=10300.)
        self.assertTrue(result['ready'])
        self.assertEqual(result['n_returns'], 30)  # Not 300 allegedly independent returns.
        self.assertEqual(result['mid_variance_price_s'], 10.)
        expected = statistics.NormalDist().inv_cdf(.975)*math.sqrt(10*180)
        self.assertAlmostEqual(result['distance_price'], expected)
        count = len(m.history)
        again = observe(m, 300.5, bid=10310.)
        self.assertEqual(len(m.history), count)
        self.assertEqual(again['sample_end_ms'], 300000)
        self.assertEqual(again['mid_variance_price_s'], 10.)

    def test_flat_prices_keep_spread_and_one_tick_floor(self):
        m = ExitModel(config())
        for t in range(301):
            out = observe(m, t, spread=2.)
        self.assertEqual(out['sigma_price_sqrt_s'], 0.)
        self.assertEqual(out['distance_price'], 2.)
        self.assertFalse(m.risk(302501)['ready'])

    def test_moving_leader_cannot_hide_behind_stationary_local_price(self):
        m = ExitModel(config())
        for t in range(301):
            out = observe(m, t, gap=t/10)
        self.assertEqual(out['mid_variance_price_s'], 0.)
        self.assertAlmostEqual(out['fair_variance_price_s'], .1)
        self.assertGreater(out['distance_price'], 8.)

    def test_gap_venue_change_tick_change_and_backwards_time_break_window(self):
        for variation in ({'second': 302}, {'second': 301, 'venues': ('B','U')},
                          {'second': 301, 'tick': 2.}, {'second': 200}):
            with self.subTest(variation=variation):
                m = ExitModel(config())
                for t in range(301):
                    observe(m, t)
                self.assertFalse(observe(m, **variation)['ready'])

    def test_fair_integration_and_disconnect_never_reuse_risk_window(self):
        f = FairValue(min_samples=1, exit_config=config())
        for t in range(302):
            f.leader_quote('U', t*1000, 9999., 10001.)
            out = f.evaluate(t*1000, 10000., 1., bid=9999.5, ask=10000.5)
        self.assertTrue(out['risk']['ready'])
        f.disconnect('U')
        f.leader_quote('U', 302000, 9999., 10001.)
        out = f.evaluate(302000, 10000., 1., bid=9999.5, ask=10000.5)
        self.assertIsNone(out['m30'])
        self.assertFalse(out['risk']['ready'])


class RiskSizingTests(unittest.TestCase):
    def setUp(self):
        self.c = config()
        self.kw = dict(coin='BTC', bid=990., ask=991., tick=1., dev=2., contract=CONTRACT,
                       units=UNITS, cash=D(500000), risk_remaining=D(1000), flow32=0., m30=0., m10=0.)

    def plan(self, distance):
        return rule.assess(self.c, **self.kw, risk=dict(ready=True, distance_price=distance))

    def test_wider_stop_shrinks_size_preserves_risk_and_follows_ladder(self):
        base = rule.assess(rule_cfg(), **self.kw)['plan']
        result = self.plan(5.2)
        self.assertTrue(result['accepted'])
        p = result['plan']
        self.assertEqual((p['stop'], p['stop_limit']), ('984', '983'))
        self.assertLess(D(p['qty']), D(base['qty']))
        self.assertLessEqual(D(p['nominal_loss_krw']), D(base['nominal_loss_krw']))
        self.assertGreaterEqual(D(p['qty'])*D(p['stop_limit']), D(5000))

    def test_quiet_market_does_not_increase_order_size(self):
        base = rule.assess(rule_cfg(), **self.kw)['plan']
        p = self.plan(1.)['plan']
        self.assertEqual(p['qty'], base['qty'])
        self.assertEqual(p['stop'], '989')

    def test_minimum_order_floor_raises_size_and_discloses_risk_increase(self):
        p = self.plan(9.)['plan']
        self.assertGreater(D(p['qty']), D(p['risk_target_qty']))
        self.assertLessEqual(D(p['qty']), D(p['base_qty']))
        self.assertGreaterEqual(D(p['qty'])*D(p['stop_limit']), D(5250))
        self.assertLess((D(p['qty'])-D(CONTRACT['qty_unit']))*D(p['stop_limit']), D(5250))
        self.assertEqual(D(p['minimum_size_uplift_qty']), D(p['qty'])-D(p['risk_target_qty']))
        self.assertGreater(D(p['nominal_loss_krw']), D(p['base_nominal_loss_krw']))
        self.assertEqual(D(p['minimum_risk_excess_krw']),
                         D(p['nominal_loss_krw'])-D(p['base_nominal_loss_krw']))

    def test_minimum_floor_cannot_override_order_cash_or_portfolio_budget(self):
        self.c['notional_krw'] = '5000'
        self.assertEqual(self.plan(9.)['reason'], 'minimum_exceeds_order_budget')
        self.c['notional_krw'] = '20000'
        p = self.plan(20.)['plan']
        self.assertGreater(D(p['minimum_size_uplift_qty']), 0)
        self.kw['risk_remaining'] = D(p['nominal_loss_krw'])-D('.01')
        self.assertEqual(self.plan(20.)['reason'], 'risk_budget')
        self.kw['risk_remaining'] = D(1000)
        self.kw['cash'] = D(5000)
        self.assertEqual(self.plan(20.)['reason'], 'cash')
        self.kw['cash'] = D(500000)
        self.kw['contract'] = dict(CONTRACT, max_qty='5')
        self.assertEqual(self.plan(20.)['reason'], 'minimum_exceeds_order_budget')

    def test_larger_base_can_meet_minimum_without_risk_target_override(self):
        self.c['notional_krw'] = '20000'
        p = self.plan(9.)['plan']
        self.assertEqual(D(p['minimum_size_uplift_qty']), 0)
        self.assertEqual(D(p['minimum_risk_excess_krw']), 0)
        self.assertGreaterEqual(D(p['qty'])*D(p['stop_limit']), D(5250))
        self.assertLessEqual(D(p['nominal_loss_krw']), D(p['base_nominal_loss_krw']))

    def test_minimum_quantity_is_rounded_up_and_tiny_price_is_invalid(self):
        self.kw['contract'] = dict(CONTRACT, min_qty='6.135')
        self.assertEqual(self.plan(9.)['plan']['qty'], '6.14')
        self.assertEqual(self.plan(991.)['reason'], 'invalid_stop_distance')
        self.assertEqual(self.plan(989.5)['reason'], 'price_ladder')

    def test_missing_invalid_volatility_refuses_and_cancels_entry(self):
        for distance in (None, float('nan'), float('inf'), -1., 0.):
            self.assertEqual(self.plan(distance)['reason'], 'volatility_unavailable')
        self.assertTrue(rule.cancel_entry(self.c, dev=2., m30=0., m10=0.))
        self.assertFalse(rule.cancel_entry(self.c, dev=2., m30=0., m10=0., risk=dict(ready=True)))

    def test_price_band_uses_actual_price_distance_for_sizing(self):
        kw = dict(self.kw, bid=1000., ask=1005., tick=5.)
        p = rule.assess(self.c, **kw, risk=dict(ready=True, distance_price=5.2))['plan']
        self.assertEqual((p['stop'],p['stop_limit']), ('994','993'))
        self.assertLessEqual(D(p['nominal_loss_krw']), D(p['base_nominal_loss_krw']))

    def test_restart_preserves_absolute_adaptive_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                now = [1788595000.]
                client = RestingExchange()
                pf = Portfolio(self.c, client, store, clock=lambda: now[0])
                pf.sync_cash(D(300000))
                p = self.plan(4.2)['plan']
                self.assertTrue(pf.enter('BTC', p, {}, '5000'))
                cid = next(o['cid'] for o in client.submissions if o['role']=='entry')
                client.fill(cid, p['qty'], p['entry'])
                now[0] += 1
                pf.book('BTC').drive(bid=990., fresh=True, quantitative_decision=dict(hold=True, cancel_entry=False))
                again = Portfolio(self.c, client, store, clock=lambda: now[0])
                campaign = again.campaigns['BTC']
                self.assertEqual(campaign['stop'], p['stop'])
                self.assertEqual(campaign['stop_limit'], p['stop_limit'])
                # A later wider volatility estimate cannot lower this campaign's stop.
                changed_risk = self.plan(5.2)['plan']
                self.assertLess(D(changed_risk['stop']), D(campaign['stop']))
                self.assertEqual(rule.hold(self.c, dev=2, bid=984, entry=990, tick=1,
                                           age_s=1, stop=campaign['stop'])['reason'], 'stop')
            finally:
                store.close()


class ContinuationTests(unittest.TestCase):
    def model(self, slope=-.01, seconds=3650):
        m = ExitModel(config())
        for t in range(seconds+1):
            observe(m, t, bid=10000+slope*t, spread=.5, gap=10.)
        return m

    def query(self, m, **over):
        p = m.current
        kw = dict(bid=p.bid, ask=p.ask, tick=p.tick, stop=p.bid-5, stop_limit=p.bid-6,
                  take=p.ask+1, age_s=0)
        kw.update(over)
        return m.continuation(p.at, **kw)

    def test_only_matured_nonoverlapping_paths_count(self):
        m = self.model(seconds=3619)
        self.assertEqual(len(m.paths), 20)
        # The twentieth path ends at this exact decision, so it is not prior evidence yet.
        self.assertEqual(self.query(m)['n_paths'], 19)
        self.assertFalse(self.query(m)['ready'])
        self.assertTrue(all(a.end.at < b.start.at for a,b in zip(m.paths,list(m.paths)[1:])))
        observe(m, 3620, bid=9963.8, spread=.5, gap=10.)
        out = self.query(m)
        self.assertEqual(out['n_paths'], 20)
        self.assertLess(out['last_source_end_ms'], m.current.at)
        before = deepcopy(out)
        self.assertEqual(self.query(m), before)  # Repeated decisions do not create new samples.

    def test_negative_continuation_exits_even_with_optimistic_passive_fill(self):
        m = self.model()
        out = self.query(m)
        self.assertTrue(out['ready'])
        self.assertTrue(out['exit'])
        self.assertAlmostEqual(out['mean_ticks'], -1.8, places=6)

    def test_upside_is_capped_at_current_take(self):
        m = self.model(slope=.1)
        out = self.query(m)
        self.assertTrue(out['ready'])
        self.assertFalse(out['exit'])
        self.assertAlmostEqual(out['mean_ticks'], 2.5)
        self.assertLess(out['mean_ticks'], .1*180)  # Cannot book the uncapped future rise.

    def test_remaining_time_and_sunk_entry_price_are_not_confused(self):
        m = self.model()
        out = self.query(m, age_s=170)
        self.assertAlmostEqual(out['mean_ticks'], -.1, places=6)
        # Neither entry price nor previous PnL is an input to continuation value.
        self.assertFalse(self.query(m, age_s=None)['ready'])
        self.assertFalse(self.query(m, age_s=180)['ready'])

    def test_state_basis_and_future_data_cannot_leak_across_resets(self):
        m = self.model()
        self.assertTrue(self.query(m)['ready'])
        observe(m, 3651, bid=9963.49, venues=('B',), spread=.5, gap=10.)
        self.assertFalse(self.query(m)['ready'])
        self.assertEqual(self.query(m)['n_paths'], 0)
        observe(m, 10, bid=10000., spread=.5, gap=10.)
        self.assertEqual(len(m.paths), 0)
        self.assertFalse(self.query(m)['ready'])

    def test_changed_book_and_irregular_asof_times_never_use_later_price(self):
        m = self.model()
        self.assertEqual(self.query(m,bid=m.current.bid-.1)['reason'], 'value_book_changed')
        self.assertEqual(asof_value([0,900,1100],[1.,100.,2.],100), 1.)
        self.assertIsNone(asof_value([900,1100],[100.,2.],100))

    def test_uncertainty_buffer_blocks_noisy_weak_negative_mean(self):
        out = mean_uncertainty([-2.1, 1.9]*10)
        self.assertLess(out['mean_ticks'], 0)
        self.assertGreater(out['upper_ticks'], 0)
        positive_dependence = mean_uncertainty([-1.]*10+[1.]*10)
        iid_se = statistics.stdev([-1.]*10+[1.]*10)/math.sqrt(20)
        self.assertGreater(positive_dependence['se_ticks'], iid_se)

    def test_flip_control_uses_its_own_deviation_sign_in_continuation(self):
        m = ExitModel(config(), flip=True)
        for t in range(3651):
            observe(m, t, bid=10000-.01*t, spread=.5, gap=-10.)
        self.assertTrue(m.current.regime[0])  # Physical negative gap, flipped positive gap.
        self.assertAlmostEqual(self.query(m)['mean_ticks'], -1.8, places=6)

    def test_value_respects_existing_protection_and_unavailable_fallback(self):
        c = config()
        kw = dict(dev=2., bid=990., entry=990., tick=1., age_s=1., stop=987., m30=0., m10=0.)
        out = dict(ready=True, exit=True, upper_ticks=-.1)
        self.assertEqual(rule.hold(c, **kw, continuation=out)['reason'], 'value')
        self.assertTrue(rule.hold(c, **kw, continuation=dict(ready=False, exit=True))['hold'])
        self.assertTrue(rule.hold(c, **kw, continuation=out, unconditional=True)['hold'])
        self.assertEqual(rule.hold(c, **dict(kw,bid=987.), continuation=out)['reason'], 'stop')
        self.assertEqual(rule.hold(c, **dict(kw,m30=-3,m10=-1), continuation=out)['reason'], 'brake')
        self.assertEqual(rule.hold(c, **dict(kw,dev=-1), continuation=out)['reason'], 'defend')
        self.assertEqual(rule.hold(c, **dict(kw,age_s=180), continuation=out)['reason'], 'time')


class ExitSettingsTests(unittest.TestCase):
    def test_defaults_preserve_legacy_callers_and_reject_invalid_settings(self):
        self.assertEqual(validate_exit_settings(rule_cfg()), DEFAULTS)
        for change in (dict(stop_mode='atr'), dict(value_exit=1), dict(stop_tail_probability=0),
                       dict(value_confidence=1), dict(stop_vol_stride_s=30), dict(stop_vol_window_s=300.5),
                       dict(value_min_paths=19), dict(value_window_s=3600)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_exit_settings(dict(rule_cfg(), **change))


if __name__ == '__main__':
    unittest.main()
