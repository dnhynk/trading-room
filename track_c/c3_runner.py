"""C3 production owner: cross-venue fair value rule over the reconciled spot portfolio.

Records Coinone (inherited) and leader venues (embedded Recorder) itself, so the
standalone leader recorder service must not run concurrently.
"""
import argparse
import asyncio
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import signal
import time

from .coinone import CoinoneError, decimal
from .fair import FairValue
from .leaders import Recorder
from .marketdata import Market
from .private_stream import follow as private_follow
from .quant_runner import QuantRunner
from . import rule
from .settings import load
from .sizing import price_unit
from .store import encoded
from .universe import ASSET_POLICY_VERSION
from .http_pool import HTTPSPool
from .service_health import notify as service_notify
from .c3_observations import Observations
from .c3_identity import EXECUTION_VERSION


class RuleRunner(QuantRunner):
    def __init__(self, cfg):
        self.fairs = {c: FairValue(window_s=cfg['ratio_window_s'], min_samples=cfg['ratio_min_samples'], leader_max_age_ms=cfg['leader_max_age_ms'],
                                   weights=cfg.get('leader_weights'), price=cfg.get('leader_price', 'microprice'),
                                   momentum_window_s=cfg['momentum_window_s'], momentum_recent_s=cfg['momentum_recent_s'], exit_config=cfg) for c in cfg['coins']}
        self.last_fair = {}
        self.last_value_eval = {}
        self.last_private_events = 0
        super().__init__(cfg)
        self.http_pool = HTTPSPool()
        self.client._transport.send = self.http_pool
        self.client._timeout = float(cfg.get('http_timeout_s',3))
        self.last_watchdog = 0
        self.observations = Observations(self.directory)
        self.recorder = Recorder(self.directory, sink=self.on_leader)
        # Resting orders are re-read at most once per second unless the private stream reports a change.
        self.oms.poll_interval = float(cfg.get('reconcile_poll_s', 1.0))

    def adopt_model(self):
        self.policy, self.model_issue = None, None

    def on_leader(self, row):
        if row[0] == 'b' and row[3] in self.fairs:
            self.fairs[row[3]].leader_quote(row[2], row[1], row[5], row[7], row[6], row[8])
        elif row[0] == 's' and row[4] == 'disconnected':
            for fair in self.fairs.values():
                fair.disconnect(row[2])
        if (row[0]=='b' and row[3] in self.fairs) or row[0]=='s':
            self.wakeup.set()

    async def candle_poll(self):
        # C3 uses receive-time books/trades; legacy REST candles never affect its rule.
        return

    def progress(self):
        # Called only after a bounded operation has returned, never by a detached
        # timer that could hide a blocked order/reconciliation call.
        if time.monotonic()-self.last_watchdog>=1:
            service_notify('WATCHDOG=1')
            self.last_watchdog=time.monotonic()

    def sample_fairs(self):
        for coin, model in self.fairs.items():
            fair=self.fair_for(coin,int(time.time()*1000)) if coin in self.markets else None
            if fair is None:
                model.reset_momentum()  # never bridge a known missing reference/book
            self.last_fair[coin]=fair

    async def fair_sampling(self):
        # Same event loop as market callbacks, independent of serialized REST
        # awaits. FairValue keeps one causal sample per second; no backfilling.
        # This task deliberately does not feed the execution watchdog.
        while not self.stopping:
            self.sample_fairs()
            await asyncio.sleep(.2)

    def observe_public(self,coin,data,recv,prior):
        if self.storage_ok:
            try:
                self.observations.public(coin,data,recv,prior,self.last_fair.get(coin))
            except (OSError,ValueError):
                self.counts['observation_write_errors']+=1
                self.storage_ok=False

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

    def fair_for(self, coin, now):
        book = self.live_book(coin, now)
        if not book:
            return None
        return self.fairs[coin].evaluate(now, (book['bid'] + book['ask']) / 2, book['tick'], bid=book['bid'], ask=book['ask'])

    def evaluate(self, snapshot):
        coin = snapshot['coin']
        m = self.markets[coin]
        fair = self.last_fair.get(coin)
        cash = self.cash() if self.cfg['funding_confirmed'] else self.oms.equity  # observe mode: hypothetical sizing
        result = rule.assess(self.cfg, coin=coin, bid=snapshot['bid'], ask=snapshot['ask'], tick=snapshot['tick'],
                             dev=fair['dev_ticks'] if fair else None, contract=m.contract, units=m.units,
                             cash=cash, risk_remaining=self.oms.remaining_risk(coin), flow32=snapshot['features'].get('flow_32'),
                             m30=(fair or {}).get('m30'), m10=(fair or {}).get('m10'), risk=(fair or {}).get('risk'))
        if any(float(v) for v in m.fees.values()):
            result = dict(result, accepted=False, reason='unreconciled_fee_currency')
        result['t'] = snapshot['t']
        result['fair'] = fair
        return result

    def holding_decision(self, c, book):
        coin = c['coin']
        fair = self.last_fair.get(coin)
        dev = fair['dev_ticks'] if fair else None
        now = int(time.time()*1000)
        snap = self.snapshot(coin, now) if coin in self.markets else None
        motion = dict(m30=(fair or {}).get('m30'), m10=(fair or {}).get('m10'),
                      flow32=snap['features'].get('flow_32') if snap else None)
        # Reconcile in drive may discover the first fill after this is calculated.
        age = now/1000 - c['first_fill'] if c['first_fill'] is not None else None
        args = dict(dev=dev, bid=book['bid'] if book else None, entry=float(c['plan']['entry']),
                    tick=float(c['plan'].get('tick', 1)), stop=c['stop'], age_s=age, **motion)
        decision = rule.hold(self.cfg, **args)
        # Protective exits do not wait for historical counterfactual work. Evaluate
        # value at most once per second; never reuse a conclusion for a later book.
        if decision['hold'] and age is not None and book and self.cfg.get('value_exit'):
            key = (c['id'], now//1000)
            if self.last_value_eval.get(coin) != key:
                continuation = self.fairs[coin].continuation(now, bid=book['bid'], ask=book['ask'], tick=book['tick'],
                    stop=c['stop'], stop_limit=c['stop_limit'], take=c['plan']['take_profit'], age_s=age)
                self.last_value_eval[coin] = key
                decision = rule.hold(self.cfg, continuation=continuation, **args)
        decision.update(cancel_entry=rule.cancel_entry(self.cfg, dev=dev, risk=(fair or {}).get('risk'), **motion), dev_ticks=dev, **motion)
        decision['recovery_ready'] = bool(book and fair and rule.momentum_available(motion['m30'],motion['m10']))
        decision['observed_ms'] = now
        return decision

    async def decisions(self):
        now = int(time.time() * 1000)
        candidates, selection = [], {}
        for coin in list(self.markets):
            if coin not in self.fairs:
                selection[coin] = dict(reason='record_only')
                continue
            if coin in self.oms.campaigns:
                f = self.last_fair.get(coin) or {}
                snap = self.snapshot(coin, now)
                selection[coin] = dict(reason='campaign_active', dev_ticks=f.get('dev_ticks'), m30=f.get('m30'), m10=f.get('m10'),
                                       flow32=snap['features'].get('flow_32') if snap else None)
                continue
            if coin in self.foreign_assets:
                selection[coin] = dict(reason='external_ownership')
                continue
            snap = self.snapshot(coin, now)
            if not snap:
                f = self.last_fair.get(coin) or {}
                selection[coin] = dict(reason='stale_book', dev_ticks=f.get('dev_ticks'), m30=f.get('m30'), m10=f.get('m10'), flow32=None)
                continue
            result = self.evaluate(snap)
            selection[coin] = {k: v for k, v in result.items() if k not in ('plan',)}
            if result['accepted']:
                candidates.append((result['best']['score'], coin))
        self.selection = selection
        if time.time() - self.last_decision_log >= 30:
            self.store.event('SELECTION', policy=rule.VERSION, candidates=selection)
            self.last_decision_log = time.time()
        if not candidates or self.cfg['mode'] != 'live' or not self.cfg['funding_confirmed'] or (self.directory / 'PAUSE').exists():
            return
        for _, coin in sorted(candidates, reverse=True):
            await self.submit_candidate(coin)

    async def submit_candidate(self, coin):
        if not 0 <= time.time() - self.account_at <= 5:
            await self.refresh_account()
        now = int(time.time() * 1000)
        self.last_fair[coin] = self.fair_for(coin, now)
        snap = self.snapshot(coin, now)
        if not snap or coin in self.foreign_assets or not self.connected or coin in self.oms.campaigns:
            return
        result = self.evaluate(snap)
        checked = int(time.time() * 1000)
        current = self.snapshot(coin, checked)
        if not result['accepted'] or not current or checked - snap['t'] > self.cfg['quote_max_age_ms'] or current['bids'] != snap['bids'] or current['asks'] != snap['asks']:
            self.counts['decision_expired'] += 1
            return
        if (self.directory / 'PAUSE').exists() or self.stopping or not self.storage_ok:
            return
        plan = result['plan']
        plan['decision_t'] = snap['t']
        plan['fair'] = result['fair']
        self.store.event('RULE_DECISION', coin=coin, policy=rule.VERSION, plan=plan, fair=result['fair'],
                         m30=result['m30'], m10=result['m10'], flow32=result['flow32'],
                         book=dict(t=snap['t'], bid=snap['bid'], ask=snap['ask'], tick=snap['tick'], spread_ticks=snap['features']['spread_ticks']),
                         equity=str(self.oms.equity), cash=str(self.cash()))
        await asyncio.to_thread(self.oms.enter, coin, plan, snap['features'], self.markets[coin].contract['min_order_amount'])

    def report(self):
        super().report()
        path = self.directory / 'status.json'
        report = json.loads(path.read_text())
        now = int(time.time() * 1000)
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
                        raise RuntimeError('C3 fair sampler stopped unexpectedly')
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
                        for coin in self.fairs:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--seconds', type=float)
    args = parser.parse_args()
    cfg = load(args.config)
    if cfg.get('policy') != 'rule':
        raise SystemExit('config policy must be rule')
    runner = RuleRunner(cfg)
    def stop(*_):
        runner.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    asyncio.run(runner.run(args.seconds))


if __name__ == '__main__':
    main()
