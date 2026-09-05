from copy import deepcopy
from decimal import Decimal as D
import json
import tempfile
import unittest

from .portfolio import Portfolio
from .store import Store
from .test_runtime import FakeExchange,cfg
from .notices import Replay,krw
from .estimation import predict,predict_one
from .test_quantitative import fake_model, snapshot
from .outcomes import action_features


class Exchange(FakeExchange):
    def balances(self): return [dict(currency=c,available=str(self.inventory),limit='0') for c in ('BTC','ETH','SOL')]


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name); self.addCleanup(self.store.close)
        self.client=Exchange(); self.now=[1788595000.]
        self.config=cfg(); self.config['policy']='quantitative'
        self.p=Portfolio(self.config,self.client,self.store,clock=lambda:self.now[0]); self.p.sync_cash(300000)
        self.plan=dict(reason=None,qty='100',entry='1000',stop='999',stop_limit='998',maker='0',taker='0',policy='quantitative',
                       entry_ttl_s=4,hold_limit_s=12,model='immutable',take_profit='1001',research=True)
    def enter(self,coin):
        self.assertTrue(self.p.enter(coin,deepcopy(self.plan),{},'5000')); return self.client.submissions[-1]['cid']
    def test_concurrent_cash_reservation_and_duplicate_symbol(self):
        self.enter('BTC'); self.enter('ETH')
        self.assertEqual(self.p.reserved_cash(),D(200000))
        self.assertFalse(self.p.enter('SOL',self.plan,{},'5000'))
        self.assertFalse(self.p.enter('BTC',self.plan,{},'5000'))
        self.assertEqual(self.p.equity,D(300000)); self.assertEqual(len(self.p.campaigns),2)
    def test_shared_risk_cap_is_not_multiplied_by_symbols(self):
        self.plan.update(qty='10',stop='966',stop_limit='965')
        self.enter('BTC'); self.enter('ETH')
        self.assertFalse(self.p.enter('SOL',self.plan,{},'5000'))
        self.assertEqual(self.p.committed_risk(),D(700))
    def test_interleaved_fills_restart_and_close_keep_other_position(self):
        a=self.enter('BTC'); b=self.enter('ETH')
        self.client.fill(b,'100','1000'); self.client.fill(a,'100','1000')
        self.p.book('ETH').drive(bid=1001,fresh=True); self.p.book('BTC').drive(bid=1001,fresh=True)
        self.assertEqual(D(self.p.state['cash_krw']),100000)
        self.p=Portfolio(self.config,self.client,self.store,clock=lambda:self.now[0])
        self.assertEqual(self.p.equity,D(300200))
        self.p.book('BTC').drive(bid=1001,fresh=True,quantitative_decision=dict(hold=True,take_profit=True))
        sell=self.client.submissions[-1]; self.assertEqual(sell['limit_price'],'1001')
        self.p.book('BTC').drive(bid=1001,fresh=True)
        self.assertEqual(set(self.p.campaigns),{'ETH'})
        self.assertTrue(self.p.book('ETH').active('protect'))
        self.assertEqual(D(self.p.state['realized']),100)
        rows=self.store.db.execute('select seq,t_ms,kind,body from events order by seq').fetchall()
        replay=Replay(); facts=[replay.apply((s,t,k,json.loads(b))) for s,t,k,b in rows]
        self.assertEqual([r['coin'] for r in facts if r and r['type']=='fill'],['ETH','BTC','BTC'])
    def test_protection_races_profit_exit_and_only_remaining_qty_is_sold(self):
        a=self.enter('BTC'); self.client.fill(a,'100','1000'); self.p.book('BTC').drive(bid=1001,fresh=True)
        self.client.cancel_race=True
        self.p.book('BTC').drive(bid=1001,fresh=True,quantitative_decision=dict(hold=True,take_profit=True))
        self.assertEqual(self.client.submissions[-1]['qty'],'60')
        self.p.book('BTC').drive(bid=1001,fresh=True)
        self.assertEqual(self.client.inventory,0); self.assertEqual(D(self.p.state['realized']),20)
    def test_lost_entry_response_reserves_cash_across_restart(self):
        self.client.fail='lost_response'; self.enter('BTC')
        restarted=Portfolio(self.config,self.client,self.store,clock=lambda:self.now[0])
        self.assertEqual(restarted.reserved_cash(),100000)
        self.assertFalse(restarted.enter('BTC',self.plan,{},'5000'))
        self.assertEqual(len(self.client.submissions),1)
    def test_risk_exit_overrides_a_pending_price_limited_profit_exit(self):
        a=self.enter('BTC'); self.client.fill(a,'100','1000'); book=self.p.book('BTC')
        book.drive(bid=1001,fresh=True)
        book.request_exit('one_tick_profit'); book.request_exit('premise')
        book.drive(bid=998,fresh=True)
        self.assertEqual(book.campaign['exit_reason'],'premise')
        self.assertNotIn('limit_price',self.client.submissions[-1])
    def test_whole_won_is_display_only_and_does_not_create_negative_zero(self):
        self.assertEqual(krw('594574.7182'),'594,575원')
        self.assertEqual(krw('-.1',True),'+0원'); self.assertEqual(krw('-1.5',True),'-2원')
    def test_vectorized_inference_preserves_scalar_model(self):
        model=fake_model(2); x=action_features(snapshot(),52,4,8,1)
        a,b=predict(model,x),predict_one(model,x)
        for k in ('mean','lower','upper','se','distance'): self.assertAlmostEqual(a[k],b[k])


if __name__=='__main__': unittest.main()
