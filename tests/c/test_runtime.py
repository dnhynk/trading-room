"""The actual C runner initializes, reconciles, records, reports and shuts down offline."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from common.paths import PROJECT_ROOT
from track_c.learning.config import validate
from track_c.learning import VERSION as MODEL_VERSION
from track_c.live import LiveRunner, VERSION, LivePortfolio
from track_c.settings import load


class Client:
    def __init__(self):
        self._transport = Mock()
        self.submit = Mock(side_effect=AssertionError('no order expected'))

    def balances(self):
        return [dict(currency='KRW',available='500000',limit='0')]

    def active_orders(self):return []

    def universe(self):
        return [dict(target_currency='BTC',trade_status=1,maintenance_status=0,
                     order_types=['limit','market','stop_limit'],min_order_amount='5000',
                     qty_unit='0.00000001',max_qty='999999',max_order_amount='999999999')], []

    def fees(self,coin):return dict(maker='0',taker='0')
    def price_units(self,coin):return [dict(range_min='0',price_unit='1000')]


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_owner_runs_account_scan_and_final_report_with_no_network_or_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            config=load(PROJECT_ROOT/'track_c/config.json')
            config.update(data_directory=directory,record_coins=[],expected_egress_ip='127.0.0.1')
            client=Client()
            artifact=dict(digest='offline-fixture',version=MODEL_VERSION)
            with patch('track_c.live.read_model',return_value=(artifact,validate(),{})), \
                 patch('track_c.runtime.Credentials.read',return_value=object()), \
                 patch('track_c.runtime.CoinoneExecution',return_value=client):
                runner=LiveRunner(config)
            self.assertIsInstance(runner.oms,LivePortfolio)
            async def idle(*args):await asyncio.Event().wait()
            with patch.object(runner,'feed',side_effect=idle), \
                 patch.object(runner.recorder,'run',side_effect=idle), \
                 patch('track_c.runtime.private_follow',side_effect=idle), \
                 patch('track_c.runtime.service_notify'), \
                 patch('urllib.request.urlopen',return_value=Mock(read=lambda:b'127.0.0.1')):
                await runner.run(seconds=0)
            status=json.loads((Path(directory)/'status.json').read_text())
            self.assertEqual(status['capital_krw'],'500000')
            self.assertEqual(status['policy'],'c4')
            self.assertEqual(status['execution_version'],VERSION)
            self.assertEqual(status['rule']['version'],VERSION)
            self.assertEqual(status['c4_live_mode'],'structural_sampling')
            self.assertEqual(status['model']['digest'],'offline-fixture')
            self.assertEqual(status['positions'],{})
            self.assertTrue(status['storage_ok'])
            client.submit.assert_not_called()
            with closing(sqlite3.connect(Path(directory)/'ledger.sqlite')) as db:
                events=[(kind,json.loads(body)) for kind,body in db.execute('select kind,body from events')]
            start=next(body for kind,body in events if kind=='START')
            self.assertEqual(start['policy'],'c4')
            self.assertEqual(start['rule'],VERSION)
            self.assertFalse(start['automatic_retraining'])
            self.assertFalse(any(kind=='ORDER_INTENT' for kind,body in events))
