"""C2 production owner: model decisions over the existing reconciled spot OMS."""
import argparse
import asyncio
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import signal
import time

from .coinone import CoinoneError, decimal
from .estimation import read as read_model
from .marketdata import Market
from .microstructure import liquidate
from .policy import Policy
from .runner import Runner
from .settings import load
from .sizing import price_unit
from .store import encoded
from .universe import ASSET_POLICY_VERSION, coverage
from .private_stream import follow as private_follow
from .rate_limit import Transport


class QuantRunner(Runner):
    def __init__(self,cfg):
        super().__init__(cfg)
        self.client._transport=Transport(self.client._transport)
        self.policy=None; self.model_mtime=None; self.model_issue='model_unavailable'; self.policies={}
        self.selection={}; self.coverage_reasons={}; self.last_decision=0; self.last_decision_log=0
        self.private_connected=False; self.wakeup=asyncio.Event(); self.execution_feedback={}; self.feedback_at=0
        self.adopt_model()

    def adopt_model(self):
        path=Path(self.cfg['model_path'])
        try:
            modified=path.stat().st_mtime_ns
            if self.policy and modified==self.model_mtime and self.model_issue in (None,'model_expired'):
                if time.time()*1000-self.policy.artifact['trained_until']>self.cfg.get('model_max_age_seconds',21600)*1000:
                    self.model_issue='model_expired'
                return
            doc=read_model(path,int(time.time()*1000))
            self.policy=Policy(doc,self.cfg); self.model_mtime=modified; self.model_issue=None
            self.policies[doc['digest']]=self.policy
            for c in self.oms.campaigns.values():
                key=c['plan']['model']
                if key not in self.policies: self.policies[key]=Policy(read_model(path.parent/(key+'.json'),int(time.time()*1000)),self.cfg)
            keep={doc['digest']}|{c['plan']['model'] for c in self.oms.campaigns.values()}
            self.policies={key:value for key,value in self.policies.items() if key in keep}
            self.store.event('MODEL_ADOPTED',digest=doc['digest'],state=doc['state'],trained_until=doc['trained_until'])
        except (OSError,ValueError,KeyError,TypeError):
            self.model_issue='model_unavailable_or_invalid'
            # Keep a pinned model for existing inventory; disable new entries until fixed.

    async def scan(self):
        contracts,tickers=await asyncio.to_thread(self.client.universe)
        current=set(self.oms.campaigns)
        desired,self.coverage_reasons=coverage(contracts,tickers,existing=current,foreign=self.foreign_assets,
                                               benchmarks=self.cfg['benchmark_symbols'],limit=self.cfg['max_symbols'])
        by={r['target_currency']:r for r in contracts}; added={}
        captured=[]
        for coin in desired:
            try:
                fees=await asyncio.to_thread(self.client.fees,coin)
                units=await asyncio.to_thread(self.client.price_units,coin)
                if coin in self.markets:
                    market=self.markets[coin]; market.fees,market.units,market.contract=fees,units,by[coin]
                else:
                    candles=await asyncio.to_thread(self.client.candles,coin)
                    market=Market(coin,self.cfg,by[coin],units,fees,candles)
                added[coin]=market
                captured.append(dict(coin=coin,available_ms=int(time.time()*1000),contract=by[coin],units=units,fees=fees))
            except (CoinoneError,ValueError,KeyError,TypeError):
                self.coverage_reasons[coin]='metadata_unavailable'; self.counts['scan_market_error']+=1
                if coin in current and coin in self.markets: added[coin]=self.markets[coin]
        for coin in current:
            if coin not in added and coin in self.markets: added[coin]=self.markets[coin]
        self.markets=added; self.generation+=1; self.last_scan=time.time()
        directory=self.directory/'contracts'; directory.mkdir(exist_ok=True)
        (directory/(str(int(time.time()*1000))+'.json')).write_text(encoded(dict(asset_policy=ASSET_POLICY_VERSION,markets=captured))+'\n')
        self.store.event('SCAN',symbols=list(added),excluded=sorted(self.foreign_assets),asset_policy=ASSET_POLICY_VERSION,
                         acquisition_only=True,reasons=self.coverage_reasons)

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
                        self.record(recv,msg); self.markets[coin].feed(channel,data,recv); self.counts['messages']+=1
                    backoff=1
            except Exception:
                self.counts['ws_errors']+=1
            finally:
                self.connected=False
                for market in self.markets.values(): market.micro.reset()
            if not self.stopping:
                await asyncio.sleep(backoff); backoff=min(15,backoff*2)

    def snapshot(self,coin,now):
        market=self.markets[coin]
        if not market.micro.bids: return None
        tick=price_unit(market.units,D(str(market.micro.bids[0][0])))
        return market.micro.snapshot(now,float(tick))

    def evaluate(self,snapshot):
        m=self.markets[snapshot['coin']]
        return self.policy.assess(snapshot,m.contract,m.units,m.fees,equity=self.oms.equity,cash=self.cash(),risk_remaining=self.oms.remaining_risk(),
                                  learning_spent=float(D(self.oms.state['research_loss_day'])+self.oms.committed_risk()))

    async def refresh_account(self):
        # Serialize this with drive/enter. Cumulative executions own cash updates.
        for coin in list(self.oms.campaigns): await asyncio.to_thread(self.oms.book(coin).reconcile)
        rows=await asyncio.to_thread(self.client.balances); orders=await asyncio.to_thread(self.client.active_orders)
        krw=[r for r in rows if r.get('currency')=='KRW']
        if len(krw)!=1: raise CoinoneError('KRW balance missing or ambiguous')
        self.account_available=decimal(krw[0]['available']); self.account_reserved=decimal(krw[0]['limit'])
        self.foreign_assets=set(self.cfg['excluded_symbols'])
        for row in rows:
            coin=row['currency']
            if coin!='KRW' and coin not in self.oms.campaigns and decimal(row['available'])+decimal(row['limit'])>0: self.foreign_assets.add(coin)
        ids=set(self.oms.state['orders']); exchange_ids={o.get('exchange_id') for o in self.oms.state['orders'].values()}
        for o in orders:
            if o.get('user_order_id') in ids or (o.get('order_id') and o['order_id'] in exchange_ids): continue
            self.foreign_assets.add(o['target_currency'])
            if str(o.get('user_order_id','')).startswith('tc-'): self.oms.halt('UNJOURNALED_TRACK_C_ORDER')
        self.oms.sync_cash(self.account_available+self.account_reserved)
        self.oms.state['capital_at']=time.time()
        self.account_at=self.last_account=time.time()

    def cash(self):
        return max(D(0),min(D(self.oms.state['cash_krw'])-self.oms.reserved_cash(),self.account_available)) if self.cfg['funding_confirmed'] else D(0)

    def holding_decision(self,c,snap):
        pinned=self.policies.get(c['plan']['model'])
        if not pinned or not snap: return dict(hold=False,reason='model_or_data_missing')
        q=float(c['qty']); target=c['plan'].get('take_profit')
        executable=liquidate(snap['bids'],q) if q>0 else None
        hit=bool(target and executable is not None and executable>=float(target))
        decision=pinned.continuation(snap,q) if q>0 else dict(hold=True)
        # Profit is available now: take it instead of asking for a larger move.
        if hit: decision.update(hold=True,take_profit=True)
        return decision

    async def decisions(self):
        now=int(time.time()*1000); candidates=[]; selection={}
        if time.time()-self.feedback_at>=30:
            try: self.execution_feedback=json.loads((self.directory/'models/execution.json').read_text())
            except (OSError,ValueError): self.execution_feedback={}
            self.feedback_at=time.time()
        if self.execution_feedback.get('negative_drift') and self.policy and self.execution_feedback.get('drift_model')==self.policy.artifact['digest']:
            self.selection={coin:dict(reason='negative_execution_drift') for coin in self.markets}; return
        if not self.policy or self.model_issue: self.selection={coin:dict(reason=self.model_issue) for coin in self.markets}; return
        for coin in list(self.markets):
            if coin in self.oms.campaigns: selection[coin]=dict(reason='campaign_active'); continue
            if coin in self.foreign_assets: selection[coin]=dict(reason='external_ownership'); continue
            snap=self.snapshot(coin,now)
            if not snap: selection[coin]=dict(reason='stale_book'); continue
            result=await asyncio.to_thread(self.evaluate,snap)
            selection[coin]={k:v for k,v in result.items() if k not in ('actions','plan')}
            if result['accepted']: candidates.append((result['best']['score'],coin,snap,result))
        self.selection=selection
        if time.time()-self.last_decision_log>=30:
            self.store.event('SELECTION',model=self.policy.artifact['digest'],candidates=selection)
            self.last_decision_log=time.time()
        if not candidates or self.cfg['mode']!='live' or not self.cfg['funding_confirmed'] or (self.directory/'PAUSE').exists(): return
        for _,coin,prior,_ in sorted(candidates,reverse=True,key=lambda row:row[0]):
            await self.submit_candidate(coin)

    async def submit_candidate(self,coin):
        await self.refresh_account()
        self.markets[coin].fees=await asyncio.to_thread(self.client.fees,coin)
        now=int(time.time()*1000); snap=self.snapshot(coin,now)
        if not snap or coin in self.foreign_assets or not self.connected or coin in self.oms.campaigns or self.model_issue: return
        result=await asyncio.to_thread(self.evaluate,snap)
        checked=int(time.time()*1000)
        current=self.snapshot(coin,checked)
        if not result['accepted'] or not current or checked-snap['t']>self.cfg['quote_max_age_ms'] or current['bids']!=snap['bids'] or current['asks']!=snap['asks']:
            self.counts['decision_expired']+=1; return
        if (self.directory/'PAUSE').exists() or self.stopping or not self.storage_ok: return
        plan=result['plan']; plan['decision_t']=snap['t']; plan['decision_features']=snap['features']
        self.store.event('MODEL_DECISION',coin=coin,model=self.policy.artifact['digest'],snapshot=snap,plan=plan,
                         alternatives=result['actions'],equity=str(self.oms.equity),cash=str(self.cash()))
        await asyncio.to_thread(self.oms.enter,coin,plan,snap['features'],self.markets[coin].contract['min_order_amount'])

    def report(self):
        super().report()
        path=self.directory/'status.json'; report=json.loads(path.read_text())
        report.update(policy='quantitative',entry_paused=(self.directory/'PAUSE').exists(),model_issue=self.model_issue,
                      model=dict(digest=self.policy.artifact['digest'],state=self.policy.artifact['state'],trained_until=self.policy.artifact['trained_until']) if self.policy else None,
                      selection=self.selection,asset_policy=ASSET_POLICY_VERSION,research_loss_day=self.oms.state['research_loss_day'])
        report.update(positions=self.oms.campaigns,reserved_buy_krw=str(self.oms.reserved_cash()),committed_risk_krw=str(self.oms.committed_risk()),
                      private_connected=self.private_connected,execution_feedback=self.execution_feedback,api_throttle_seconds=self.client._transport.wait_seconds)
        tmp=path.with_suffix('.tmp'); tmp.write_text(encoded(report)+'\n'); tmp.replace(path)

    async def run(self,seconds=None):
        import urllib.request
        egress=await asyncio.to_thread(lambda:urllib.request.urlopen('https://checkip.amazonaws.com',timeout=10).read().decode().strip())
        if egress!=self.cfg['expected_egress_ip']: raise RuntimeError('unauthorized egress IP')
        await self.refresh_account(); await self.scan()
        self.store.event('START',config=self.cfg,policy='quantitative',code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        feed=asyncio.create_task(self.feed()); candles=asyncio.create_task(self.candle_poll()); private=asyncio.create_task(private_follow(self)); start=time.monotonic()
        try:
            while True:
                now=int(time.time()*1000)
                if (seconds is not None and time.monotonic()-start>=seconds) or (self.directory/'STOP').exists(): self.stopping=True
                try:
                    for coin in list(self.oms.campaigns):
                        c=self.oms.campaigns[coin]; snap=self.snapshot(coin,int(time.time()*1000)) if coin in self.markets else None
                        decision=self.holding_decision(c,snap)
                        await asyncio.to_thread(self.oms.book(coin).drive,bid=snap['bid'] if snap else None,fresh=bool(snap and self.connected),
                                                stopping=self.stopping,quantitative_decision=decision)
                    if self.stopping and not self.oms.campaigns and not self.oms.active(): break
                    if time.time()-self.last_account>=15: await self.refresh_account()
                    self.adopt_model()
                    if not self.oms.campaigns and time.time()-self.last_scan>=self.cfg['scan_seconds']: await self.scan()
                    if not self.stopping and self.connected and self.storage_ok and time.time()-self.last_decision>=self.cfg.get('decision_seconds',1):
                        await self.decisions(); self.last_decision=time.time()
                except CoinoneError as exc:
                    self.counts['account_errors']+=1; self.account_at=0
                    self.store.event('API_ERROR',error=str(exc))
                    if self.oms.campaign: self.oms.request_exit('account_error')
                if time.time()-self.last_report>=30: self.report()
                self.wakeup.clear()
                try: await asyncio.wait_for(self.wakeup.wait(),.2)
                except asyncio.TimeoutError: pass
        finally:
            self.stopping=True; feed.cancel(); candles.cancel(); private.cancel()
            await asyncio.gather(feed,candles,private,return_exceptions=True)
            self.report()
            if self.raw: self.raw.close()
            self.store.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--config',required=True); parser.add_argument('--seconds',type=float)
    args=parser.parse_args(); runner=QuantRunner(load(args.config))
    def stop(*_): runner.stopping=True
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    asyncio.run(runner.run(args.seconds))


if __name__=='__main__': main()
