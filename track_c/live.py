"""Explicit-mode C4 live owner using the reconciled portfolio and native stops.

structural_sampling preserves the original 0-tick minimum-lot collection rule.
execution_sampling uses a registered lower admission floor and the nearest safe
minimum lot without claiming learned alpha. learned requires frozen model support.
"""
import argparse
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from decimal import Decimal as D
import json
import math
from pathlib import Path
import signal
import time

from track_c.runtime import Runtime
from track_c.market.state import Market
from track_c.replay.evidence import read_model
from track_c.learning.features import candidates
from track_c.replay.engine import stage
from track_c.market.microstructure import liquidate
from track_c.execution.portfolio import Portfolio, Book
from track_c.execution.coinone import EntryExpired
from track_c.market.prices import price_floor
from track_c.settings import load
from track_c.ops.store import encoded

VERSION='c4-live-v2.3'
MODES=('structural_sampling','execution_sampling','learned')


class Journal:
    """Preserve the shared execution loop while identifying its C4 decision owner."""
    def __init__(self,store,model,mode,entry_ticks=None):
        self.store,self.model,self.mode,self.entry_ticks=store,model,mode,entry_ticks
    def __getattr__(self,name):return getattr(self.store,name)
    def event(self,kind,**fields):
        if kind=='START':
            fields.update(policy='c4',rule=VERSION,model=self.model,c4_live_mode=self.mode,
                          c4_entry_ticks=self.entry_ticks,automatic_retraining=False,
                          shared_execution_code=fields.pop('code',None))
        return self.store.event(kind,**fields)


def admission_state(state,cfg):
    """Recompute admission from the registered execution policy, without mutating model state."""
    if not state:return state
    ref=state.get('reference') or {}
    common_fall=ref.get('m10') is not None and ref['m10']<=-cfg['common_drop_ticks']
    eligible=bool(ref.get('ready') and ref.get('dev_ticks',float('-inf'))>=cfg['entry_ticks'] and not common_fall)
    return dict(state,entry_eligible=eligible)


def choose(state,cfg,cash,risk,model,mode):
    if mode not in MODES:raise ValueError('explicit C4 live decision mode required')
    state=admission_state(state,cfg)
    reason=stage(state,cfg)
    if reason:return dict(accepted=False,reason=reason,candidates=[])
    actions=candidates(state,cfg,float(cash),float(risk))
    values=[model.predict(a,state['t_ms'],len(actions)) for a in actions]
    ranked=[]
    for a,p in zip(actions,values):
        if mode=='structural_sampling':
            if a['offset']==0 and a['size']=='minimum':ranked.append((0.,a,p))
        elif mode=='execution_sampling':
            # The reference lower bound, freshness, depth, cash and risk gates have
            # already admitted these actions. Sample the closest legal minimum lot.
            if a['size']=='minimum':ranked.append((a['price'],a,p))
        elif p.get('ready') and p.get('score_krw',0)>0:ranked.append((p['score_krw'],a,p))
    if not ranked:return dict(accepted=False,reason='model_support_or_value' if actions else 'price_size_or_capacity',
                              candidates=[dict(action=a,prediction=p) for a,p in zip(actions,values)])
    _,action,pred=max(ranked,key=lambda row:row[0])
    return dict(accepted=True,reason=mode,action=action,prediction=pred,
                candidates=[dict(action=a,prediction=p) for a,p in zip(actions,values)])


def plan_for(action,state,model,mode,entry_ticks=None):
    text=lambda k:str(D(str(action[k])))
    target=price_floor(state['units'],D(str(state['reference']['lower']))-D(str(state['tick'])))
    return dict(reason=None,qty=text('qty'),entry=text('price'),stop=text('stop'),stop_limit=text('stop_limit'),
                take_profit=str(target),notional_krw=text('notional'),nominal_loss_krw=text('nominal_loss'),
                maker='0',taker='0',policy='rule',model=model,take_mode='market',research=True,
                entry_ttl_s=action['ttl_s'],hold_limit_s=action['hold_s'],horizon_s=action['hold_s'],
                tick=text('tick'),dev_ticks=action['dev_ticks'],c4_action=action,c4_mode=mode,
                c4_version=VERSION,c4_entry_ticks=entry_ticks,decision_t=state['t_ms'])


class LiveBook(Book):
    def submit(self,role,side,kind,qty,**fields):
        c=self.campaign
        if side=='SELL' and c:
            # Cancellation can race with a protective fill. Re-check the remainder
            # after reconciliation, before issuing any new sell identifier.
            if D(qty)*D(c['mark'])<D(c['minimum']) and not self.active():
                self.carry_residual();return None
            if role=='protect' and D(qty)*D(fields['price'])<D(c['minimum']):
                self.request_exit('protection_below_minimum');return None
        return super().submit(role,side,kind,qty,**fields)

    def carry_residual(self,*,no_fill=False):
        c=self.campaign
        if self.active():raise RuntimeError('C4 residual before order settlement')
        existing=self.state['residuals'].get(c['coin'])
        owned=dict(qty=c['qty'],cost=c['cost'],mark=c['mark'],mark_at=c.get('mark_at',0),t=self.clock())
        combined=dict(owned)
        if existing:
            combined.update(qty=str(D(owned['qty'])+D(existing['qty'])),cost=str(D(owned['cost'])+D(existing['cost'])))
        if D(combined['qty']):self.state['residuals'][c['coin']]=combined
        c['exit_reason']=c['exit_reason'] or 'dust'
        self.save('NO_FILL' if no_fill else 'CLOSE',campaign=c,residual=owned if D(owned['qty']) else None,
                  inventory_flat=not bool(D(owned['qty'])),c4_retained_total=combined)
        self.state.update(campaign=None,orders={},cooldown=self.clock())
        self.save('FLAT',inventory_flat=not bool(D(combined['qty'])))

    def drive(self,**kwargs):
        self.roll_day()
        self.reconcile(force=kwargs.get('force_reconcile',False))
        kwargs=dict(kwargs,force_reconcile=False)
        c=self.campaign;bid=kwargs.get('bid')
        if c and kwargs.get('fresh') and bid is not None and D(c['qty']) and D(c['qty'])*D(str(bid))<D(c['minimum']):
            if not self.active('entry'):
                for order in self.active():self.cancel(order)
                if not self.active():self.carry_residual()
                return
        return super().drive(**kwargs)


class LivePortfolio(Portfolio):
    def book(self,coin):
        if coin not in self.books:self.books[coin]=LiveBook(self,coin)
        return self.books[coin]


class LiveRunner(Runtime):
    def __init__(self,cfg):
        if cfg.get('c4_live_mode') not in MODES:raise ValueError('explicit C4 live mode required')
        self.artifact,self.c4cfg,self.models=read_model(cfg['c4_model_path'])
        if self.c4cfg['exit_protocol']!='protect_cancel_reconcile':
            raise ValueError('live owner requires protection-aware replay; direct is comparison-only')
        if cfg['coins']!=['BTC'] or float(cfg['notional_krw'])>20000:raise ValueError('C4 live universe/size mismatch')
        if any(float(cfg[k])!=float(self.c4cfg[k]) for k in ('risk_fraction','daily_loss_fraction','cash_fraction')):
            raise ValueError('C4 live risk must match frozen research constraints')
        self.entry_cfg=dict(self.c4cfg)
        if cfg['c4_live_mode']=='execution_sampling':
            value=cfg.get('c4_sampling_entry_ticks')
            if type(value) not in (int,float) or not math.isfinite(value) or not 1<=value<self.c4cfg['entry_ticks']:
                raise ValueError('execution sampling requires an explicit narrower entry threshold')
            self.entry_cfg['entry_ticks']=float(value)
        self.c4markets={coin:Market(coin,self.c4cfg) for coin in self.c4cfg['coins']}
        self.c4states={};self.c4slot=None;self.c4pending={}
        super().__init__(cfg)
        self.store=Journal(self.store,self.artifact['digest'],cfg['c4_live_mode'],self.entry_cfg['entry_ticks'])
        self.oms=LivePortfolio(cfg,self.client,self.store)
        self.oms.poll_interval=float(cfg.get('reconcile_poll_s',1.))
        self.last_decided=self.oms.state.setdefault('c4_decided_episode',{})

    @property
    def connected(self):return getattr(self,'_connected',False)

    @connected.setter
    def connected(self,value):
        if not value and getattr(self,'_connected',False):
            for coin,m in self.c4markets.items():
                m.micro.reset();m.reference.history.clear()
                m.reference.previous_mid=m.reference.previous_external=None
                m.risk=type(m.risk)(self.c4cfg)
                m.sells.clear();m.episode=m.latest=None
                self.c4pending.pop(coin,None);self.c4states[coin]=None
        self._connected=value

    def record(self,recv,message):
        super().record(recv,message)
        data=message.get('data') or {};coin=data.get('target_currency')
        if coin in self.c4markets:self.c4markets[coin].feed(message.get('channel'),data,recv)

    def observe_public(self,coin,data,recv,prior):
        # C4 has explicit unavailable-reference objects. The shared observation
        # writer accepts a priced reference or None; missing fair is not a WS error.
        fair=self.last_fair.get(coin)
        if fair and fair.get('fair') is None:fair=None
        if self.storage_ok:
            try:self.observations.public(coin,data,recv,prior,fair)
            except (OSError,ValueError):
                self.counts['observation_write_errors']+=1;self.storage_ok=False

    def on_leader(self,row):
        if row[0]=='b' and row[3] in self.c4markets:self.c4markets[row[3]].reference.quote(row)
        elif row[0]=='s':
            for m in self.c4markets.values():m.reference.quote(row)
        self.wakeup.set()

    def sample_fairs(self):
        now=time.time_ns()//1000000
        if self.c4slot==now//self.c4cfg['decision_ms']:return
        self.c4slot=now//self.c4cfg['decision_ms']
        for coin,m in self.c4markets.items():
            meta=self.markets.get(coin)
            s=m.snapshot(now,meta.contract,meta.units) if meta else None
            self.c4states[coin]=s
            self.last_fair[coin]=dict(s['reference'],risk=s['risk'],evaluated_ms=now) if s else None
            if s and s['new_episode']:self.c4pending[coin]=s['episode_id']

    def current_state(self,coin):
        meta=self.markets.get(coin)
        if not meta:return None
        return self.c4markets[coin].snapshot(time.time_ns()//1000000,meta.contract,meta.units)

    def entry_invalid_reason(self,coin,origin,state,plan,cash,risk,deadline):
        """Cheap validation on the event-loop thread; never run another prediction."""
        now=time.time_ns()//1000000
        age=self.c4cfg['book_max_age_ms']
        if time.monotonic()>deadline or not 0<=now-origin['t_ms']<=age:return 'decision_age'
        if not 0<=now-state['book_ms']<=age:return 'book_age'
        if (not self.connected or not self.private_connected or self.stopping or not self.storage_ok
                or self.cfg['mode']!='live' or not self.cfg['funding_confirmed']
                or (self.directory/'PAUSE').exists() or (self.directory/'STOP').exists()):return 'runtime_unavailable'
        if not 0<=time.time()-self.account_at<=5:return 'account_age'
        if coin in self.foreign_assets or self.oms.state['halt']:return 'ownership_or_halt'
        # The final transport check follows our durable INTENT. That reservation
        # may exist, but no other campaign/order or already submitted entry may.
        own=list(self.oms.campaigns.values())
        if any(c['plan'] is not plan or D(c['qty']) for c in own):return 'ownership_changed'
        orders=self.oms.active()
        if any(o['coin']!=coin or o['role']!='entry' or o['status']!='INTENT' for o in orders):return 'orders_changed'
        latest=self.current_state(coin)
        if (not latest or latest.get('episode_id')!=origin.get('episode_id')
                or latest['bids']!=state['bids'] or latest['asks']!=state['asks']):return 'market_changed'
        if not 0<=now-latest['book_ms']<=age:return 'book_age'
        latest=admission_state(latest,self.entry_cfg)
        reason=stage(latest,self.entry_cfg)
        if reason:return reason
        if any(float(v) for v in self.markets[coin].fees.values()):return 'unverified_nonzero_fees'
        reserved=sum(float(o['qty'])*float(o['price']) for o in orders)
        committed=sum(float(o['qty'])*(float(o['price'])-float(plan['stop_limit'])) for o in orders)
        available=min(cash,float(self.cash())+reserved)
        remaining=min(risk,float(self.oms.remaining_risk())+committed)
        legal=candidates(latest,self.entry_cfg,available,remaining)
        keys=('id','price','qty','stop','stop_limit')
        if not any(all(a[k]==plan['c4_action'][k] for k in keys) for a in legal):return 'action_changed'
        return None

    def submission_guard(self,coin,origin,state,plan,cash,risk,deadline):
        loop=asyncio.get_running_loop()
        async def validate():
            return self.entry_invalid_reason(coin,origin,state,plan,cash,risk,deadline)
        def check():
            # Called inside the worker and again after throttle/pool/TLS waits.
            # Market.snapshot mutates history, so access it only on its owner loop.
            remaining=deadline-time.monotonic()
            if remaining<=0:raise EntryExpired('decision_age')
            future=asyncio.run_coroutine_threadsafe(validate(),loop)
            try:reason=future.result(timeout=remaining)
            except FutureTimeout:
                future.cancel()
                raise EntryExpired('validation_wait_expired') from None
            if reason:raise EntryExpired(reason)
            if time.monotonic()>deadline:raise EntryExpired('decision_age')
        return check

    async def evaluate_entry(self,coin,ep,s):
        started=time.monotonic();timings={};expiry=None
        age=self.c4cfg['book_max_age_ms']
        deadline=started+max(0,min(s['t_ms'],s['book_ms'])+age-time.time_ns()//1000000)/1000
        model=self.models['refined'][1];mode=self.cfg['c4_live_mode']
        async def predict(phase,fn,*args):
            queued=time.monotonic()
            def compute():
                timings[phase+'_queue_ms']=(time.monotonic()-queued)*1000
                begin=time.perf_counter()
                try:return fn(*args)
                finally:timings[phase+'_ms']=(time.perf_counter()-begin)*1000
            # Model documents and the captured state are immutable during a run.
            # Long neighborhood scans must not stop receive-time market ingestion.
            return await asyncio.to_thread(compute)
        try:
            outcome=await predict('first_choose',choose,s,self.entry_cfg,self.cash(),self.oms.remaining_risk(),model,mode)
            if any(float(v) for v in self.markets[coin].fees.values()):outcome.update(accepted=False,reason='unverified_nonzero_fees')
            self.selection[coin]={k:v for k,v in outcome.items() if k not in ('candidates','action')}
            self.store.save(self.oms.state,'C4_DECISION',coin=coin,model=self.artifact['digest'],mode=mode,
                            admission_entry_ticks=self.entry_cfg['entry_ticks'],market_state=s,decision=outcome,
                            ep=ep,exchange_fills_verified=False)
            if not outcome['accepted']:return
            current=self.current_state(coin)
            if not current or current['bids']!=s['bids'] or current['asks']!=s['asks']:
                raise EntryExpired('market_changed')
            cash,risk=float(self.cash()),float(self.oms.remaining_risk())
            preliminary=plan_for(outcome['action'],s,self.artifact['digest'],mode,self.entry_cfg['entry_ticks'])
            reason=self.entry_invalid_reason(coin,s,current,preliminary,cash,risk,deadline)
            if reason:raise EntryExpired(reason)
            fresh=await predict('second_choose',choose,current,self.entry_cfg,cash,risk,model,mode)
            if (not fresh['accepted'] or
                    any(fresh['action'][k]!=outcome['action'][k] for k in ('id','price','qty','stop','stop_limit'))):
                raise EntryExpired('action_changed')
            plan=plan_for(fresh['action'],current,self.artifact['digest'],mode,self.entry_cfg['entry_ticks'])
            plan['c4_prediction']=fresh['prediction']
            plan['c4_probabilities']=await predict('survival',self.models['refined'][0].survival,fresh['action'])
            # All model work is now complete. Do not reset the decision lifetime.
            reason=self.entry_invalid_reason(coin,s,current,plan,cash,risk,deadline)
            if reason:raise EntryExpired(reason)
            guard=self.submission_guard(coin,s,current,plan,cash,risk,deadline)
            queued=time.monotonic()
            def enter():
                timings['worker_queue_ms']=(time.monotonic()-queued)*1000
                return self.oms.enter(coin,plan,current,self.markets[coin].contract['min_order_amount'],before_send=guard)
            entered=await asyncio.to_thread(enter)
            if not entered:self.store.event('C4_ENTRY_REJECTED',coin=coin,episode_id=ep,reason='portfolio_admission_guard')
        except EntryExpired as exc:
            expiry=str(exc)
            self.counts['c4_decision_expired']+=1
            self.selection[coin]=dict(reason='decision_expired',detail=expiry)
            self.store.event('C4_ENTRY_REJECTED',coin=coin,episode_id=ep,reason='decision_expired',detail=expiry,transmitted=False)
        finally:
            self.store.event('C4_DECISION_TIMING',coin=coin,episode_id=ep,model=self.artifact['digest'],
                             **timings,total_ms=(time.monotonic()-started)*1000,expiry_reason=expiry,
                             cash_rows=len(getattr(model,'doc',{}).get('rows',[])),
                             hazard_rows=len(getattr(self.models['refined'][0],'doc',{}).get('rows',[])))

    async def decisions(self):
        self.sample_fairs()
        self.selection={c:dict(reason='record_only') for c in self.markets if c not in self.c4markets}
        for coin in self.c4markets:
            ep=self.c4pending.get(coin);s=self.c4states.get(coin)
            if not ep or self.last_decided.get(coin)==ep:
                self.selection[coin]=dict(reason='waiting_new_episode');continue
            if (self.directory/'PAUSE').exists() or self.cfg['mode']!='live' or not self.cfg['funding_confirmed']:
                self.last_decided[coin]=ep  # do not replay a pre-activation shock on resume
                self.selection[coin]=dict(reason='entry_paused');continue
            # An episode is evaluated once, including a rejected episode.
            self.last_decided[coin]=ep
            if self.oms.campaigns or self.oms.active() or coin in self.foreign_assets:
                self.selection[coin]=dict(reason='owned_inventory_or_external_ownership');continue
            if not 0<=time.time()-self.account_at<=5:await self.refresh_account()
            s=self.current_state(coin)
            if not s or s.get('episode_id')!=ep:
                self.selection[coin]=dict(reason='episode_expired');continue
            await self.evaluate_entry(coin,ep,s)

    def holding_decision(self,c,book):
        s=self.current_state(c['coin']);ref=(s or {}).get('reference',{})
        now=time.time_ns()//1000000;action=c['plan']['c4_action'];qty=float(c['qty'])
        fresh=bool(s and s.get('entry_fresh') and book and self.connected)
        age=now/1000-c['first_fill'] if c['first_fill'] is not None else None
        cancel=not fresh or not ref.get('ready') or ref.get('lower',0)<=action['price']
        if fresh and qty*min(s['bid'],action['stop_limit'])>=action['minimum']*1.05:cancel=True
        reason=None;target=None;value=None
        if age is not None:
            if not fresh or not ref.get('ready'):reason='market_data_unavailable'
            elif s['bid']<=action['stop']:reason='stop'
            elif ref['m10']<=-self.c4cfg['common_drop_ticks'] or ref['upper']<action['price']:reason='premise'
            elif age>=action['hold_s']:reason='time'
            else:
                vwap=liquidate([(p,q*self.c4cfg['depth_haircut']) for p,q in s['bids']],qty) if qty else None
                if vwap is not None and vwap>=ref['lower']-s['tick']:
                    target=price_floor(s['units'],D(str(ref['lower']))-D(str(s['tick'])))
                    c['plan']['take_profit']=str(target)
                elif vwap is not None and self.cfg['c4_live_mode']=='learned' and not self.oms.book(c['coin']).active('entry'):
                    value=self.models['refined'][0].continuation(action,age,now,vwap,state=s,qty=qty)
                    if value.get('ready') and not value['hold']:reason='continuation_value'
        return dict(hold=reason is None,reason=reason,take_profit=target is not None,cancel_entry=cancel,
                    recovery_ready=fresh and ref.get('ready',False),observed_ms=now,c4_value=value)

    def report(self):
        super().report()
        path=self.directory/'status.json';r=json.loads(path.read_text())
        r.update(policy='c4',model=dict(digest=self.artifact['digest'],state='frozen_'+self.cfg['c4_live_mode']),execution_version=VERSION,
                 c4_live_mode=self.cfg['c4_live_mode'],automatic_retraining=False,
                 c4=dict(version=self.artifact['version'],frozen=True,coins=['BTC'],
                         admission_entry_ticks=self.entry_cfg['entry_ticks'],minimum_size_only=self.cfg['c4_live_mode']=='execution_sampling',
                         readiness='evaluated_per_candidate',live_calibration='collecting_actual_orders; not_yet_validated'))
        r['rule']['version']=VERSION
        tmp=path.with_suffix('.tmp');tmp.write_text(encoded(r)+'\n');tmp.replace(path)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--seconds',type=float)
    a=p.parse_args();runner=LiveRunner(load(a.config))
    def stop(*_):runner.stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    asyncio.run(runner.run(a.seconds))


if __name__=='__main__':main()
