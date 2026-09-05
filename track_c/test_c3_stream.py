"""Ordering, latency, and wealth invariants of bounded C3 replay."""
from collections import Counter
import gzip
import json
from pathlib import Path as FilePath
import tempfile
import unittest
from unittest.mock import patch

from .c3_replay import evaluation_blocks, run
from .fair import FairValue
from .outcomes import Path
from .replay_stream import BookWindow, EventSpool, ReplayExchange, WealthSummary
from .simulation import Exchange
from .test_c3 import rule_cfg, CONTRACT, UNITS


def message(at, ident, coin='BTC'):
    return dict(response_type='DATA',channel='ORDERBOOK',data=dict(quote_currency='KRW',target_currency=coin,
        timestamp=at,id=str(ident),bids=[dict(price='100',qty='2')],asks=[dict(price='101',qty='3')]))


class StreamTests(unittest.TestCase):
    def test_replay_rejects_input_or_code_that_changes_after_capture(self):
        for changed_source in ('tape','code'):
            with self.subTest(source=changed_source), tempfile.TemporaryDirectory() as directory:
                root=FilePath(directory); public=root/'public.jsonl'; leader=root/'leaders.jsonl.gz'; contract=root/'contracts.json'
                public.write_text(''.join(json.dumps(dict(received_ms=t,message=message(t,i)))+'\n' for i,t in enumerate((0,1000,2000))))
                with gzip.open(leader,'wt') as stream:
                    for t in (0,1000,2000): stream.write(json.dumps(['b',t,'U','BTC',t,100,1,101,1])+'\n')
                contract.write_text(json.dumps(dict(captures=[dict(coin='BTC',available_ms=0,contract=CONTRACT,units=UNITS)])))
                revision=['before']
                class ChangingFair(FairValue):
                    changed=False
                    def evaluate(self,*args,**kwargs):
                        if not self.changed:
                            if changed_source=='tape':
                                with public.open('a') as stream: stream.write('\n')
                            else:
                                revision[0]='after'
                            self.changed=True
                        return super().evaluate(*args,**kwargs)
                with patch('track_c.c3_replay.FairValue',ChangingFair), \
                     patch('track_c.c3_replay.source_hashes',side_effect=lambda root:dict(source=revision[0])), \
                     self.assertRaisesRegex(ValueError,'source '+changed_source+' changed'):
                    run(rule_cfg(entry_ticks=1000),contract,[public],[leader])

    def test_disk_sort_filters_coins_preserves_ties_and_cross_file_identity(self):
        q=Counter()
        raw=[(2000,message(2000,2)),(1000,message(1000,1)),(2000,message(2000,2)),
             (1500,message(1500,9,'ETH')),(3000,message(3000,3))]
        leaders=[['b',3000,'U','BTC',3000,100,1,101,1],['s',1000,'U','connected','connected']]
        with EventSpool(iter(raw),iter(leaders),['BTC'],1500,q) as spool:
            rows=list(spool)
            self.assertEqual([r[:3] for r in rows],[(1000,0,'coinone'),(1000,1,'connection'),
                (2000,0,'coinone'),(3000,0,'coinone'),(3000,1,'leader')])
            self.assertEqual((spool.start,spool.end),(1000,3000))
            self.assertEqual(q['coinone_duplicate_book'],1)
            self.assertEqual(q['coinone_accepted'],3)
            self.assertGreater(spool.bytes,0)

    def test_same_time_arrival_sees_all_tied_books_before_trade_fill(self):
        first=dict(t=0,exchange_t=0,kind='book',bids=[(99.,2.)],asks=[(101.,2.)])
        trade=dict(t=100,kind='trade',price=99.,qty=10.,buy=False)
        second=dict(t=100,exchange_t=100,kind='book',bids=[(99.,2.)],asks=[(100.,2.)])
        full=Path([first,trade,second],stale_ms=1500)
        window=BookWindow(1500)
        for e in (first,trade,second): window.append(e)
        clients=[Exchange({'BTC':full},lambda:0.,100),ReplayExchange({'BTC':window},lambda:0.,100)]
        for client in clients:
            client.submit(dict(cid='x',coin='BTC',side='BUY',type='LIMIT',qty='1',price='100'))
            client.event('BTC',trade)
            self.assertEqual(client.orders['x']['status'],'REJECTED')
            self.assertEqual(client.orders['x']['executed_qty'],'0')

    def test_book_window_retains_only_left_anchor_and_stale_exchange_time(self):
        window=BookWindow(1500)
        for t in range(10000):
            window.append(dict(t=t,exchange_t=t,kind='book',bids=[(99,1)],asks=[(100,1)]))
            window.trim(t)
        self.assertEqual(len(window.books),1)
        self.assertLessEqual(window.max_books,2)
        self.assertIsNotNone(window.book_at(11000))
        self.assertIsNone(window.book_at(12000))
        window.append(dict(t=12000,exchange_t=10000,kind='book',bids=[(99,1)],asks=[(100,1)]))
        self.assertIsNone(window.book_at(12000))

    def test_pruning_keeps_terminal_order_until_oms_has_reconciled_it(self):
        client=ReplayExchange({},lambda:0.)
        client.orders={'done':dict(status='FILLED'),'active':dict(status='LIVE')}
        client.pending=[(1000,'cancel','done')]
        client.prune({'done'})
        self.assertIn('done',client.orders)
        client.prune(set())
        self.assertEqual(list(client.orders),['active'])
        self.assertEqual(client.pending,[])

    def test_online_blocks_match_curve_asof_for_exact_partial_and_missing_boundaries(self):
        day=86400000
        curve=[(1000,100.),(2000,97.),(day+500,105.),(day+2000,102.),(2*day+2000,111.)]
        for start,end in ((1000,2*day+1000),(1500,2*day+1500),(0,2*day),(1500,3*day),(1000,day+600)):
            summary=WealthSummary(500,100.,500,start,end)
            for t,value in curve: summary.observe(t,value)
            daily,blocks=summary.finish()
            self.assertEqual(blocks,evaluation_blocks(curve,start,end))
            self.assertAlmostEqual(sum(r['net_krw'] for r in daily.values()),11.)
            self.assertEqual(summary.drawdown,3.)


if __name__=='__main__': unittest.main()
