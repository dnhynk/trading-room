from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from .dataset import asof,public_contracts
from .feedback import extract
from .simulation import Exchange
from .outcomes import Path as Tape
from .test_quantitative import snapshot
from .store import Store


class ResearchTests(unittest.TestCase):
    def test_metadata_uses_last_known_contract_and_not_future_ladder(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'contracts.json'
            p.write_text(json.dumps(dict(captures=[dict(coin='BTC',available_ms=t,contract=dict(price_unit=str(v)),units=[dict(range_min='0',price_unit=str(v))]) for t,v in ((1000,1),(2000,10))])))
            contracts,units=public_contracts(p)
            self.assertIsNone(asof(contracts,'BTC',999))
            self.assertEqual(asof(units,'BTC',1999)['rows'][0]['price_unit'],'1')
            self.assertEqual(asof(units,'BTC',2000)['rows'][0]['price_unit'],'10')
    def test_simulated_maker_queue_and_bounded_profit_sale_are_executable(self):
        books=[dict(t=t,kind='book',bids=[(1000 if t<2000 else 1001,100)],asks=[(1002,100)]) for t in range(0,5001,100)]
        tape=Tape(books); clock=[0.]; ex=Exchange({'BTC':tape},lambda:clock[0],latency=250)
        order=dict(cid='tc-entry-test123',coin='BTC',role='entry',side='BUY',type='LIMIT',qty='10',price='1000')
        ex.submit(order); ex.settle(250)
        ex.event('BTC',dict(t=500,kind='trade',price=1000,qty=105,buy=False))
        self.assertEqual(D(ex.detail('BTC',order['cid'])['executed_qty']),5)
        ex.event('BTC',dict(t=600,kind='trade',price=1000,qty=5,buy=False))
        self.assertEqual(ex.detail('BTC',order['cid'])['status'],'FILLED')
        clock[0]=2.; sell=dict(cid='tc-exit-test1234',coin='BTC',role='exit',side='SELL',type='MARKET',qty='10',limit_price='1001')
        ex.submit(sell); ex.settle(2250)
        self.assertEqual(D(ex.detail('BTC',sell['cid'])['executed_qty']),10)
        self.assertEqual(ex.inventory['BTC'],0); self.assertEqual(ex.cash,D(594584))
    def test_mature_feedback_excludes_open_attempts_and_deduplicates_close(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            snap=snapshot(); plan=dict(qty='100',entry='100',entry_ttl_s=4,horizon_s=8,target_ticks=1,model='x',p_fill=.1,expected_net_bp=2,policy='quantitative')
            store.event('MODEL_DECISION',coin='BTC',snapshot=snap,plan=plan)
            store.event('CAMPAIGN_INTENT',coin='BTC',plan=plan)
            camp=dict(id='done',coin='BTC',bought='100',net='5')
            store.event('CLOSE',campaign=camp); store.event('CLOSE',campaign=camp)
            store.event('MODEL_DECISION',coin='ETH',snapshot=dict(snap,coin='ETH'),plan=plan)
            store.event('CAMPAIGN_INTENT',coin='ETH',plan=plan)
            rows,report=extract(directory,9999999999999)
            self.assertEqual(len(rows),1); self.assertEqual(rows[0]['source'],'exchange_completed')
            self.assertEqual(report['open_attempts_excluded'],1); self.assertEqual(report['completed_attempts'],1)
            store.close()
    def test_artifact_nonfinite_uncertainty_is_rejected(self):
        from .estimation import digest,validate_artifact
        from .test_quantitative import artifact
        d=artifact(); d['models']['net']['cluster_se']=-1; d['digest']=digest(d)
        with self.assertRaises(ValueError): validate_artifact(d)
    def test_zero_fills_and_small_constant_sample_cannot_promote_capital(self):
        from .train import live_evidence
        rows=[dict(t=i*60000,net_bp=0.,filled=0.) for i in range(100)]
        self.assertFalse(live_evidence(rows,4,9)['eligible'])
        tiny=[dict(t=i*60000,net_bp=.1,filled=1.) for i in range(2)]
        self.assertFalse(live_evidence(tiny,4,9)['eligible'])
        earned=[dict(t=i*60000,net_bp=2.+(-1)**i,filled=1.) for i in range(200)]
        self.assertTrue(live_evidence(earned,4,9)['eligible'])
    def test_api_budget_waits_before_exceeding_shared_read_window(self):
        from .rate_limit import Transport
        from urllib.request import Request
        clock=[0.]; called=[]
        def sleep(seconds): clock[0]+=seconds
        transport=Transport(lambda req,timeout:called.append(clock[0]),clock=lambda:clock[0],sleep=sleep)
        for _ in range(71): transport(Request('https://api.coinone.co.kr/v2.1/order/detail'),1)
        self.assertEqual(called[69],0); self.assertGreaterEqual(called[70],1)


if __name__=='__main__': unittest.main()
