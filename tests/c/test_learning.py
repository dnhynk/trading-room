"""Regression evidence for actual C4 state/value/censoring defects."""
from copy import deepcopy
from collections import Counter
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from tests.c.test_market import snap as old_snap, action as old_action, book, trade, cfg
from track_c.replay.queue import Attempt as OldAttempt
from track_c.market.state import candidates as old_candidates
from track_c.learning.benchmark import CashModel as OldCash
from track_c.learning.features import WIDTHS, enrich, candidates, vector
from track_c.learning.model import CashModel, HazardModel, neighborhood
from track_c.replay.execution import Attempt, ExitPair
from track_c.replay.metrics import score_predictions, fill_selection
from track_c.replay.engine import Book, build

DAY=86400000
UNTIL=10*DAY


def state(t=1000000,bid=100.,lower=104.):
    return dict(old_snap(t,bid,lower),entry_eligible=True,entry_fresh=True,new_episode=True)


def action():return enrich(old_action(),state(),cfg())


def label(i,net=2.,filled=1.):
    a=action();a['episode_id']=f'ep-{i}'
    start=(i//20+1)*DAY+i%20*10000
    a['t_ms']=start
    return dict(action=a,episode_id=a['episode_id'],start_ms=start,end_ms=start+1000,
                censored=False,filled_qty=filled,fill_fraction=filled,net_bp=net,
                gross_bp=10000*filled+net,spent_bp=10000*filled,
                terminal_bp=net,cause='recovery' if net>0 else 'collapse',duration_s=1.,
                occupied_s=1.,exit_model='exit',exit_trained_until=0,
                exit_protocol='protect_cancel_reconcile',
                cash_net_krw=net*a['notional']/10000,residual_qty=0.)


class StateValueTests(unittest.TestCase):
    def test_old_or_different_execution_labels_cannot_enter_the_new_model(self):
        for protocol in (None,'direct'):
            with self.subTest(protocol=protocol):
                row=label(0);row['exit_protocol']=protocol
                with self.assertRaisesRegex(ValueError,'protocol'):
                    CashModel.fit([row],UNTIL,cfg(),'exit')
                row['exit_model']='structural'
                with self.assertRaisesRegex(ValueError,'protocol'):
                    HazardModel.fit([row],[],UNTIL,cfg())

    def test_candidates_keep_original_price_quantity_and_safety(self):
        s=state();s['ask']=102.;s['asks']=[(102.,10.)]
        old=old_candidates(s,cfg(),10000.,100.)
        new=candidates(s,cfg(),10000.,100.)
        self.assertEqual([{k:a[k] for k in old[0]} for a in new],old)
        self.assertEqual(len({json.dumps(a['x'],sort_keys=True) for a in new}),len(new))

    def test_current_state_changes_mean_inside_the_old_same_bucket(self):
        rows=[label(i) for i in range(120)]
        center=action()['x']['log_pressure']
        for i,r in enumerate(rows):
            high=i%2==1
            r['action']['x']['log_pressure']=center+(.35 if high else -.35)
            r['net_bp']=8. if high else -4.
        m=CashModel.fit(rows,UNTIL,cfg(),'exit')
        low=action();high=action()
        low['x']['log_pressure']=center-.3;high['x']['log_pressure']=center+.3
        self.assertEqual(low['key'],high['key'])
        a,b=m.predict(low,UNTIL+1),m.predict(high,UNTIL+1)
        self.assertGreater(b['expected_net_krw'],a['expected_net_krw'])
        self.assertTrue(a['ready']);self.assertTrue(b['ready'])
        self.assertLess(a['score_krw'],0.);self.assertGreater(b['score_krw'],0.)
        old=OldCash.fit(rows,UNTIL,cfg(),'exit')
        self.assertEqual(old.predict(low,UNTIL+1)['mean'],old.predict(high,UNTIL+1)['mean'])
        self.assertGreater(old.predict(low,UNTIL+1)['score_krw'],0.)

    def test_order_quantity_changes_fill_and_cash_predictions(self):
        rows=[label(i) for i in range(120)]
        middle=action()['x']['log_notional']
        for i,r in enumerate(rows):
            large=i%2==1
            r['action']['x']['log_notional']=middle+(.4 if large else -.4)
            r.update(filled_qty=0. if large else 1.,fill_fraction=0. if large else 1.,net_bp=0. if large else 4.)
        m=CashModel.fit(rows,UNTIL,cfg(),'exit')
        low,high=action(),action()
        low['x']['log_notional']=middle-.35;high['x']['log_notional']=middle+.35
        a,b=m.predict(low,UNTIL+1),m.predict(high,UNTIL+1)
        self.assertGreater(a['p_fill'],b['p_fill'])
        self.assertGreater(a['expected_fill_fraction'],b['expected_fill_fraction'])
        self.assertGreater(a['expected_net_krw'],b['expected_net_krw'])

    def test_joint_hole_rejected_despite_each_marginal_being_in_range(self):
        rows=[label(i) for i in range(60)];a=action();center=deepcopy(a['x'])
        for i,r in enumerate(rows):
            sign=1 if i%2 else -1
            for k in ('edge_ticks','log_pressure'):r['action']['x'][k]=center[k]+sign*.9*WIDTHS[k]
        a['x']['edge_ticks']=center['edge_ticks']-.9*WIDTHS['edge_ticks']
        a['x']['log_pressure']=center['log_pressure']+.9*WIDTHS['log_pressure']
        for k in ('edge_ticks','log_pressure'):
            self.assertLessEqual(min(r['action']['x'][k] for r in rows),a['x'][k])
            self.assertGreaterEqual(max(r['action']['x'][k] for r in rows),a['x'][k])
        p=CashModel.fit(rows,UNTIL,cfg(),'exit').predict(a,UNTIL+1)
        self.assertFalse(p['ready']);self.assertEqual(p['episodes'],0)

    def test_six_candidates_and_priors_do_not_make_six_episodes(self):
        rows=[]
        for i in range(10):
            for offset in range(6):
                r=label(i);r['action']['id']=str(offset);rows.append(r)
        p=CashModel.fit(rows,UNTIL,cfg(),'exit').predict(action(),UNTIL+1)
        self.assertEqual(p['episodes'],10);self.assertFalse(p['ready'])

    def test_direct_attempt_cash_is_not_multiplied_by_fill_probability(self):
        rows=[label(i,net=4. if i%2 else 0.,filled=float(i%2)) for i in range(60)]
        p=CashModel.fit(rows,UNTIL,cfg(),'exit').predict(action(),UNTIL+1)
        self.assertAlmostEqual(p['expected_net_krw'],.02)
        self.assertAlmostEqual(p['p_fill_empirical']*p['fill_conditioned_net_krw'],p['expected_net_krw'])
        self.assertAlmostEqual(p['expected_gross_recovery_krw']-p['expected_spend_krw'],p['expected_net_krw'])
        self.assertAlmostEqual(p['score_krw'],p['cash_lower_krw']-p['inventory_penalty_krw']-p['time_penalty_krw'])

    def test_unobserved_neighbor_outcome_cannot_be_dropped_to_get_ready(self):
        rows=[label(i) for i in range(60)];rows[-1]['censored']=True
        p=CashModel.fit(rows,UNTIL,cfg(),'exit').predict(action(),UNTIL+1)
        self.assertFalse(p['ready']);self.assertEqual(p['reason'],'unresolved_neighbor_outcomes')

    def test_time_blocks_still_required_and_future_model_rejected(self):
        rows=[label(i) for i in range(60)]
        for i,r in enumerate(rows):r.update(start_ms=10000+i,end_ms=11000+i)
        m=CashModel.fit(rows,UNTIL,cfg(),'exit')
        self.assertFalse(m.predict(action(),UNTIL+1)['ready'])
        self.assertEqual(m.predict(action(),UNTIL)['reason'],'future_model')

    def test_unknown_feature_does_not_silently_become_zero(self):
        a=action();a['x']['external_10_ticks']=None
        # Non-finite/malformed observations are rejected when feature extraction runs.
        s=state();s['reference']['m10']=None
        self.assertIsNone(vector(action(),s,cfg()))


class ExecutionAndHoldTests(unittest.TestCase):
    def test_full_exit_cause_is_not_overwritten_by_zero_quantity_dust(self):
        for reason in ('recovery','stop','timeout'):
            with self.subTest(reason=reason):
                a=Attempt(action(),cfg(),state());a.event(trade(1000300));a.request_exit(1000400,reason)
                a.advance(1001400)
                self.assertEqual(a.result()['reason'],reason)
                self.assertEqual(a.result()['cause'],'recovery' if reason=='recovery' else 'timeout' if reason=='timeout' else 'collapse')

    def test_observation_gap_while_resting_is_not_known_zero_fill(self):
        a=Attempt(action(),cfg(),state());a.advance(1009000)
        r=a.result();self.assertEqual(r['reason'],'no_fill')
        self.assertTrue(r['censored']);self.assertTrue(r['observation_gap']);self.assertIsNone(r['cause'])
        old=OldAttempt(action(),cfg(),state());old.advance(1009000)
        self.assertFalse(old.result()['censored'])

    def test_no_gap_is_inferred_after_confirmed_cancel(self):
        a=Attempt(action(),cfg(),state());a.advance(1000250);a.cancel(1000300);a.advance(1000800)
        a.advance(1020000)
        self.assertFalse(a.result()['censored'])

    def test_minimum_order_is_rechecked_at_sell_arrival(self):
        ac=action();ac['minimum']=80.
        a=Attempt(ac,cfg(),state());a.event(trade(1000300));a.request_exit(1000400,'stop')
        a.event(book(1000500,70.));a.advance(1001400)
        r=a.result();self.assertEqual(r['sold_qty'],0.);self.assertEqual(r['residual_qty'],1.)
        self.assertEqual(r['cash_net_krw'],-100.);self.assertEqual(r['reason'],'residual')

    def test_paired_exits_both_use_arrival_book_and_include_later_failure(self):
        a=Attempt(action(),cfg(),state());a.event(trade(1000300));a.event(book(1000400,100.))
        pair=ExitPair(a,1000450,state(1000450))
        for obj in (a,pair.sell):obj.event(book(1000600,99.))
        pair.sell.advance(1001400)
        self.assertEqual(pair.sell.gross,99.)  # Not the instantaneous100 quote.
        a.event(book(1000900,96.));a.decide(1001000,state(1001000,96.))
        a.event(book(1001100,95.));a.advance(1001800)
        r=pair.result(1001800)
        self.assertFalse(r['censored']);self.assertEqual(r['hold_reason'],'stop')
        self.assertEqual(r['hold_cash_krw']-r['sell_cash_krw'],-4.)

    def test_pair_uses_remaining_inventory_after_partial_sale(self):
        a=Attempt(action(),cfg(),state());a.event(trade(1000300))
        a.sold=.5;a.gross=50.
        pair=ExitPair(a,1000400,state(1000400))
        pair.sell.advance(1001400)
        a.event(book(1000800,96.));a.decide(1000900,state(1000900,96.));a.advance(1001700)
        r=pair.result(1001700)
        self.assertEqual(r['filled_qty'],.5)
        self.assertAlmostEqual(r['sell_cash_krw'],50.)
        self.assertAlmostEqual(r['hold_cash_krw'],48.)

    def test_censoring_is_not_a_stop_or_time_expiry(self):
        a=Attempt(action(),cfg(),state());a.event(trade(1000300))
        r=a.result(1000400)
        self.assertTrue(r['censored']);self.assertIsNone(r['cause'])

    def test_active_inventory_identity_when_observation_goes_stale(self):
        b=Book(cfg(),1000.);b.active=Attempt(action(),cfg(),state());b.active.event(trade(1000300))
        b.active.decide(1002000,None)
        s=state(1002000);s['book_ms']=1000000
        r=b.report(1002000,{'BTC':s})
        self.assertEqual(r['accounting_error_krw'],0.)
        self.assertTrue(r['open_attempt']['censored'])

    def test_less_queue_fill_is_not_a_cash_pnl_lower_bound(self):
        def run(good,queue):
            s=state();s['bids']=[(100.,1.),(99.,10.)]
            a=Attempt(action(),cfg(),s,queue_multiplier=queue)
            a.event(trade(1000300,1.5 if good else 10.))
            a.cancel(1000400)
            a.event(book(1000700,104. if good else 90.))
            a.decide(1001000,state(1001000,104. if good else 90.))
            a.event(book(1001100,104. if good else 90.));a.advance(1002100)
            r=a.result();r['episode_id']='good' if good else 'bad'
            return r
        base=[run(True,1.),run(False,1.)];stress=[run(True,2.),run(False,2.)]
        diag=fill_selection(base,stress)
        self.assertEqual(diag['base_only']['episodes'],1)
        self.assertGreater(diag['base_only']['mean_known_cash_krw'],0.)
        self.assertLess(diag['common_under_stress']['mean_known_cash_krw'],0.)

    def test_terminal_failure_pairs_are_included_in_hold_value(self):
        rows=[]
        for i in range(60):
            r=label(i);r.update(landmark_ms=r['start_ms']+300,x=vector(action(),state(),cfg(),qty=1.,age=1.),
                delta_bp=10. if i%3 else -50.,hold_bp=10010. if i%3 else 9950.,sell_bp=10000.,
                extra_occupied_s=1.)
            rows.append(r)
        h=HazardModel.fit([],rows,UNTIL,cfg())
        p=h.continuation(action(),1.,UNTIL+1,state=state(),qty=1.)
        self.assertTrue(p['ready']);self.assertFalse(p['hold'])
        self.assertLess(p['incremental_mean_krw'],0.)


class ProbabilityAndMetricsTests(unittest.TestCase):
    def test_no_observation_has_no_prior_only_probability_claim(self):
        h=HazardModel.fit([],[],UNTIL,cfg())
        for p in h.survival(action()):self.assertIsNone(p['incidence']);self.assertFalse(p['ready'])

    def test_horizons_include_failures_and_preserve_mass(self):
        rows=[dict(label(i),exit_model='structural',duration_s=1. if i%2 else 10.,
                   cause='recovery' if i%2 else 'collapse') for i in range(60)]
        h=HazardModel.fit(rows,[],UNTIL,cfg())
        points=h.survival(action())
        self.assertGreater(points[2]['incidence']['collapse'],points[0]['incidence']['collapse'])
        for p in points:self.assertAlmostEqual(sum(p['incidence'].values())+p['survival'],1.)

    def test_probability_and_cash_errors_are_separate_and_episode_weighted(self):
        records=[]
        for j in range(6):
            r=label(0);r['action']['id']=str(j)
            records.append(dict(prediction=dict(expected_net_krw=.02,ready=True,p_fill=.8),
                                probabilities=[dict(seconds=2,incidence=dict(recovery=.8,collapse=.2,timeout=0.))],outcome=r))
        r=score_predictions(records)
        self.assertEqual(r['order_cash_decision_ready']['episodes'],1)
        self.assertEqual(r['order_cash_decision_ready']['mae'],0.)
        self.assertAlmostEqual(r['fill_brier']['mse'],.04)
        self.assertFalse(r['probability_calibration_fitted'])


def synthetic(root,base=1000000):
    root.mkdir(parents=True,exist_ok=True)
    public=root/'public.jsonl';leaders=root/'leaders.jsonl.gz';contract=root/'contracts.json'
    with public.open('w') as p,gzip.open(leaders,'wt') as l:
        for second in range(2001):
            at=base+second*1000;shock=any(x<=second<=x+2 for x in (500,1050,1600));bid=96. if shock else 100.
            common=dict(quote_currency='KRW',target_currency='BTC',timestamp=at,id=str(second*2+1))
            data=dict(common,bids=[dict(price=bid,qty=10.)],asks=[dict(price=bid+1,qty=10.)])
            p.write(json.dumps(dict(received_ms=at,message=dict(response_type='DATA',channel='ORDERBOOK',data=data)))+'\n')
            data=dict(common,timestamp=at+100,id=str(second*2+2),price=bid,qty=12. if shock else .5,is_seller_maker=not shock)
            p.write(json.dumps(dict(received_ms=at+100,message=dict(response_type='DATA',channel='TRADE',data=data)))+'\n')
            for venue in ('U','B'):l.write(json.dumps(['b',at,venue,'BTC',at,100.,10.,101.,10.])+'\n')
    contract.write_text(json.dumps(dict(captures=[dict(coin='BTC',available_ms=0,
        contract=dict(qty_unit='.1',min_order_amount='5',max_qty='1000',max_order_amount='1000000',price_unit='1'),
        units=[dict(range_min=0,price_unit=1)])])))
    return dict(coinone=[str(public)],leaders=[str(leaders)],contracts=str(contract),start_ms=base,
                exit_end_ms=base+800000,entry_end_ms=base+1400000,test_start_ms=base+1400000,end_ms=base+2000000)


class PipelineTests(unittest.TestCase):
    def test_chronology_common_candidates_censoring_and_existing_output_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);spec=synthetic(root)
            r=build(spec,root/'out')
            self.assertGreater(r['exit_training']['filled_labels'],0)
            self.assertGreater(r['exit_training']['pairs'],0)
            base=r['scenarios']['base']
            self.assertEqual(base['legacy']['ledger']['attempts'],0)
            self.assertEqual(base['refined']['ledger']['attempts'],0)
            self.assertGreater(base['structural']['ledger']['filled_attempts'],0)
            self.assertAlmostEqual(base['structural']['ledger']['accounting_error_krw'],0.,delta=1e-6)
            self.assertEqual(r['verdict'],'IMPROVEMENT_UNCONFIRMED')
            for variant,names in r['stress_fill_selection'].items():
                for result in names.values():self.assertTrue(result['candidate_keys_equal'])
            for name in ('legacy','refined'):
                labels=json.loads((root/'out'/(name+'-entry-labels.json')).read_text())
                self.assertTrue(all(x['start_ms']>spec['exit_end_ms'] and x['end_ms']<spec['entry_end_ms'] for x in labels))
            with self.assertRaises(FileExistsError):build(spec,root/'out')

    def test_frozen_forward_evaluation_does_not_refit_and_cannot_mix_windows(self):
        from unittest.mock import patch
        from track_c.replay.evidence import read_model, register, evaluate, assess
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);spec=synthetic(root)
            build(spec,root/'out')
            doc,_,_=read_model(root/'out/model.json')
            future=synthetic(root/'future',base=50*DAY)
            start=future['start_ms']+400000
            protocol=register(root/'out/model.json',start,now_ms=spec['end_ms']+1)
            forward={k:future[k] for k in ('coinone','leaders','contracts','end_ms')};forward['start_ms']=start
            with patch.object(CashModel,'fit',side_effect=AssertionError('forward refit forbidden')):
                result=evaluate(root/'out/model.json',protocol,forward,root/'forward')
            self.assertEqual(result['model'],doc['digest'])
            self.assertFalse(result['assessment']['orders_enabled'])
            self.assertIn('window_mismatch',result['assessment']['reasons'])  # Partial future window.
            bad=dict(forward,start_ms=start+1)
            with self.assertRaisesRegex(ValueError,'window'):evaluate(root/'out/model.json',protocol,bad,root/'bad')
            with self.assertRaises(ValueError):register(root/'out/model.json',spec['end_ms'],now_ms=1)


if __name__=='__main__':unittest.main()
