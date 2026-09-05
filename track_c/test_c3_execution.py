"""Execution regressions: uncertain responses, cancellation races and causal wakes."""
import asyncio
from collections import Counter
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

from .coinone import CoinoneError
from .http_pool import HTTPSPool
from .microstructure import Micro
from .recovery import protect_once
from . import test_c3 as fixture


class Connection:
    def __init__(self, host, timeout=3, *, status=200, raw=b'{"result":"success","error_code":"0"}', error=None):
        self.sock = Mock()
        self.timeout, self.status, self.raw, self.error = timeout, status, raw, error
        self.requests, self.closed = [], False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body))
        if self.error: raise self.error

    def getresponse(self):
        return SimpleNamespace(read=lambda n:self.raw[:n], status=self.status, will_close=False)

    def close(self): self.closed = True


class TransportTests(unittest.TestCase):
    def test_persistent_signed_requests_are_sent_once(self):
        factory = Mock(side_effect=Connection)
        pool = HTTPSPool(size=1, factory=factory); self.addCleanup(pool.close)
        for payload in (b'first', b'second'):
            pool(Request('https://api.coinone.co.kr/v2.1/order', data=payload), 3)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(pool.report()['requests'], 2)
        self.assertEqual(pool.report()['connections'], 1)

    def test_lost_response_is_not_retried_or_exposed(self):
        bad = Connection('api.coinone.co.kr', error=OSError('secret request'))
        good = Connection('api.coinone.co.kr')
        factory = Mock(side_effect=[bad, good])
        pool = HTTPSPool(size=1, factory=factory); self.addCleanup(pool.close)
        request = Request('https://api.coinone.co.kr/v2.1/order', data=b'signed')
        with self.assertRaisesRegex(CoinoneError, '^network or response decoding failure$'):
            pool(request, 3)
        self.assertEqual(len(bad.requests), 1); self.assertTrue(bad.closed)
        self.assertEqual(factory.call_count, 1)
        pool(Request('https://api.coinone.co.kr/public/v2/markets/KRW'), 3)
        self.assertEqual(factory.call_count, 2)

    def test_redirect_and_other_origins_are_refused(self):
        factory = Mock(side_effect=lambda *a,**k:Connection(*a,**k,status=302))
        pool = HTTPSPool(size=1, factory=factory); self.addCleanup(pool.close)
        for url in ('http://api.coinone.co.kr/', 'https://untrusted.example/', 'https://x@api.coinone.co.kr/'):
            with self.assertRaisesRegex(CoinoneError, 'origin'): pool(Request(url), 3)
        factory.assert_not_called()
        with self.assertRaisesRegex(CoinoneError, 'redirect'): pool(Request('https://api.coinone.co.kr/'), 3)
        self.assertEqual(factory.call_count, 1)

    def test_api_rejection_preserves_numeric_code_only(self):
        factory = lambda *a,**k:Connection(*a,**k,raw=b'{"result":"error","error_code":"103","message":"secret"}')
        pool=HTTPSPool(size=1,factory=factory); self.addCleanup(pool.close)
        with self.assertRaises(CoinoneError) as caught: pool(Request('https://api.coinone.co.kr/'), 3)
        self.assertEqual(caught.exception.code, 103)
        self.assertNotIn('secret', str(caught.exception))


class RecoveryTests(unittest.TestCase):
    setUp = fixture.RestingFlowTests.setUp
    drive = fixture.RestingFlowTests.drive
    entry_cid = fixture.RestingFlowTests.entry_cid
    events = fixture.RestingFlowTests.events

    def filled(self):
        self.pf.enter('BTC', self.plan, {}, '5000')
        self.client.fill(self.entry_cid(), self.plan['qty'], '990')
        self.client.orderbook = lambda c:dict(bids=[dict(price='990', qty='1000')])
        self.now[0] += 1

    def test_lost_protection_response_reconciles_same_intent(self):
        self.filled(); self.client.fail = 'lost_response'
        self.assertFalse(protect_once(self.pf))
        self.assertTrue(protect_once(self.pf))
        stops = [o for o in self.client.submissions if o['role']=='protect']
        self.assertEqual(len(stops), 1)
        self.assertEqual((stops[0]['trigger_price'],stops[0]['price']), (self.plan['stop'],self.plan['stop_limit']))

    def test_unfilled_entry_returns_preexisting_dust_without_halting(self):
        self.pf.state['residuals']['BTC']=dict(qty='1',cost='990',mark='990',mark_at=self.now[0],t=self.now[0])
        self.pf.enter('BTC',self.plan,{},'5000')
        self.assertTrue(protect_once(self.pf))
        self.assertIsNone(self.pf.state['halt'])
        self.assertEqual(self.pf.state['residuals']['BTC']['qty'],'1')
        self.assertEqual(self.pf.state['residuals']['BTC']['cost'],'990')
        self.assertTrue(any(kind=='NO_FILL' for _,kind,_ in self.events()))
        self.assertFalse([o for o in self.client.submissions if o['side']=='SELL'])

    def test_native_stop_remains_during_warmup_then_racing_fill_exits_remainder(self):
        self.filled(); protect_once(self.pf); protect_once(self.pf)
        stop = self.pf.book('BTC').active('protect')[0]
        self.drive(bid=None, fresh=False, quantitative_decision=dict(hold=True, cancel_entry=True))
        self.assertFalse(self.client.cancels)
        self.assertIsNone(self.pf.campaigns['BTC']['exit_reason'])
        # A stop fill racing with cancellation leaves less than the original qty.
        original = self.client.cancel
        def race(coin, cid):
            if cid==stop['cid']: self.client.fill(cid, '1', '987', 'PARTIALLY_FILLED')
            return original(coin, cid)
        self.client.cancel = race
        self.drive(quantitative_decision=dict(hold=True,recovery_ready=True))
        self.assertFalse([o for o in self.client.submissions if o['role']=='take'])
        self.drive(quantitative_decision=dict(hold=True,recovery_ready=True))
        exit_order = [o for o in self.client.submissions if o['role']=='exit'][0]
        self.assertEqual(D(exit_order['qty']), D(self.plan['qty'])-1)

    def test_healthy_warmup_retires_protection_before_resting_take(self):
        self.filled(); protect_once(self.pf); protect_once(self.pf)
        self.drive(quantitative_decision=dict(hold=True,recovery_ready=True))
        self.assertFalse(self.pf.book('BTC').active('protect'))
        self.assertFalse([o for o in self.client.submissions if o['role']=='take'])
        self.drive(quantitative_decision=dict(hold=True,recovery_ready=True))
        take = [o for o in self.client.submissions if o['role']=='take'][0]
        self.assertEqual(take['qty'], self.plan['qty'])

    def test_take_fill_during_recovery_cancel_is_never_sold_twice(self):
        self.filled(); self.drive(quantitative_decision=dict(hold=True))
        take = self.pf.book('BTC').active('take')[0]
        original = self.client.cancel
        def race(coin,cid):
            if cid==take['cid']: self.client.fill(cid, self.plan['qty'], '991')
            return original(coin,cid)
        self.client.cancel=race
        self.assertTrue(protect_once(self.pf))
        self.assertFalse([o for o in self.client.submissions if o['role'] in ('protect','exit')])
        self.assertEqual(self.pf.campaigns,{})

    def test_unconfirmed_cancel_blocks_new_sell_reservation(self):
        self.filled(); self.drive(quantitative_decision=dict(hold=True))
        self.client.cancel = Mock(side_effect=CoinoneError('network failure'))
        self.assertFalse(protect_once(self.pf))
        self.assertFalse([o for o in self.client.submissions if o['role'] in ('protect','exit')])

    def test_bid_gap_before_fill_ack_exits_without_placing_take(self):
        self.filled()
        self.drive(bid=983, quantitative_decision=dict(hold=False,reason='stop'),force_reconcile=True)
        sells = [o for o in self.client.submissions if o['side']=='SELL']
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]['role'], 'exit')
        self.assertEqual(sells[0]['qty'], self.plan['qty'])
        self.drive(bid=983,force_reconcile=True)
        self.assertEqual(self.pf.campaigns,{})
        self.assertTrue(any(k=='FILL' and b.get('exit_request_to_fill_seen_ms') is not None for _,k,b in self.events()))

    def test_unchanged_order_poll_does_not_append_journal(self):
        self.pf.enter('BTC',self.plan,{},'5000')
        book=self.pf.book('BTC'); book.reconcile(force=True)
        before=len(self.events())
        for _ in range(10): book.reconcile(force=True)
        self.assertEqual(len(self.events()),before)


class FeatureTests(unittest.TestCase):
    def test_rule_fast_path_preserves_books_trades_quality_and_core_features(self):
        normal, fast=Micro('BTC'), Micro('BTC',legacy_features=False)
        for n in range(1,90):
            now=n*1000
            b=dict(quote_currency='KRW',target_currency='BTC',id=n,timestamp=now,
                   bids=[dict(price=str(990+n%2),qty='100')],asks=[dict(price=str(992+n%2),qty='110')])
            t=dict(quote_currency='KRW',target_currency='BTC',id=str(n),timestamp=now,
                   price='991',qty=str(n),is_seller_maker=bool(n%2))
            for channel, data in (('ORDERBOOK',b),('TRADE',t),('TRADE',t)):
                self.assertEqual(normal.feed(channel,data,now),fast.feed(channel,data,now))
            a,z=normal.snapshot(now,1),fast.snapshot(now,1)
            self.assertEqual({k:v for k,v in a['features'].items() if not k.startswith('b_')},
                             {k:v for k,v in z['features'].items() if not k.startswith('b_')})
        self.assertEqual(normal.quality,fast.quality)
        self.assertIsNone(fast.b_features)
        fast.reset(); self.assertIsNone(fast.b_features)


class RunnerIntegrationTests(unittest.TestCase):
    def test_stop_has_priority_and_value_is_not_reused_with_later_book(self):
        from .c3_runner import RuleRunner
        r=object.__new__(RuleRunner)
        r.cfg=fixture.rule_cfg(value_exit=True); r.last_value_eval={}
        model=Mock(); model.continuation.return_value=dict(ready=True,exit=True,upper_ticks=-.1)
        r.fairs=dict(BTC=model); r.last_fair=dict(BTC=dict(dev_ticks=2,m30=0,m10=0,risk=dict(ready=True)))
        r.markets={}; c=dict(id='c',coin='BTC',first_fill=99,stop='987',stop_limit='986',plan=dict(entry='990',tick='1',take_profit='991'))
        with patch('track_c.c3_runner.time.time',return_value=100.):
            self.assertEqual(r.holding_decision(c,dict(bid=987,ask=988,tick=1))['reason'],'stop')
            model.continuation.assert_not_called()
            self.assertEqual(r.holding_decision(c,dict(bid=990,ask=991,tick=1))['reason'],'value')
            self.assertTrue(r.holding_decision(c,dict(bid=991,ask=992,tick=1))['hold'])
            model.continuation.assert_called_once()


class LeaderRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def run_refresh(self, changed):
        from .leaders import Recorder
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        recorder=Recorder(temp.name,venues=['upbit']); recorder.universe=lambda:{'BTC','ETH'}
        recorder.write=Mock(); received=[]; recorder.sink=received.append
        clock=[0]; polls=[0]; connections=[]
        async def recv():
            clock[0]+=200
            await asyncio.sleep(.004)
            if clock[0]>2200: recorder.stopping=True
            return '{}'
        class Socket:
            async def __aenter__(self): connections.append(self); return self
            async def __aexit__(self,*a): return None
            async def send(self,*a): pass
            async def recv(self): return await recv()
        def listed(name):
            polls[0]+=1
            return {'BTC','ETH'} if changed and polls[0]>1 else {'BTC'}
        with patch('websockets.asyncio.client.connect',side_effect=lambda *a,**k:Socket()), \
             patch('track_c.leaders.listed',side_effect=listed), \
             patch('track_c.leaders.time',SimpleNamespace(monotonic=lambda:clock[0],time_ns=lambda:clock[0]*1000000000)), \
             patch('track_c.leaders.parse',return_value=['b',1000,'U','BTC',1000,990,1,991,1]):
            await recorder.venue('upbit')
        self.assertGreater(polls[0],1)
        return len(connections),recorder

    async def test_same_universe_keeps_one_connection_across_refresh(self):
        count,recorder=await self.run_refresh(False)
        self.assertEqual(count,1)
        self.assertEqual(recorder.counts['upbit_subscription_changes'],0)

    async def test_changed_universe_reconnects_once(self):
        count,recorder=await self.run_refresh(True)
        self.assertEqual(count,2)
        self.assertEqual(recorder.counts['upbit_subscription_changes'],1)


class StorageTests(unittest.TestCase):
    def test_forecast_combines_leaders_and_public_without_deleting(self):
        from .storage import capacity,GIB
        import datetime as dt
        with tempfile.TemporaryDirectory() as directory:
            for name in ('public','leaders'):
                folder=Path(directory)/name; folder.mkdir()
                (folder/'20260905-15.jsonl.gz').write_bytes(b'x'*1000)
            now=dt.datetime(2026,9,5,16,tzinfo=dt.timezone.utc).timestamp()
            disk=SimpleNamespace(total=32*GIB,free=20*GIB)
            with patch('track_c.storage.shutil.disk_usage',return_value=disk):
                report=capacity(directory,16*GIB,now=now)
            self.assertEqual(report['tapes']['public']['bytes_per_hour'],1000)
            self.assertAlmostEqual(report['estimated_remaining_hours'],19*GIB/2000)
            self.assertTrue(report['ok']); self.assertFalse(report['forecast_shortfall'])
            self.assertEqual(len(list(Path(directory).glob('*/*.gz'))),2)


if __name__=='__main__': unittest.main()
