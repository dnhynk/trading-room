"""Market sampling must survive awaited REST work, but never bridge a feed gap."""
import asyncio
from collections import Counter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from .c3_runner import RuleRunner
from .fair import FairValue
from .test_c3 import UNITS
from .test_c3_exit import config


class SamplingTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, disconnect=None):
        runner=object.__new__(RuleRunner)
        runner.cfg=config(value_exit=False)
        runner.cfg.setdefault('excluded_symbols',[])
        runner.connected=True; runner.stopping=False
        runner.last_watchdog=0; runner.progress=Mock(); runner.counts=Counter()
        runner.wakeup=asyncio.Event(); runner.last_fair={}
        runner.fairs={'BTC':FairValue(min_samples=60,exit_config=runner.cfg)}
        micro=SimpleNamespace(bids=[(990.,100.)],asks=[(991.,100.)],book_ms=0,book_exchange=0)
        runner.markets={'BTC':SimpleNamespace(micro=micro,units=UNITS)}
        runner.client=SimpleNamespace(
            balances=lambda:[dict(currency='KRW',available='300000',limit='0')],
            active_orders=lambda:[])
        runner.oms=SimpleNamespace(campaigns={},state=dict(residuals={},orders={}),sync_cash=Mock())
        now=[0]

        def quote():
            micro.book_ms=micro.book_exchange=now[0]
            runner.fairs['BTC'].leader_quote('U',now[0],999.,1001.)

        for second in range(361):
            now[0]=second*1000; quote()
            runner.last_fair['BTC']=runner.fair_for('BTC',now[0])
        self.assertTrue(runner.last_fair['BTC']['risk']['ready'])
        self.assertEqual(len(runner.fairs['BTC'].exit_model.history),301)

        entered=asyncio.Event(); release=asyncio.Event()
        calls=[0]; observed=[]; io_times={}
        async def rest(function,*args,**kwargs):
            if function is runner.client.balances:
                io_times['started']=now[0]; entered.set()
                await release.wait()
                io_times['returned']=now[0]
            return function(*args,**kwargs)

        async def pulse(delay):
            # Advance the receive clock while another coroutine is blocked in REST.
            observed.append(runner.last_fair.get('BTC'))
            await asyncio.sleep(0)
            calls[0]+=1; now[0]+=1000
            outage=disconnect is not None and calls[0] in (2,3)
            if disconnect=='coinone': runner.connected=not outage
            if disconnect=='leader' and outage:
                if calls[0]==2:
                    runner.on_leader(['s',now[0],'U','connection','disconnected'])
                micro.book_ms=micro.book_exchange=now[0]
            else:
                quote()
            if calls[0]==4: release.set()
            if calls[0]>=6: runner.stopping=True

        fake_async=SimpleNamespace(**{**vars(asyncio),'sleep':pulse,'to_thread':rest})
        fake_time=SimpleNamespace(time=lambda:now[0]/1000,time_ns=lambda:now[0]*1000000,
                                  monotonic=lambda:now[0]/1000)
        tasks=[]
        with patch('track_c.c3_runner.asyncio',fake_async), patch('track_c.c3_runner.time',fake_time), \
             patch('track_c.c3_runner.service_notify') as watchdog:
            try:
                tasks.append(asyncio.create_task(runner.refresh_account()))
                await asyncio.wait_for(entered.wait(),1)
                tasks.append(asyncio.create_task(runner.fair_sampling()))
                await asyncio.wait_for(tasks[-1],2)
                await asyncio.wait_for(tasks[0],1)
                watchdog.assert_not_called()
                runner.progress.assert_not_called()
            finally:
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        self.assertGreaterEqual(io_times['returned']-io_times['started'],2000)
        return runner,observed

    async def test_healthy_feeds_keep_risk_ready_while_account_io_is_waiting(self):
        runner,observed=await self.exercise()
        self.assertTrue(all(row and row['risk']['ready'] for row in observed))
        self.assertTrue(runner.last_fair['BTC']['risk']['ready'])
        self.assertEqual(len(runner.fairs['BTC'].exit_model.history),301)
        self.assertEqual(runner.last_fair['BTC']['m30'],0.)

    async def test_real_public_or_leader_disconnect_discards_continuity(self):
        for venue in ('coinone','leader'):
            with self.subTest(venue=venue):
                runner,observed=await self.exercise(venue)
                self.assertTrue(any(row is None for row in observed))
                self.assertFalse(runner.last_fair['BTC']['risk']['ready'])
                self.assertIsNone(runner.last_fair['BTC']['m30'])
                self.assertLessEqual(len(runner.fairs['BTC'].exit_model.history),2)


if __name__=='__main__': unittest.main()
