"""Economic/accounting/causality regressions, not profit claims."""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest

from track_c.learning.config import validate, digest
from track_c.market.reference import Reference
from track_c.market.state import candidates, Market
from track_c.learning.benchmark import HazardModel, CashModel, cluster_interval
from track_c.replay.queue import Attempt
from track_c.replay.ledger import Book


def cfg(**overrides): return validate(overrides)


def snap(now=1000000, bid=100., lower=104.):
    return dict(coin='BTC',t_ms=now,book_ms=now,bid=bid,ask=bid+1,tick=1.,
                bids=[(bid,10.),(bid-1,10.)],asks=[(bid+1,10.)],
                reference=dict(ready=True,t_ms=now,fair=lower+1,lower=lower,upper=lower+2,
                               dev_ticks=lower+1-(bid+.5),m10=0.,m30=0.),
                pressure=2.,flow=-.7,buy_volume=30.,sell_volume=50.,episode_id='BTC-e1',
                risk=dict(ready=True,distance_price=2.),contract=dict(qty_unit='.1',min_qty='.1',
                    min_order_amount='5',max_order_amount='1000000',max_qty='1000'),
                units=[dict(range_min='0',price_unit='1')])


def action(now=1000000, **changes):
    return dict(id='0:minimum',offset=0,size='minimum',price=100.,qty=1.,stop=97.,stop_limit=96.,
                minimum=5.,qty_step=.1,tick=1.,ttl_s=8,hold_s=180,key='BTC:deep:sell',notional=100.,
                nominal_loss=4.,episode_id='BTC-e1',coin='BTC',t_ms=now,reference=105.,dev_ticks=4.,
                pressure=2.,spread_ticks=1.,**changes)


def trade(t,qty=12.,price=100.,buy=False):
    return dict(kind='trade',t=t,qty=qty,price=price,buy=buy)


def book(t,bid=100.,qty=10.):
    return dict(kind='book',t=t,bids=[(bid,qty)],asks=[(bid+1,10.)])


def label(i, *, net=2., filled=1., duration=8., cause='recovery', until=5000000, exit_model='structural'):
    a=action()
    a['episode_id']=f'ep{i}'
    return dict(action=a,episode_id=a['episode_id'],start_ms=i*86400000,end_ms=i*86400000+10000,
                censored=False,filled_qty=filled,net_bp=net,fill_fraction=filled,terminal_bp=net,
                duration_s=duration,cause=cause,occupied_s=10.,exit_model=exit_model,
                exit_trained_until=-1)


class ReferenceTests(unittest.TestCase):
    def warm(self):
        c=cfg(basis_min_samples=2)
        r=Reference(c)
        for t in range(1000,35000,1000):
            for v in ('U','B'): r.quote(['b',t,v,'BTC',t,100.,10.,101.,10.])
            out=r.evaluate(t,100.5,1.)
        self.assertTrue(out['ready'])
        return r

    def test_current_sample_cannot_warm_its_own_reference(self):
        r=Reference(cfg(basis_min_samples=2))
        for t in (1000,2000):
            for v in ('U','B'): r.quote(['b',t,v,'BTC',t,100.,10.,101.,10.])
            self.assertFalse(r.evaluate(t,100.5,1.)['ready'])
        self.assertEqual(len(r.basis['U']),2)

    def test_shock_does_not_rebase_fair_downward(self):
        r=self.warm()
        before=list(r.basis['U'])
        out=r.evaluate(34500,94.5,1.,sell_pressure=2.)
        self.assertAlmostEqual(out['fair'],100.5)
        self.assertEqual(before,list(r.basis['U']))

    def test_two_venues_and_known_disconnect_are_mandatory(self):
        r=self.warm()
        r.quote(['s',34500,'B','connection','disconnected'])
        self.assertFalse(r.evaluate(34500,100.5,1.)['ready'])

    def test_disagreement_is_uncertainty_not_averaged_alpha(self):
        r=self.warm()
        r.quote(['b',34500,'B','BTC',34500,110.,1.,111.,1.])
        out=r.evaluate(34500,100.5,1.)
        self.assertEqual(out['reason'],'reference_disagreement')
        self.assertLess(out['lower'],out['fair'])

    def test_future_or_stale_exchange_quotes_do_not_enter_reference(self):
        for exchange in (35001,1):
            r=self.warm()
            r.quote(['b',35000,'B','BTC',exchange,110.,1.,111.,1.])
            self.assertEqual(r.quotes['B']['mid'],100.5)

    def test_persistent_dislocation_disables_entry(self):
        r=self.warm()
        for t in range(35000,230000,1000):
            for v in ('U','B'): r.quote(['b',t,v,'BTC',t,100.,1.,101.,1.])
            out=r.evaluate(t,94.5,1.,sell_pressure=2.)
        self.assertEqual(out['reason'],'basis_break')

    def test_basis_break_recalibrates_only_after_quiet_and_requires_new_history(self):
        r=self.warm()
        for t in range(35000,230000,1000):
            for v in ('U','B'):r.quote(['b',t,v,'BTC',t,100.,1.,101.,1.])
            r.evaluate(t,94.5,1.,sell_pressure=2.)
        for t in (230000,231000):
            for v in ('U','B'):r.quote(['b',t,v,'BTC',t,100.,1.,101.,1.])
            out=r.evaluate(t,94.5,1.)
        self.assertEqual(out['reason'],'basis_recalibrated')
        self.assertFalse(out['ready'])
        self.assertEqual(r.regime,1)


class CapacityTests(unittest.TestCase):
    def test_price_improvement_cannot_cross_ask(self):
        actions=candidates(snap(),cfg(),10000.,100.)
        self.assertTrue(actions)
        self.assertTrue(all(a['price']<101 for a in actions))
        self.assertTrue(all(a['qty'] <= 1. for a in actions))

    def test_minimum_never_rounds_past_cash_risk_or_depth(self):
        self.assertFalse(candidates(snap(),cfg(),.1,100.))
        self.assertFalse(candidates(snap(),cfg(),10000.,.001))
        s=snap();s['bids']=[(100.,.001)]
        self.assertFalse(candidates(s,cfg(),10000.,100.))

    def test_no_observed_buy_flow_has_no_exit_capacity(self):
        s=snap();s['buy_volume']=0.
        self.assertFalse(candidates(s,cfg(),10000.,100.))

    def test_sampling_tolerance_does_not_allow_stale_entry(self):
        s=snap();s['entry_fresh']=False
        self.assertFalse(candidates(s,cfg(),10000.,100.))

    def test_exchange_maintenance_disables_candidates(self):
        s=snap();s['contract']['maintenance_status']=1
        self.assertFalse(candidates(s,cfg(),10000.,100.))

    def test_rejected_shocks_can_be_labeled_but_never_admitted(self):
        s=snap();s['entry_eligible']=False;s['reference']['ready']=False
        self.assertFalse(candidates(s,cfg(),10000.,100.))
        self.assertTrue(candidates(s,cfg(),10000.,100.,research=True))

    def test_inside_spread_uses_actual_ladder_and_all_actions_have_legal_exit(self):
        s=snap();s['ask']=102.;s['asks']=[(102.,10.)]
        actions=candidates(s,cfg(),10000.,100.)
        self.assertIn(1,[a['offset'] for a in actions])
        self.assertTrue(all(a['qty']*a['stop_limit'] >= a['minimum']*1.05 for a in actions))


class ExecutionTests(unittest.TestCase):
    def test_touch_without_trade_never_fills(self):
        a=Attempt(action(),cfg(),snap())
        a.event(book(1000300,100.,0.1))
        a.advance(1001000)
        self.assertEqual(a.bought,0.)

    def test_queue_cancellation_does_not_improve_position(self):
        a=Attempt(action(),cfg(),snap())
        a.event(book(1000300,100.,.1))
        a.event(trade(1000400,1.))
        self.assertEqual(a.bought,0.)
        self.assertEqual(a.ahead,9.)

    def test_arrival_has_no_access_to_later_same_time_book(self):
        a=Attempt(action(),cfg(),snap())
        a.event(trade(1000250,11.))
        a.event(book(1000250,99.))
        self.assertAlmostEqual(a.bought,1.)

    def test_marketable_at_arrival_is_rejected(self):
        s=snap();s['asks']=[(100.,10.)]
        a=Attempt(action(),cfg(),s)
        a.advance(1000250)
        self.assertEqual(a.reason,'arrival_reject')
        self.assertEqual(a.bought,0.)

    def test_partial_fill_during_cancel_is_included(self):
        a=Attempt(action(),cfg(),snap())
        a.event(trade(1000300,10.5))
        a.decide(1000350,snap(1000350))
        self.assertIsNotNone(a.cancel_at)
        a.event(trade(1000600,.3))
        self.assertAlmostEqual(a.bought,.8)
        a.event(trade(1000900,10.))
        self.assertAlmostEqual(a.bought,.8)

    def test_aggressive_buy_does_not_fill_our_bid(self):
        a=Attempt(action(),cfg(),snap())
        a.event(trade(1000300,1000.,buy=True))
        self.assertEqual(a.bought,0.)

    def test_exit_uses_arrival_depth_and_fee_once(self):
        a=Attempt(action(),cfg(fee_bp=1.),snap())
        a.event(trade(1000300))
        a.event(book(1000400,104.))
        a.decide(1000450,snap(1000450,104.,104.))
        a.event(book(1000600,103.))
        a.advance(1000800)
        self.assertTrue(a.done)
        r=a.result()
        self.assertAlmostEqual(r['gross_exit_krw'],103.)
        self.assertAlmostEqual(r['fees_krw'],.0203)
        self.assertAlmostEqual(r['net_krw'],2.9797)
        self.assertAlmostEqual(r['external_pnl_krw']+r['relative_pnl_krw'],3.)

    def test_subminimum_partial_is_residual_not_a_win_or_flat(self):
        ac=action();ac['minimum']=80.
        a=Attempt(ac,cfg(),snap())
        a.event(trade(1000300,10.5))
        a.cancel(1000350)
        a.advance(1000900)
        r=a.result()
        self.assertEqual(r['reason'],'residual')
        self.assertEqual(r['residual_qty'],.5)
        self.assertEqual(r['cash_net_krw'],-50.)
        self.assertEqual(r['net_krw'],0.)

    def test_boundary_censor_is_not_no_fill(self):
        a=Attempt(action(),cfg(),snap())
        a.event(trade(1000300))
        r=a.result(1000400)
        self.assertTrue(r['censored'])
        self.assertEqual(r['filled_qty'],1.)

    def test_replayed_clock_cannot_move_backward(self):
        a=Attempt(action(),cfg(),snap())
        with self.assertRaises(ValueError): a.advance(999999)

    def test_no_model_does_not_degenerate_to_instant_sell(self):
        a=Attempt(action(),cfg(),snap())
        a.event(trade(1000300))
        a.decide(1000400,snap(1000400))
        self.assertIsNone(a.pending_exit)
        self.assertFalse(a.done)

    def test_future_hazard_cannot_force_exit(self):
        c=cfg()
        h=HazardModel.fit([],2000000,c)
        a=Attempt(action(),c,snap(),h)
        a.event(trade(1000300))
        a.decide(1000400,snap(1000400))
        self.assertIsNone(a.pending_exit)

    def test_recovery_can_never_remove_stop_priority_after_cancel(self):
        a=Attempt(action(),cfg(),snap());a.event(trade(1000300,10.5))
        a.event(book(1000400,104.));a.decide(1000450,snap(1000450,104.,104.))
        self.assertEqual(a.requested,'recovery')
        a.event(book(1000900,96.));a.decide(1001000,snap(1001000,96.,104.))
        self.assertEqual(a.pending_exit['reason'],'stop')
        self.assertEqual(a.pending_exit['limit'],0.)

    def test_price_limited_partial_exit_does_not_reuse_consumed_depth(self):
        a=Attempt(action(),cfg(),snap());a.event(trade(1000300))
        a.event(book(1000400,104.,2.));a.decide(1000450,snap(1000450,104.,104.))
        a.event(book(1000600,104.,1.))
        a.advance(1000800)
        self.assertAlmostEqual(a.sold,.5)
        a.decide(1000900,snap(1000900,104.,104.));a.advance(1001200)
        self.assertAlmostEqual(a.sold,.5)


class ModelTests(unittest.TestCase):
    def test_duplicate_actions_cannot_inflate_sample_size(self):
        r=label(1)
        with self.assertRaisesRegex(ValueError,'duplicate'): HazardModel.fit([r,r],10**12,cfg())

    def test_censored_and_boundary_crossing_labels_are_purged(self):
        rows=[label(i) for i in (1,2,3)]
        rows[0]['censored']=True
        h=HazardModel.fit(rows,2*86400000+5000,cfg())
        self.assertFalse(h.doc['groups'])

    def test_hazards_preserve_probability_mass(self):
        h=HazardModel.fit([label(i,cause=c) for i,c in enumerate(('recovery','collapse','timeout'),1)],10**12,cfg())
        for point in h.survival(action()):
            self.assertAlmostEqual(point['survival']+sum(point['incidence'].values()),1.)
            self.assertTrue(all(0<=v<=1 for v in point['hazard'].values()))

    def test_many_candidates_in_one_time_block_do_not_make_evidence(self):
        rows=[dict(start_ms=i,net_bp=2.) for i in range(100)]
        result=cluster_interval(rows,'net_bp',cfg())
        self.assertIsNone(result['lower'])
        self.assertEqual(result['blocks'],1)

    def test_adverse_fill_conditioning_can_reject_high_recovery_rate(self):
        c=cfg(min_attempts=10,min_fills=10,min_blocks=2)
        rows=[label(i,net=10. if i%10<6 else -20.,exit_model='exit') for i in range(40)]
        m=CashModel.fit(rows,10**12,c,'exit')
        out=m.predict(action(),10**12+1)
        self.assertTrue(out['ready'])
        self.assertLess(out['score_krw'],0.)
        self.assertLess(out['fill_conditioned_bp'],0.)

    def test_model_before_training_end_and_outside_size_support_reject(self):
        c=cfg(min_attempts=3,min_fills=2,min_blocks=2)
        rows=[label(i,exit_model='exit') for i in range(1,6)]
        m=CashModel.fit(rows,10**12,c,'exit')
        self.assertEqual(m.predict(action(),1)['reason'],'future_model')
        a=action();a['notional']=200.
        self.assertEqual(m.predict(a,10**12+1)['reason'],'outside_support')

    def test_entry_targets_require_exact_frozen_exit_model(self):
        with self.assertRaisesRegex(ValueError,'another exit'): CashModel.fit([label(1)],10**12,cfg(),'not_structural')

    def test_entry_labels_cannot_precede_exit_training(self):
        r=label(1,exit_model='exit');r['exit_trained_until']=r['start_ms']
        with self.assertRaisesRegex(ValueError,'leaked'): CashModel.fit([r],10**12,cfg(),'exit')

    def test_digest_rejects_artifact_mutation(self):
        h=HazardModel.fit([],1000,cfg())
        h.doc['trained_until']=0
        with self.assertRaisesRegex(ValueError,'digest'): HazardModel(h.doc,cfg())

    def test_invalid_settings_fail_closed(self):
        for change in (dict(fee_bp=float('nan')),dict(coins=['ETH']),dict(entry_ticks=1),
                       dict(notional_krw=20001),dict(price_offsets=[0,1,2])):
            with self.subTest(change=change),self.assertRaises(ValueError): cfg(**change)


class AccountingTests(unittest.TestCase):
    def test_selected_book_preserves_cash_and_dust_identity(self):
        c=cfg();b=Book(c,1000.)
        ac=action();ac['minimum']=80.
        a=Attempt(ac,c,snap());a.event(trade(1000300,10.5));a.cancel(1000350);a.advance(1000900)
        b.active=a;b.settle(1000900,{'BTC':snap(1000900)})
        report=b.report(1000900,{'BTC':snap(1000900)})
        self.assertAlmostEqual(report['cash_krw'],950.)
        self.assertAlmostEqual(report['terminal_equity_krw'],1000.)
        self.assertAlmostEqual(report['cash_recovery_stress_krw'],-50.)
        self.assertEqual(report['accounting_error_krw'],0.)
        self.assertEqual(b.capacity({'BTC':snap()})[1],0.)

    def test_restart_preserves_pending_cancel_and_partial_inventory(self):
        c=cfg();b=Book(c,1000.);a=Attempt(action(),c,snap())
        a.event(trade(1000300,10.5));a.cancel(1000350);b.active=a
        restored=Book.restore(json.loads(json.dumps(b.export())),c,None)
        restored.active.event(trade(1000600,.3))
        self.assertAlmostEqual(restored.active.bought,.8)
        self.assertEqual(restored.active.cancel_at,1000850)

    def test_cash_report_includes_unfinished_buy(self):
        c=cfg();b=Book(c,1000.);a=Attempt(action(),c,snap())
        a.event(trade(1000300));b.active=a
        r=b.report(1000400,{'BTC':snap(1000400)})
        self.assertAlmostEqual(r['cash_krw'],900.)
        self.assertAlmostEqual(r['terminal_equity_krw'],1000.)

    def test_market_restart_preserves_basis_risk_and_duplicate_filter(self):
        c=cfg(basis_min_samples=2);m=Market('BTC',c)
        for t in range(1000,34000,1000):
            common=dict(quote_currency='KRW',target_currency='BTC',timestamp=t,id=t)
            data=dict(common,bids=[dict(price=100,qty=10)],asks=[dict(price=101,qty=10)])
            m.feed('ORDERBOOK',data,t)
            for venue in ('U','B'):m.reference.quote(['b',t,venue,'BTC',t,100.,10.,101.,10.])
            m.snapshot(t,snap()['contract'],snap()['units'])
        state=json.loads(json.dumps(m.export()))
        restored=Market.restore(state,c)
        self.assertEqual(json.loads(json.dumps(restored.export())),state)
        self.assertIsNone(restored.feed('ORDERBOOK',data,33000))
        self.assertEqual(m.snapshot(33500,snap()['contract'],snap()['units']),
                         restored.snapshot(33500,snap()['contract'],snap()['units']))


class LocalRiskTests(unittest.TestCase):
    def test_local_volatility_survives_unavailable_reference(self):
        from track_c.market.risk import LocalRisk
        r=LocalRisk(cfg())
        for t in range(0,301000,1000):r.observe(book(t,100.+(t//10000)%2))
        value=r.evaluate(300000,1.)
        self.assertTrue(value['ready'])
        self.assertEqual(value['n_returns'],30)
        self.assertGreater(value['distance_price'],1.)

    def test_gaps_are_counted_and_future_books_are_not_used(self):
        from track_c.market.risk import LocalRisk
        r=LocalRisk(cfg())
        for t in range(0,301000,1000):
            if not 150000<=t<=170000:r.observe(book(t,100.+(t//10000)%2))
        value=r.evaluate(300000,1.)
        self.assertTrue(value['ready'])
        self.assertGreater(value['missing_intervals'],0)
        self.assertFalse(r.evaluate(299000,1.)['ready'])




if __name__ == '__main__': unittest.main()
