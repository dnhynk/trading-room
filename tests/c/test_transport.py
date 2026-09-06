import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from urllib.request import Request
from track_c.execution.coinone import CoinoneError
from track_c.execution.http_pool import HTTPSPool
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

class StorageTests(unittest.TestCase):
    def test_forecast_combines_leaders_and_public_without_deleting(self):
        from track_c.ops.storage import capacity,GIB
        import datetime as dt
        with tempfile.TemporaryDirectory() as directory:
            for name in ('public','leaders'):
                folder=Path(directory)/name; folder.mkdir()
                (folder/'20260905-15.jsonl.gz').write_bytes(b'x'*1000)
            now=dt.datetime(2026,9,5,16,tzinfo=dt.timezone.utc).timestamp()
            disk=SimpleNamespace(total=32*GIB,free=20*GIB)
            with patch('track_c.ops.storage.shutil.disk_usage',return_value=disk):
                report=capacity(directory,16*GIB,now=now)
            self.assertEqual(report['tapes']['public']['bytes_per_hour'],1000)
            self.assertAlmostEqual(report['estimated_remaining_hours'],19*GIB/2000)
            self.assertTrue(report['ok']); self.assertFalse(report['forecast_shortfall'])
            self.assertEqual(len(list(Path(directory).glob('*/*.gz'))),2)
