from decimal import Decimal as D
import gzip
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from .c3_observations import Observations
from .c3_live_evidence import asof_queries,audit,books,decompose
from .microstructure import liquidate
from .portfolio import Portfolio
from .store import Store
from .test_c3 import rule_cfg,UNITS,CONTRACT,RestingExchange
from . import rule


def reference(at,price,basis=1.):
    return dict(t_ms=at,fair=dict(evaluated_ms=at,fair=price*basis,
        reference_components=dict(U=dict(price=price,basis_ratio=basis,weight=1.))))


class EvidenceTests(unittest.TestCase):
    def test_vwap_uses_full_order_quantity_and_refuses_missing_depth(self):
        self.assertEqual(liquidate([(100,1),(99,2)],3),298/3)
        self.assertIsNone(liquidate([(100,1)],2))

    def test_asof_never_uses_future_or_stale_book(self):
        rows=[dict(t_ms=1000,coin='BTC',book_ms=1000),dict(t_ms=1300,coin='BTC',book_ms=1300)]
        result=asof_queries(rows,[(1250,'BTC','before'),(1300,'BTC','same'),(3000,'BTC','stale')],age_field='book_ms')
        self.assertEqual(result['before']['t_ms'],1000)
        self.assertEqual(result['same']['t_ms'],1300)
        self.assertIsNone(result['stale'])
        late=[dict(t_ms=1400,coin='BTC',book_ms=1400,exchange_ms=0)]
        self.assertIsNone(asof_queries(late,[(2000,'BTC','late')],age_field=('book_ms','exchange_ms'))['late'])

    def test_premium_change_is_separate_from_external_price_move(self):
        result=decompose(99,104,reference(1000,100,1),reference(2000,102,1.01))
        self.assertAlmostEqual(result['external_change'],2.01)
        self.assertAlmostEqual(result['basis_change'],1.01)
        self.assertAlmostEqual(result['relative_change'],1.98)
        self.assertAlmostEqual(result['identity_error'],0.)
        old=reference(1000,100); old['fair']['evaluated_ms']=-1000
        self.assertIsNone(decompose(99,104,old,reference(2000,102)))

    def test_episode_requires_fresh_reference_and_preserves_nontrigger_pressure(self):
        with tempfile.TemporaryDirectory() as directory:
            observer=Observations(directory)
            prior=dict(t_ms=1000,exchange_ms=999,bids=[(99,1)],asks=[(100,1)],tick=1)
            trade=dict(timestamp=1100,price='99',qty='2',is_seller_maker=False)
            fair=dict(fair=101,dev_ticks=1.5,evaluated_ms=-5000)
            observer.public('BTC',trade,1100,prior,fair)
            self.assertFalse(observer.episodes)
            observer.public('BTC',trade,1200,prior,dict(fair,evaluated_ms=1150))
            self.assertIn('BTC',observer.episodes)
            observer.expire('BTC',34000); self.assertFalse(observer.episodes)
            observer.close()
            self.assertEqual(observer.counts['SELL_PRESSURE'],2)
            self.assertEqual(observer.counts['EPISODE_START'],1)
            self.assertEqual(observer.counts['EPISODE_END'],1)

    def test_actual_pnl_and_extra_costs_are_not_double_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            now=[1788629000.]
            with patch('track_c.store.time.time',side_effect=lambda:now[0]):
                store=Store(directory); client=RestingExchange(); cfg=rule_cfg()
                pf=Portfolio(cfg,client,store,clock=lambda:now[0]); pf.sync_cash(D(300000))
                plan=rule.assess(cfg,coin='BTC',bid=990,ask=991,tick=1,dev=1,contract=CONTRACT,units=UNITS,cash=D(300000),risk_remaining=D(1000),m30=0,m10=0,flow32=0)['plan']
                pf.enter('BTC',plan,{},'5000')
                entry=client.submissions[-1]
                now[0]+=1; client.fill(entry['cid'],plan['qty'],'990')
                pf.book('BTC').drive(bid=990,fresh=True,quantitative_decision=dict(hold=True))
                take=client.submissions[-1]
                now[0]+=1; client.fill(take['cid'],plan['qty'],'991')
                pf.book('BTC').drive(bid=991,fresh=True,quantitative_decision=dict(hold=True))
                store.close()
            public=Path(directory)/'public'; public.mkdir()
            with gzip.open(public/'20260905-17.jsonl.gz','wt',encoding='utf-8') as stream:
                for n in range(35):
                    at=1788629000000+n*1000
                    data=dict(timestamp=at,id=n,quote_currency='KRW',target_currency='BTC',bids=[dict(price='991',qty='1000')],asks=[dict(price='992',qty='1000')])
                    stream.write(json.dumps(dict(received_ms=at,message=dict(response_type='DATA',channel='ORDERBOOK',data=data)))+'\n')
            report=audit(directory,start_ms=1788629000000,end_ms=1788629034000)
            self.assertEqual(report['attempts'],1); self.assertEqual(report['filled_attempts'],1)
            self.assertEqual(report['closed'],1); self.assertEqual(report['no_fill'],0)
            self.assertEqual(D(report['realized_krw']),D(plan['qty']))
            self.assertAlmostEqual(report['extra_cost_stress_realized_krw']['1'],float(plan['qty'])-report['turnover_krw']/10000)
            self.assertEqual(report['displayed_liquidation_markout_bp']['filled_250ms']['n'],1)
            self.assertEqual(report['current_owned_inventory'],[])


if __name__=='__main__': unittest.main()
