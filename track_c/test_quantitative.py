from copy import deepcopy
from decimal import Decimal as D
import json
import math
from pathlib import Path
import tempfile
import unittest

from .microstructure import FEATURES, Micro, SCHEMA, liquidate
from .outcomes import ACTION_FEATURES, Path as MarketPath, action_features
from .estimation import NAMES, digest, fit, predict, purged, validate_artifact
from .policy import Policy
from .universe import asset_reason, coverage


def snapshot(t=200000):
    f={k:0. for k in FEATURES}
    f.update(spread_bp=10.,tick_bp=10.,spread_ticks=1.,log_volume_32=math.log1p(1000000),fresh_fraction=1.)
    return dict(t=t,coin='BTC',schema=SCHEMA,bid=100.,ask=100.1,tick=.1,bids=[(100.,10000.),(99.9,10000.)],
                asks=[(100.1,10000.)],features=f,history_s=180,minimum=5000,qty_step='.01')


def fake_model(mean, binary=False):
    n=len(NAMES)
    return dict(binary=binary,center=[0.]*n,scale=[100.]*n,coef=[mean]+[0.]*n,
                inverse=[[1e-10 if i==j else 0. for j in range(n+1)] for i in range(n+1)],
                variance=1e-10,cluster_se=1e-5,residuals=[-1.,0.,1.] if not binary else [0.],
                support_distance=1e8,calibration=[],target='net_bp',penalty=10)


def artifact(state='validated'):
    doc=dict(version=1,features=list(NAMES),feature_schema=SCHEMA,feature_windows=[2,8,32,120],trained_until=1,state=state,
             quantity_support_krw=[5000,200000],ttl_grid=[1,4],horizon_grid=[2,8],confidence_alpha=.05,
             models=dict(fill=fake_model(4,True),net=fake_model(20),adverse=fake_model(math.log1p(10)),continuation=fake_model(2)))
    doc['models']['adverse']['residuals']=[0.]
    doc['digest']=digest(doc)
    return doc


class MicroTests(unittest.TestCase):
    def book(self,t=1000,identity=1,bid='100',ask='100.1'):
        return dict(quote_currency='KRW',target_currency='BTC',timestamp=t,id=identity,
                    bids=[dict(price=bid,qty='100')],asks=[dict(price=ask,qty='100')])

    def test_receive_causality_duplicate_and_snapshot_immutability(self):
        m=Micro('BTC'); b=self.book(); self.assertIsNotNone(m.feed('ORDERBOOK',b,1000))
        first=m.snapshot(1001,.1)
        self.assertIsNone(m.feed('ORDERBOOK',b,1002))
        m.feed('ORDERBOOK',self.book(2000,2,'110','110.1'),2000)
        self.assertEqual(first['bid'],100.)
        self.assertIsNone(m.snapshot(1900,.1))
        self.assertIsNone(m.feed('ORDERBOOK',self.book(1500,3),1900))

    def test_future_stale_crossed_and_nonfinite_are_rejected(self):
        for b,recv in ((self.book(10000),1000),(self.book(),3000),(self.book(bid='101'),1000),(self.book(bid='nan'),1000)):
            m=Micro('BTC'); self.assertIsNone(m.feed('ORDERBOOK',b,recv)); self.assertFalse(m.bids)

    def test_ofi_sign_and_trade_deduplication(self):
        m=Micro('BTC'); m.feed('ORDERBOOK',self.book(),1000)
        b=self.book(2000,2); b['bids'][0]['qty']='200'; m.feed('ORDERBOOK',b,2000)
        self.assertGreater(m.snapshot(2000,.1)['features']['ofi_2'],0)
        trade=dict(quote_currency='KRW',target_currency='BTC',timestamp=2001,id='a',qty='2',price='100',is_seller_maker=False)
        m.feed('TRADE',trade,2001); self.assertIsNone(m.feed('TRADE',trade,2002))
        self.assertEqual(m.snapshot(2002,.1)['features']['flow_2'],-1)

    def test_depth_walk_does_not_invent_liquidity(self):
        self.assertAlmostEqual(liquidate([(100,2),(99,3)],4),99.5)
        self.assertIsNone(liquidate([(100,2)],3))


class OutcomeTests(unittest.TestCase):
    def path(self, trades=(), holes=()):
        rows=[dict(t=t,exchange_t=t,kind='book',bids=[(100.,100.)],asks=[(100.1,100.)]) for t in range(0,15001,500) if t not in holes]
        rows+=list(trades); return MarketPath(sorted(rows,key=lambda row:row['t']))

    def test_queue_ahead_must_be_consumed_before_our_fill(self):
        s=snapshot(1000); s['bids']=[(100.,100.)]; s['minimum']=1
        partial=dict(t=1800,kind='trade',price=100.,qty=90.,buy=False)
        self.assertEqual(self.path([partial]).label(s,10,2,2)['filled'],0.)
        next_trade=dict(t=2200,kind='trade',price=100.,qty=20.,buy=False)
        label=self.path([partial,next_trade]).label(s,10,2,2)
        self.assertEqual(label['fill_fraction'],1.)
        self.assertEqual(label['fill_t'],2200)

    def test_exit_begins_after_fill_and_fee_not_midprice_profit(self):
        s=snapshot(1000); s['minimum']=1
        trade=dict(t=2200,kind='trade',price=100.,qty=110.,buy=False)
        label=self.path([trade]).label(s,10,2,4,maker=.001,taker=.001)
        self.assertEqual(label['end'],6450)
        self.assertAlmostEqual(label['net_bp'],-20.)

    def test_partial_dust_is_never_booked_as_a_successful_liquidation(self):
        trade=dict(t=2200,kind='trade',price=100.,qty=105.,buy=False)
        label=self.path([trade]).label(snapshot(1000),10,2,2)
        self.assertTrue(label['dust']); self.assertEqual(label['fill_fraction'],.5); self.assertEqual(label['net_bp'],-5000)

    def test_gaps_and_right_censoring_are_not_unfilled_negatives(self):
        s=snapshot(1000)
        path=self.path(holes=set(range(1500,5000,500)))
        self.assertIsNotNone(path.label(s,10,4,2)['censored'])
        self.assertIsNotNone(self.path().label(snapshot(14000),10,2,2)['censored'])

    def test_buy_trades_cannot_fill_our_passive_buy(self):
        trade=dict(t=2200,kind='trade',price=100.,qty=10000.,buy=True)
        self.assertEqual(self.path([trade]).label(snapshot(1000),10,2,2)['filled'],0)


class EstimationTests(unittest.TestCase):
    def test_purge_uses_label_end_and_all_symbols_share_time_boundary(self):
        rows=[dict(t=0,end=80,coin='A'),dict(t=10,end=101,coin='B'),dict(t=100,end=120,coin='A'),dict(t=120,end=150,coin='B')]
        train,test=purged(rows,100,150,10)
        self.assertEqual(train,[rows[0]]); self.assertEqual(test,[rows[2]])

    def test_fit_is_finite_and_effective_samples_are_time_blocks(self):
        data=[]
        for i in range(80):
            x={k:0. for k in NAMES}; x['flow_2']=(i%10)/10
            data.append(dict(t=i*1000,x=x,y=2*x['flow_2']+1))
        model=fit(data,'y',penalty=1)
        self.assertEqual(model['blocks'],2)
        self.assertTrue(math.isfinite(predict(model,data[0]['x'])['mean']))
        copied=fit(data*5,'y',penalty=1)
        self.assertEqual(copied['blocks'],2)
        self.assertAlmostEqual(copied['coef'][1+NAMES.index('flow_2')],model['coef'][1+NAMES.index('flow_2')])

    def test_artifact_tamper_future_and_dimensions_fail_closed(self):
        good=artifact(); validate_artifact(good,now_ms=1000)
        tampered=deepcopy(good); tampered['models']['net']['coef'][0]=999
        with self.assertRaises(ValueError): validate_artifact(tampered)
        with self.assertRaises(ValueError): validate_artifact(good,now_ms=1)
        bad=deepcopy(good); bad['models']['net']['scale'][0]=0; bad['digest']=digest(bad)
        with self.assertRaises(ValueError): validate_artifact(bad)


class PolicyTests(unittest.TestCase):
    def args(self):
        return dict(snapshot=snapshot(),contract=dict(qty_unit='.01',min_order_amount='5000',max_qty='1000000',max_order_amount='100000000'),
                    units=[dict(range_min='0',price_unit='.1')],fees=dict(maker='0',taker='0'),equity=D(500000),cash=D(500000),risk_remaining=D(7500))

    def config(self): return dict(risk_fraction='.0025',cash_fraction='.95',learning_enabled=True)

    def test_positive_supported_model_selects_feasible_size_and_negative_model_waits(self):
        doc=artifact(); p=Policy(doc,self.config()); result=p.assess(**self.args())
        self.assertTrue(result['accepted'])
        plan=result['plan']; self.assertLessEqual(D(plan['nominal_loss_krw']),D(1250))
        self.assertGreaterEqual(D(plan['qty'])*D(plan['stop_limit']),D(5000))
        doc['models']['net']['coef'][0]=-20
        self.assertFalse(Policy(doc,self.config()).assess(**self.args())['accepted'])

    def test_unsupported_stable_missing_and_risk_exhaustion_refuse(self):
        for change in ('stable','risk','missing','unsupported'):
            doc=artifact(); args=self.args()
            if change=='stable': args['snapshot']['coin']='USDT'
            elif change=='risk': args['risk_remaining']=0
            elif change=='missing': doc['models']['net']=None
            else: doc['models']['net']['support_distance']=1e-10
            self.assertFalse(Policy(doc,self.config()).assess(**args)['accepted'])

    def test_research_is_explicit_and_spent_learning_budget_blocks_more(self):
        p=Policy(artifact('research'),self.config()); result=p.assess(**self.args())
        self.assertTrue(result['accepted']); self.assertTrue(result['plan']['research'])
        self.assertLess(float(result['plan']['notional_krw']),11000)
        self.assertFalse(p.assess(**self.args(),learning_spent=1250)['accepted'])

    def test_continuation_ignores_entry_price_and_exits_negative_value(self):
        doc=artifact(); p=Policy(doc,self.config())
        self.assertTrue(p.continuation(snapshot(),100)['hold'])
        doc['models']['continuation']['coef'][0]=-2
        self.assertFalse(p.continuation(snapshot(),100)['hold'])

    def test_fees_are_not_silently_assumed_zero(self):
        args=self.args(); args['fees']['taker']='.001'
        self.assertEqual(Policy(artifact(),self.config()).assess(**args)['reason'],'unreconciled_fee_currency')


class UniverseTests(unittest.TestCase):
    def test_stables_do_not_take_slots_and_foreign_inventory_is_not_adopted(self):
        coins=['USDT','USDC','RLUSD','BTC','ETH','DOGE','XRP']
        contracts=[dict(target_currency=c,trade_status=1,maintenance_status=0,order_types=['limit','market','stop_limit']) for c in coins]
        tickers=[dict(target_currency=c,quote_volume=1000-i) for i,c in enumerate(coins)]
        chosen,why=coverage(contracts,tickers,foreign=['XRP'],limit=20)
        self.assertEqual(chosen,['BTC','ETH','DOGE']); self.assertEqual(why['USDT'],'pegged_asset')
        self.assertEqual(why['XRP'],'external_ownership')
        self.assertIsNone(asset_reason('DOGE'))


if __name__=='__main__': unittest.main()
