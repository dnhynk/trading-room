import unittest
from copy import deepcopy
from track_c.market.microstructure import Micro,FEATURES,SCHEMA,liquidate
def snapshot(t=200000):
    f={k:0. for k in FEATURES}
    f.update(spread_bp=10.,tick_bp=10.,spread_ticks=1.,log_volume_32=math.log1p(1000000),fresh_fraction=1.)
    return dict(t=t,coin='BTC',schema=SCHEMA,bid=100.,ask=100.1,tick=.1,bids=[(100.,10000.),(99.9,10000.)],
                asks=[(100.1,10000.)],features=f,history_s=180,minimum=5000,qty_step='.01')

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
