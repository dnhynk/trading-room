"""Market feeds, reconciled account and persistent execution loop for Track C."""
import asyncio
from collections import Counter
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import time

from track_c.execution.coinone import CoinoneError, Credentials, decimal
from track_c.execution.client import CoinoneExecution
from track_c.execution.portfolio import Portfolio
from track_c.execution.rate_limit import Transport
from track_c.execution.http_pool import HTTPSPool
from track_c.execution.private_stream import follow as private_follow
from track_c.execution.sizing import price_unit
from track_c.market.metadata import Market
from track_c.market.leaders import Recorder
from track_c.market.universe import ASSET_POLICY_VERSION
from track_c.market import prices as rule
from track_c.ops.store import Store, encoded
from track_c.ops.observations import Observations
from track_c.ops.service_health import notify as service_notify

EXECUTION_VERSION = 'c4-runtime-layout1'

class Runtime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.directory = Path(cfg["data_directory"])
        self.store = Store(self.directory)
        self.client = CoinoneExecution(Credentials.read(cfg["env_path"], profile=cfg["credential_profile"]), timeout=5)
        self.oms = Portfolio(cfg, self.client, self.store)
        self.markets, self.foreign_assets = {}, set(cfg["excluded_symbols"])
        self.account_available, self.account_reserved, self.account_at = D(0), D(0), 0
        self.connected, self.stopping, self.generation = False, False, 0
        self.queue = asyncio.Queue(maxsize=200)
        self.counts = Counter()
        self.last_scan = self.last_account = self.last_report = 0
        self.raw, self.raw_hour, self.storage_ok = None, None, True
        self.client._transport=Transport(self.client._transport)
        self.selection={}; self.coverage_reasons={}; self.last_decision=0; self.last_decision_log=0
        self.private_connected=False; self.wakeup=asyncio.Event(); self.execution_feedback={}
        self.last_fair = {}
        self.last_private_events = 0
        self.http_pool = HTTPSPool()
        self.client._transport.send = self.http_pool
        self.client._timeout = float(cfg.get('http_timeout_s',3))
        self.last_watchdog = 0
        self.observations = Observations(self.directory)
        self.recorder = Recorder(self.directory, sink=self.on_leader)
        # Resting orders are re-read at most once per second unless the private stream reports a change.
        self.oms.poll_interval = float(cfg.get('reconcile_poll_s', 1.0))

    def record(self, recv, message):
        if not self.storage_ok:
            return
        hour = time.strftime("%Y%m%d-%H", time.gmtime(recv/1000))
        if hour != self.raw_hour:
            if self.raw:
                self.raw.close()
            folder = self.directory/"public"
            folder.mkdir(exist_ok=True)
            self.raw = gzip.open(folder/(hour+".jsonl.gz"), "at", encoding="utf-8")
            self.raw_hour = hour
        self.raw.write(encoded(dict(received_ms=recv, message=message))+"\n")

    async def feed(self):
        from websockets.asyncio.client import connect
        backoff=1
        while not self.stopping:
            try:
                async with connect('wss://stream.coinone.co.kr',open_timeout=10,ping_interval=15,ping_timeout=15,close_timeout=3,max_queue=2048) as ws:
                    subscribed=set(); pending=set(); last_ping=time.monotonic()
                    while not self.stopping:
                        desired={(coin,ch) for coin in self.markets for ch in ('ORDERBOOK','TRADE')}
                        for action,pairs in (('UNSUBSCRIBE',subscribed-desired),('SUBSCRIBE',desired-subscribed)):
                            for coin,channel in sorted(pairs):
                                await ws.send(encoded(dict(request_type=action,channel=channel,topic=dict(quote_currency='KRW',target_currency=coin))))
                                if action=='SUBSCRIBE': pending.add((coin,channel))
                                else: pending.discard((coin,channel))
                        subscribed=desired
                        if time.monotonic()-last_ping>=15:
                            await ws.send('{"request_type":"PING"}'); last_ping=time.monotonic()
                        try: raw=await asyncio.wait_for(ws.recv(),1)
                        except asyncio.TimeoutError: continue
                        recv=time.time_ns()//1000000; msg=json.loads(raw); data=msg.get('data') or {}
                        coin=data.get('target_currency'); channel=msg.get('channel'); kind=msg.get('response_type')
                        if kind=='ERROR': raise CoinoneError('public subscription rejected')
                        if kind=='SUBSCRIBED': pending.discard((coin,channel))
                        self.connected=bool(subscribed) and not pending
                        if kind!='DATA' or coin not in self.markets: continue
                        self.record(recv,msg)
                        hook=getattr(self,'observe_public',None)
                        micro=self.markets[coin].micro
                        prior=dict(t_ms=micro.book_ms,exchange_ms=micro.book_exchange,bids=list(micro.bids),asks=list(micro.asks),
                                   tick=float(price_unit(self.markets[coin].units,D(str(micro.bids[0][0])))) if micro.bids else None) if hook and channel=='TRADE' and coin in self.cfg.get('coins',[]) else None
                        accepted=micro.quality['accepted']
                        self.markets[coin].feed(channel,data,recv); self.counts['messages']+=1
                        if prior is not None and micro.quality['accepted']>accepted:
                            hook(coin,data,recv,prior)
                        if self.cfg.get('policy')=='rule' and coin in self.cfg['coins']:
                            self.wakeup.set()
                    backoff=1
            except Exception:
                self.counts['ws_errors']+=1
            finally:
                self.connected=False
                for market in self.markets.values(): market.micro.reset()
            if not self.stopping:
                await asyncio.sleep(backoff); backoff=min(15,backoff*2)

    def cash(self):
        return max(D(0),min(D(self.oms.state['cash_krw'])-self.oms.reserved_cash(),self.account_available)) if self.cfg['funding_confirmed'] else D(0)

    async def candle_poll(self):
        # Track C uses receive-time books/trades; REST candles do not drive it.
        return

    def progress(self):
        # Called only after a bounded operation has returned, never by a detached
        # timer that could hide a blocked order/reconciliation call.
        if time.monotonic()-self.last_watchdog>=1:
            service_notify('WATCHDOG=1')
            self.last_watchdog=time.monotonic()

    async def fair_sampling(self):
        # Same event loop as market callbacks, independent of serialized REST
        # awaits. FairValue keeps one causal sample per second; no backfilling.
        # This task deliberately does not feed the execution watchdog.
        while not self.stopping:
            self.sample_fairs()
            await asyncio.sleep(.2)

    async def scan(self):
        contracts, _ = await asyncio.to_thread(self.client.universe)
        self.progress()
        by = {r['target_currency']: r for r in contracts}
        added, captured = {}, []
        # Trading coins plus record-only coins (subscribed for tapes and leader coverage, never traded).
        for coin in list(self.cfg['coins']) + [c for c in self.cfg.get('record_coins', []) if c not in self.cfg['coins']]:
            row = by.get(coin)
            required={'limit','market','stop_limit'} if coin in self.cfg['coins'] else {'limit','market'}
            if not row or row.get('trade_status') != 1 or row.get('maintenance_status') != 0 or not required <= set(row.get('order_types', [])):
                self.coverage_reasons[coin] = 'contract_unavailable'
                if coin in self.oms.campaigns and coin in self.markets:
                    added[coin] = self.markets[coin]
                continue
            try:
                fees = await asyncio.to_thread(self.client.fees, coin)
                self.progress()
                units = await asyncio.to_thread(self.client.price_units, coin)
                self.progress()
                if coin in self.markets:
                    market = self.markets[coin]
                    market.fees, market.units, market.contract = fees, units, row
                else:
                    market = Market(coin, self.cfg, row, units, fees, [])
                added[coin] = market
                self.coverage_reasons.pop(coin, None)
                captured.append(dict(coin=coin, available_ms=int(time.time() * 1000), contract=row, units=units, fees=fees))
            except (CoinoneError, ValueError, KeyError, TypeError):
                self.progress()
                self.coverage_reasons[coin] = 'metadata_unavailable'
                self.counts['scan_market_error'] += 1
                if coin in self.markets:
                    added[coin] = self.markets[coin]
        self.markets = added
        self.generation += 1
        self.last_scan = time.time()
        directory = self.directory / 'contracts'
        directory.mkdir(exist_ok=True)
        (directory / (str(int(time.time() * 1000)) + '.json')).write_text(encoded(dict(asset_policy=ASSET_POLICY_VERSION, markets=captured)) + '\n')
        self.store.event('SCAN', symbols=list(added), excluded=sorted(self.foreign_assets), asset_policy=ASSET_POLICY_VERSION, policy='rule', reasons=self.coverage_reasons)

    async def refresh_account(self):
        for coin in list(self.oms.campaigns):
            await asyncio.to_thread(self.oms.book(coin).reconcile)
        rows = await asyncio.to_thread(self.client.balances)
        orders = await asyncio.to_thread(self.client.active_orders)
        krw = [r for r in rows if r.get('currency') == 'KRW']
        if len(krw) != 1:
            raise CoinoneError('KRW balance missing or ambiguous')
        self.account_available, self.account_reserved = decimal(krw[0]['available']), decimal(krw[0]['limit'])
        self.foreign_assets = set(self.cfg['excluded_symbols'])
        residuals = self.oms.state['residuals']
        for row in rows:
            coin = row['currency']
            if coin == 'KRW' or coin in self.oms.campaigns:
                continue
            held = decimal(row['available']) + decimal(row['limit'])
            # Our own dust residual is not an external position; anything beyond it is.
            if held > D(residuals[coin]['qty']) * D('1.001') if coin in residuals else held > 0:
                self.foreign_assets.add(coin)
        ids = set(self.oms.state['orders'])
        exchange_ids = {o.get('exchange_id') for o in self.oms.state['orders'].values()}
        for o in orders:
            if o.get('user_order_id') in ids or (o.get('order_id') and o['order_id'] in exchange_ids):
                continue
            self.foreign_assets.add(o['target_currency'])
            if str(o.get('user_order_id', '')).startswith('tc-'):
                self.oms.halt('UNJOURNALED_TRACK_C_ORDER')
        self.oms.sync_cash(self.account_available + self.account_reserved)
        self.oms.state['capital_at'] = time.time()
        self.account_at = self.last_account = time.time()

    def live_book(self, coin, now):
        """Last book while the stream is alive: Coinone pushes only on change.

        A book received after `now` was sampled (the feed runs concurrently) is current, not stale."""
        m = self.markets.get(coin)
        micro = m.micro if m else None
        if not self.connected or not micro or not micro.bids:
            return None
        age = max(0, now - micro.book_ms)
        if age > self.cfg['liveness_ms']:
            return None
        bid, ask = micro.bids[0][0], micro.asks[0][0]
        return dict(bid=bid, ask=ask, tick=float(price_unit(m.units, D(str(bid)))), age_ms=age)

    async def run(self, seconds=None):
        import urllib.request
        egress = await asyncio.to_thread(lambda: urllib.request.urlopen('https://checkip.amazonaws.com', timeout=10).read().decode().strip())
        if egress != self.cfg['expected_egress_ip']:
            raise RuntimeError('unauthorized egress IP')
        await self.refresh_account()
        await self.scan()
        if not self.markets:
            raise RuntimeError('no observable markets')
        self.store.event('START', config=self.cfg, policy='rule', rule=rule.VERSION, code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        sampler=asyncio.create_task(self.fair_sampling())
        tasks = [asyncio.create_task(t) for t in (self.feed(), self.candle_poll(), private_follow(self), self.recorder.run())]+[sampler]
        service_notify('READY=1')
        start = time.monotonic()
        try:
            while True:
                self.wakeup.clear()  # retain any new market wakeups during REST awaits
                now = int(time.time() * 1000)
                if (seconds is not None and time.monotonic() - start >= seconds) or (self.directory / 'STOP').exists():
                    self.stopping = True
                try:
                    if sampler.done() and not self.stopping:
                        sampler.result()
                        raise RuntimeError('Track C market sampler stopped unexpectedly')
                    self.sample_fairs()
                    private_events = self.counts['private_order_events']
                    self.oms.mark_residuals({c: b['bid'] for c in self.oms.state['residuals'] if (b := self.live_book(c, now))})
                    force = private_events != self.last_private_events
                    self.last_private_events = private_events
                    for coin in list(self.oms.campaigns):
                        c = self.oms.campaigns[coin]
                        book = self.live_book(coin, int(time.time() * 1000))
                        decision = self.holding_decision(c, book)
                        await asyncio.to_thread(self.oms.book(coin).drive, bid=book['bid'] if book else None, fresh=bool(book and self.connected),
                                                stopping=self.stopping, quantitative_decision=decision, force_reconcile=force or bool(c['exit_reason']))
                    if self.stopping and not self.oms.campaigns and not self.oms.active():
                        break
                    if time.time() - self.last_account >= 15:
                        await self.refresh_account()
                    if not self.oms.campaigns and time.time() - self.last_scan >= self.cfg['scan_seconds']:
                        await self.scan()
                    if not self.stopping and self.connected and self.storage_ok and time.time() - self.last_decision >= self.cfg['decision_ms'] / 1000:
                        await self.decisions()
                        self.last_decision = time.time()
                except CoinoneError as exc:
                    self.counts['account_errors'] += 1
                    self.account_at = 0
                    self.store.event('API_ERROR', error=str(exc))
                    if self.oms.campaigns:
                        self.oms.request_exit('account_error')
                if time.time() - self.last_report >= 30:
                    self.report()
                if self.storage_ok:
                    try:
                        for coin in self.cfg['coins']:
                            self.observations.frame(coin,int(time.time()*1000),self.markets[coin].micro if coin in self.markets else None,
                                self.last_fair.get(coin),self.oms.campaigns.get(coin),self.selection.get(coin),
                                (self.directory/'PAUSE').exists())
                    except (OSError,ValueError):
                        self.counts['observation_write_errors']+=1
                        self.storage_ok=False
                self.progress()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), .2)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.stopping = True
            self.recorder.stopping = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.report()
            if self.raw:
                self.raw.close()
            self.store.close()
            self.http_pool.close()
            self.observations.close()

    def report(self):
        now = int(time.time()*1000)
        if self.raw:
            self.raw.flush()
        size = sum(p.stat().st_size for p in (self.directory/"public").glob("*.gz"))
        self.storage_ok = size < self.cfg.get('public_storage_max_bytes',512*1024**2) and shutil.disk_usage(self.directory).free > 1024**3
        storage = None
        if self.cfg.get('policy')=='rule':
            from track_c.ops.storage import capacity
            storage = capacity(self.directory,self.cfg['public_storage_max_bytes'])
            self.storage_ok = storage['ok']
        result = dict(t_ms=now, mode=self.cfg["mode"], connected=self.connected,
                      funding_confirmed=self.cfg["funding_confirmed"], capital_krw=str(self.oms.equity),
                      capital_mode=self.cfg["capital_mode"], account_krw_available=str(self.account_available),
                      account_krw_reserved=str(self.account_reserved), order_cash_available=str(self.cash()),
                      external_capital_flows=self.oms.state["external_flows"], initial_equity=self.oms.state["initial_equity"],
                      position=self.oms.campaign, halt=self.oms.state["halt"], realized=self.oms.state["realized"],
                      today_realized=self.oms.state["day_realized"], counts=dict(self.counts), storage_ok=self.storage_ok,
                      markets={c:dict(fresh=m.fresh(now), counts=m.counts, features=m.features.f if m.features is not None else {}, fees=m.fees) for c,m in self.markets.items()})
        if storage is not None: result['storage']=storage
        path = self.directory / 'status.json'
        report = result
        report.update(entry_paused=(self.directory/'PAUSE').exists(),
                      selection=self.selection,asset_policy=ASSET_POLICY_VERSION,research_loss_day=self.oms.state['research_loss_day'])
        report.update(positions=self.oms.campaigns,reserved_buy_krw=str(self.oms.reserved_cash()),committed_risk_krw=str(self.oms.committed_risk()),
                      private_connected=self.private_connected,execution_feedback=self.execution_feedback,api_throttle_seconds=self.client._transport.wait_seconds)
        report.update(policy='rule', rule=dict(version=rule.VERSION, **{k: self.cfg[k] for k in rule.PARAMS}, coins=self.cfg['coins']),
                      fair={c: (dict(f, book_age_ms=(self.live_book(c, now) or {}).get('age_ms')) if f else None) for c, f in self.last_fair.items()},
                      leaders=dict(venues=self.recorder.state, counts=dict(self.recorder.counts), storage_ok=self.recorder.storage_ok),
                      residuals=self.oms.state['residuals'], model=None, model_issue=None)
        report['http_transport'] = self.http_pool.report()
        report['execution_version'] = EXECUTION_VERSION
        report['observations'] = dict(self.observations.counts)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(encoded(report) + '\n')
        tmp.replace(path)
        self.last_report = time.time()
