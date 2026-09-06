"""Live adapter tests use a fake exchange; no credentials or real orders."""
from copy import deepcopy
from decimal import Decimal as D
import tempfile
import unittest

from track_c.live import choose, plan_for, LivePortfolio, LiveRunner, Journal
from tests.c.test_learning import state, action
from tests.c.test_market import cfg as research_cfg
from tests.c.test_execution import FakeExchange, cfg
from track_c.ops.store import Store


class UnsupportedModel:
    def predict(self,*args):return dict(ready=False,reason='joint_support',score_krw=None)


class DecisionModeTests(unittest.TestCase):
    def test_unavailable_reference_cannot_disconnect_the_public_feed(self):
        from track_c.ops.observations import Observations
        with tempfile.TemporaryDirectory() as folder:
            r=LiveRunner.__new__(LiveRunner);r.storage_ok=True;r.observations=Observations(folder)
            r.last_fair={'BTC':dict(ready=False,reason='reference_unavailable',evaluated_ms=1000)}
            r.observe_public('BTC',dict(is_seller_maker=False,price=100,qty=2,timestamp=1000),1000,
                             dict(t_ms=1000,bids=[(100,1)],asks=[(101,1)],tick=1))
            self.assertTrue(r.storage_ok);self.assertEqual(r.observations.counts['SELL_PRESSURE'],1)
            r.observations.close()

    def test_paused_episode_is_not_replayed_after_activation(self):
        import asyncio
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            r=LiveRunner.__new__(LiveRunner);r.sample_fairs=lambda:None
            r.c4markets={'BTC':None};r.markets={'BTC':None};r.c4pending={'BTC':'before-start'}
            r.c4states={'BTC':state()};r.last_decided={};r.directory=Path(folder)
            r.cfg=dict(mode='live',funding_confirmed=True)
            (r.directory/'PAUSE').write_text('test')
            asyncio.run(r.decisions());self.assertEqual(r.last_decided['BTC'],'before-start')
            (r.directory/'PAUSE').unlink();asyncio.run(r.decisions())
            self.assertEqual(r.selection['BTC']['reason'],'waiting_new_episode')

    def test_known_disconnect_invalidates_local_history_and_pending_episode(self):
        from track_c.market.state import Market
        r=LiveRunner.__new__(LiveRunner);r.c4cfg=research_cfg()
        m=Market('BTC',r.c4cfg);r.c4markets={'BTC':m}
        r.c4pending={'BTC':'pending'};r.c4states={'BTC':state()}
        m.reference.history.append((1,100));m.sells.append((1,1));m.episode={'id':'pending'}
        r.connected=True;r.connected=False
        self.assertFalse(m.reference.history);self.assertFalse(m.sells)
        self.assertIsNone(m.episode);self.assertFalse(r.c4pending);self.assertIsNone(r.c4states['BTC'])

    def test_start_identifies_c4_without_rewriting_the_shared_loop(self):
        from unittest.mock import Mock
        store=Mock();j=Journal(store,'frozen','learned')
        j.event('START',policy='rule',rule='c3',code='shared')
        body=store.event.call_args.kwargs
        self.assertEqual(body['policy'],'c4');self.assertEqual(body['model'],'frozen')
        self.assertFalse(body['automatic_retraining']);self.assertEqual(body['shared_execution_code'],'shared')

    def test_explicit_sampling_and_learned_are_distinct(self):
        s=state();model=UnsupportedModel()
        sampled=choose(s,research_cfg(),10000.,100.,model,'structural_sampling')
        learned=choose(s,research_cfg(),10000.,100.,model,'learned')
        self.assertTrue(sampled['accepted']);self.assertFalse(learned['accepted'])
        self.assertEqual(sampled['action']['id'],'0:minimum')
        self.assertFalse(sampled['prediction']['ready'])

    def test_sampling_does_not_bypass_price_freshness_or_risk(self):
        model=UnsupportedModel()
        for s,risk in [(dict(state(),entry_fresh=False),100.),(state(),.00001)]:
            self.assertFalse(choose(s,research_cfg(),10000.,risk,model,'structural_sampling')['accepted'])
        s=state();s['reference']['dev_ticks']=1.9
        self.assertFalse(choose(s,research_cfg(),10000.,100.,model,'structural_sampling')['accepted'])
        with self.assertRaises(ValueError):choose(state(),research_cfg(),10000.,100.,model,'auto')

    def test_plan_uses_taker_exit_native_stop_and_original_ttl(self):
        a=action();p=plan_for(a,state(),'frozen','learned')
        self.assertEqual(p['take_mode'],'market')
        self.assertEqual(p['stop'],str(D(str(a['stop']))))
        self.assertEqual(p['entry_ttl_s'],8)
        self.assertEqual(p['hold_limit_s'],180)
        self.assertEqual(p['model'],'frozen')


class LiveOMSTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.addCleanup(self.store.close)
        self.client=FakeExchange();self.now=[1788595000.]
        self.config=cfg();self.config.update(policy='rule',entry_ttl_s=8,hold_s=180)
        self.p=LivePortfolio(self.config,self.client,self.store,clock=lambda:self.now[0]);self.p.sync_cash(300000.)
        self.plan=dict(reason=None,qty='10',entry='1000',stop='999',stop_limit='998',maker='0',taker='0',
                       policy='rule',entry_ttl_s=8,hold_limit_s=180,model='c4-test',take_mode='market',
                       take_profit='1001',research=True,c4_action=dict(action(),qty=10.,price=1000.,notional=10000.),
                       c4_mode='learned',c4_prediction=dict(ready=True,expected_net_krw=10.,p_fill=.8))

    def enter(self):
        self.assertTrue(self.p.enter('BTC',deepcopy(self.plan),{},'5000'))
        return self.client.submissions[-1]['cid']

    def live_runner(self,s):
        import time
        from types import SimpleNamespace
        from collections import Counter
        from unittest.mock import Mock
        r=LiveRunner.__new__(LiveRunner);r.sample_fairs=lambda:None
        r.c4cfg=research_cfg();r.c4markets={'BTC':None}
        r.cfg=dict(self.config,c4_live_mode='structural_sampling')
        r.c4pending={'BTC':'decision-ep'};s.update(episode_id='decision-ep',t_ms=time.time_ns()//1000000)
        r.c4states={'BTC':s};r.current_state=lambda coin:s;r.last_decided={}
        r.directory=self.store.directory;r.oms=self.p;r.store=self.store
        r.models={'refined':(SimpleNamespace(survival=lambda a:[]),UnsupportedModel())}
        r.markets={'BTC':SimpleNamespace(fees={'maker':0,'taker':0},contract=s['contract'])}
        r.account_at=time.time();r.foreign_assets=set();r.connected=True;r.private_connected=True
        r.storage_ok=True;r.stopping=False;r.counts=Counter();r.cash=lambda:D('10000')
        r.artifact={'digest':'c4-test'}
        return r

    def test_real_decision_path_journals_rejection_without_an_exchange_order(self):
        import asyncio,json
        s=state();s['reference'].update(ready=False,reason='reference_unavailable')
        r=self.live_runner(s);asyncio.run(r.decisions())
        body=json.loads(self.store.db.execute("select body from events where kind='C4_DECISION'").fetchone()[0])
        self.assertEqual(body['market_state']['episode_id'],'decision-ep')
        self.assertFalse(body['decision']['accepted']);self.assertFalse(self.client.submissions)

    def test_real_decision_path_to_fake_exchange_intent_preserves_order_prediction(self):
        import asyncio,json
        r=self.live_runner(state());asyncio.run(r.decisions())
        self.assertEqual(len(self.client.submissions),1)
        self.assertEqual(self.client.submissions[0]['type'],'LIMIT')
        intent=json.loads(self.store.db.execute("select body from events where kind='CAMPAIGN_INTENT'").fetchone()[0])
        self.assertEqual(intent['plan']['c4_action']['id'],'0:minimum')
        self.assertEqual(intent['plan']['model'],'c4-test')
        self.assertFalse(intent['plan']['c4_prediction']['ready'])
        self.assertEqual(intent['plan']['c4_mode'],'structural_sampling')

    def test_fill_establishes_native_stop_not_resting_take(self):
        cid=self.enter();self.client.fill(cid,'10','1000');book=self.p.book('BTC')
        book.drive(bid=1000,fresh=True)
        self.assertEqual(self.client.submissions[-1]['type'],'STOP_LIMIT')
        self.assertEqual(len(book.active('protect')),1);self.assertFalse(book.active('take'))

    def test_profit_cancel_settles_stop_before_market_exit(self):
        cid=self.enter();self.client.fill(cid,'10','1000');book=self.p.book('BTC')
        book.drive(bid=1000,fresh=True)
        book.drive(bid=1001,fresh=True,quantitative_decision=dict(hold=True,take_profit=True))
        self.assertEqual(self.client.submissions[-1]['type'],'MARKET')
        self.assertEqual(self.client.submissions[-1]['limit_price'],'1001')
        self.assertTrue(self.client.cancels)
        book.drive(bid=1001,fresh=True)
        self.assertFalse(self.p.campaigns)

    def test_small_partial_keeps_cost_and_adds_to_existing_owned_dust(self):
        self.p.state['residuals']['BTC']=dict(qty='.1',cost='100',mark='1000',mark_at=self.now[0],t=self.now[0])
        cid=self.enter();self.client.fill(cid,'1','1000',status='LIVE')
        self.now[0]+=9;book=self.p.book('BTC')
        book.drive(bid=1000,fresh=True,quantitative_decision=dict(hold=True,cancel_entry=True))
        # Reconciliation of the cancelled entry exposes the remaining subminimum inventory.
        book.drive(bid=1000,fresh=True)
        self.assertFalse(self.p.campaigns)
        r=self.p.state['residuals']['BTC']
        self.assertEqual(D(r['qty']),D('1.1'));self.assertEqual(D(r['cost']),D('1100'))
        self.assertFalse(any(o['side']=='SELL' for o in self.client.submissions))
        self.assertGreaterEqual(self.p.committed_risk(),D('1100'))

    def test_uncertain_entry_does_not_submit_twice(self):
        self.client.fail='lost_response';self.enter()
        self.assertFalse(self.p.enter('BTC',self.plan,{},'5000'))
        self.assertEqual(len(self.client.submissions),1)

    def test_cancel_race_rechecks_subminimum_remainder_before_a_new_sell(self):
        self.enter();book=self.p.book('BTC')
        # Model the reconciled state after a protective cancellation raced a fill.
        for o in book.active():o['status']='CANCELED'
        book.campaign.update(qty='1',cost='1000',mark='1000',first_fill=self.now[0])
        book.submit('exit','SELL','MARKET','1')
        self.assertFalse(self.p.campaigns)
        self.assertEqual(D(self.p.state['residuals']['BTC']['qty']),D('1'))
        self.assertFalse(any(o['side']=='SELL' for o in self.client.submissions))

    def test_stop_below_minimum_requests_exit_without_an_invalid_order(self):
        self.enter();book=self.p.book('BTC')
        for o in book.active():o['status']='CANCELED'
        book.campaign.update(qty='5',cost='5000',mark='1000',first_fill=self.now[0])
        book.submit('protect','SELL','STOP_LIMIT','5',price='998',trigger_price='999')
        self.assertEqual(book.campaign['exit_reason'],'protection_below_minimum')
        self.assertEqual(len(self.client.submissions),1)

    def evidence(self):
        from track_c.ops.live_evidence import read
        return read(self.store.directory/'ledger.sqlite',0,2000000000000,'c4-test')

    def test_actual_evidence_has_unfilled_orders_and_unknown_orders(self):
        self.enter();r=self.evidence()
        self.assertEqual(r['attempts'],1);self.assertEqual(r['unknown_attempts'],1)
        self.assertEqual(r['predictions']['fill_brier']['episodes'],0)
        self.now[0]+=9;self.p.book('BTC').drive(bid=1000,fresh=True)
        r=self.evidence();self.assertEqual(r['unknown_attempts'],0)
        self.assertEqual(r['cash_change_krw'],0)
        self.assertAlmostEqual(r['predictions']['fill_brier']['mse'],.64)

    def test_actual_evidence_keeps_partial_cost_and_does_not_call_dust_a_sale(self):
        cid=self.enter();self.client.fill(cid,'1','1000',status='LIVE')
        self.now[0]+=9;book=self.p.book('BTC');book.drive(bid=1000,fresh=True)
        book.drive(bid=1000,fresh=True)
        r=self.evidence()
        self.assertEqual(r['filled_attempts'],1);self.assertEqual(r['partial_entry_attempts'],1)
        self.assertEqual(r['cash_change_krw'],-1000)
        self.assertEqual(r['remaining_inventory_cost_krw'],1000)
        self.assertEqual(r['outcomes'][0]['residual_qty'],1)
        self.assertEqual(r['realized_pnl_krw'],0)

    def test_actual_evidence_reconciles_roundtrip_cash_and_latency(self):
        cid=self.enter();self.client.fill(cid,'10','1000');book=self.p.book('BTC')
        book.drive(bid=1000,fresh=True)
        book.drive(bid=1001,fresh=True,quantitative_decision=dict(hold=True,take_profit=True))
        book.drive(bid=1001,fresh=True)
        r=self.evidence()
        self.assertEqual(r['remaining_inventory_cost_krw'],0)
        self.assertEqual(r['cash_change_krw'],r['realized_pnl_krw'])
        self.assertIn('entry_submit_roundtrip_ms',r['latency'])
        self.assertEqual(r['unknown_attempts'],0)
        self.assertFalse(r['predictions']['probability_calibration_fitted'])


if __name__=='__main__':unittest.main()
