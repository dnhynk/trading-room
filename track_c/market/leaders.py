"""Upbit/Bithumb top-of-book and trade recorder for the Coinone fair value. Public only.

Rows are compact JSON arrays in hourly gzip files named by UTC hour like the Coinone
recorder (data/leaders/YYYYMMDD-HH.jsonl.gz):
  ["b", recv_ms, venue, coin, exchange_ms, bid, bid_qty, ask, ask_qty, stream]
  ["t", recv_ms, venue, coin, exchange_ms, price, qty, side, sequential_id]
"""
import argparse
import asyncio
from collections import Counter
import gzip
import json
import math
from pathlib import Path
import shutil
import time
import urllib.request

WS = {'upbit': 'wss://api.upbit.com/websocket/v1', 'bithumb': 'wss://ws-api.bithumb.com/websocket/v1'}
MARKETS = {'upbit': 'https://api.upbit.com/v1/market/all', 'bithumb': 'https://api.bithumb.com/v1/market/all'}
CODE = {'upbit': 'U', 'bithumb': 'B'}
BASE_COINS = ('BTC', 'ETH', 'XRP', 'SOL', 'DOGE', 'ADA', 'TRX', 'XLM', 'SUI', 'USDT', 'ENA', 'WLD', 'PENGU', 'ONDO', 'KAIA', 'FIL', 'TRUMP', 'ONE', 'ZORA')


def now():
    return time.time_ns() // 1000000


def listed(venue):
    with urllib.request.urlopen(urllib.request.Request(MARKETS[venue], headers={'Accept': 'application/json'}), timeout=15) as r:
        rows = json.load(r)
    if not isinstance(rows, list):
        raise ValueError('market list')
    return {str(r['market'])[4:] for r in rows if isinstance(r, dict) and str(r.get('market', '')).startswith('KRW-')}


def parse(venue, recv, raw):
    """Returns a compact row or None. Rejects malformed and non-finite values."""
    m = json.loads(raw)
    kind, code = m.get('type'), str(m.get('code', ''))
    if not code.startswith('KRW-'):
        return None
    coin = code[4:]
    if kind == 'orderbook':
        units = m['orderbook_units']
        if not units:
            return None
        u = units[0]
        ts = int(m['timestamp'])
        if ts > 10**14:
            ts //= 1000  # Bithumb book timestamps arrive in microseconds
        bp, bq, ap, aq = (float(u[k]) for k in ('bid_price', 'bid_size', 'ask_price', 'ask_size'))
        if not all(math.isfinite(v) for v in (bp, bq, ap, aq)) or not (0 < bp < ap) or bq < 0 or aq < 0:
            return None
        return ['b', recv, CODE[venue], coin, ts, bp, bq, ap, aq, str(m.get('stream_type', ''))[:1]]
    if kind == 'trade':
        ts = int(m.get('trade_timestamp') or m['timestamp'])
        p, q = float(m['trade_price']), float(m['trade_volume'])
        if not math.isfinite(p+q) or p <= 0 or q <= 0:
            return None
        return ['t', recv, CODE[venue], coin, ts, p, q, 'buy' if m.get('ask_bid') == 'BID' else 'sell', str(m.get('sequential_id', ''))]
    return None


class Recorder:
    def __init__(self, directory, venues=('upbit', 'bithumb'), refresh_s=600, sink=None):
        self.directory = Path(directory)
        self.folder = self.directory / 'leaders'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.venues, self.refresh_s, self.sink = list(venues), refresh_s, sink
        self.raw, self.raw_hour, self.storage_ok = None, None, True
        self.counts = Counter()
        self.state = {v: dict(connected=False, coins=[], last_recv_ms=0, errors=0) for v in self.venues}
        self.stopping = False
        self.last_flush = time.monotonic()

    def universe(self):
        coins = set(BASE_COINS)
        try:
            status = json.loads((self.directory / 'status.json').read_text())
            coins |= {c for c in status.get('markets', {}) if isinstance(c, str) and c.isalnum()}
        except (OSError, ValueError, AttributeError):
            self.counts['universe_read_error'] += 1
        return coins

    def write(self, row):
        if not self.storage_ok:
            return
        hour = time.strftime('%Y%m%d-%H', time.gmtime(row[1] / 1000))
        if hour != self.raw_hour:
            if self.raw:
                self.raw.close()
            self.raw = gzip.open(self.folder / (hour + '.jsonl.gz'), 'at', encoding='utf-8')
            self.raw_hour = hour
        self.raw.write(json.dumps(row, separators=(',', ':')) + '\n')
        if time.monotonic() - self.last_flush >= 5:
            self.raw.flush()
            self.last_flush = time.monotonic()

    async def venue(self, name):
        from websockets.asyncio.client import connect
        backoff = 1
        while not self.stopping:
            st = self.state[name]
            refresh_task = None
            try:
                available = await asyncio.to_thread(listed, name)
                coins = sorted(self.universe() & available)
                if not coins:
                    raise ValueError('no coins')
                st['coins'] = coins
                started = time.monotonic()
                async with connect(WS[name], open_timeout=15, ping_interval=20, ping_timeout=20, close_timeout=3, max_queue=4096, max_size=2**22) as ws:
                    codes = ['KRW-' + c for c in coins]
                    await ws.send(json.dumps([dict(ticket='trading-room-c-leaders'), dict(type='orderbook', codes=codes), dict(type='trade', codes=codes), dict(format='DEFAULT')]))
                    last = time.monotonic()
                    while not self.stopping:
                        if refresh_task is None and time.monotonic()-started >= self.refresh_s:
                            refresh_task = asyncio.create_task(asyncio.to_thread(listed,name))
                        if refresh_task is not None and refresh_task.done():
                            try:
                                available = refresh_task.result()
                                desired = sorted(self.universe() & available)
                            except (OSError,ValueError):
                                self.counts[name+'_refresh_errors'] += 1
                                desired = coins
                            refresh_task = None
                            started = time.monotonic()
                            if desired != coins:
                                self.counts[name+'_subscription_changes'] += 1
                                break  # only a real subscription change reconnects
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 5)
                        except asyncio.TimeoutError:
                            if time.monotonic() - last > 60:
                                raise TimeoutError('silent stream')
                            continue
                        recv = now()
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        try:
                            row = parse(name, recv, raw)
                        except (KeyError, ValueError, TypeError):
                            self.counts[name + '_invalid'] += 1
                            continue
                        last = time.monotonic()
                        st['connected'] = True
                        st['last_recv_ms'] = recv
                        if row:
                            self.write(row)
                            self.counts[name + '_' + row[0]] += 1
                            if self.sink:
                                self.sink(row)
                    backoff = 1
            except Exception:
                st['errors'] += 1
                self.counts[name + '_errors'] += 1
            finally:
                if refresh_task is not None:
                    refresh_task.cancel()
                    await asyncio.gather(refresh_task,return_exceptions=True)
                st['connected'] = False
                row = ['s', now(), CODE[name], None, 'disconnected']
                self.write(row)
                if self.sink:
                    self.sink(row)
            if not self.stopping:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def report(self):
        if self.raw:
            self.raw.flush()
        size = sum(p.stat().st_size for p in self.folder.glob('*.gz'))
        self.storage_ok = shutil.disk_usage(self.folder).free > 1024**3
        report = dict(t_ms=now(), venues=self.state, counts=dict(self.counts), storage_ok=self.storage_ok, bytes=size, hour=self.raw_hour)
        tmp = self.folder / 'status.tmp'
        tmp.write_text(json.dumps(report) + '\n', encoding='utf-8')
        tmp.replace(self.folder / 'status.json')

    async def run(self, seconds=None):
        tasks = [asyncio.create_task(self.venue(v)) for v in self.venues]
        start = time.monotonic()
        try:
            while not self.stopping:
                if seconds is not None and time.monotonic() - start >= seconds:
                    self.stopping = True
                if (self.directory / 'STOP').exists():
                    self.stopping = True
                self.report()
                await asyncio.sleep(30 if seconds is None else min(30, max(.5, seconds - (time.monotonic() - start))))
        finally:
            self.stopping = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.report()
            if self.raw:
                self.raw.close()


def rows(path):
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, list) and (len(row) >= 9 or (len(row) == 5 and row[0] == 's')):
                yield row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', required=True)
    p.add_argument('--seconds', type=float)
    p.add_argument('--venues', nargs='+', default=['upbit', 'bithumb'], choices=list(WS))
    args = p.parse_args()
    recorder = Recorder(args.data_dir, args.venues)
    import signal
    def stop(*_):
        recorder.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    asyncio.run(recorder.run(args.seconds))


if __name__ == '__main__':
    main()
