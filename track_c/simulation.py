"""Event replay of the production portfolio, OMS and quantitative policy.

Queue order is unobservable: conservative and cancellation-ahead upper scenarios
are separate runs. Synthetic inventory and PnL never reach the live ledger.
"""
import argparse
from bisect import bisect_right
from collections import Counter
from copy import deepcopy
from decimal import Decimal as D
import gzip
import hashlib
import json
import math
from pathlib import Path
import time

from .coinone import CoinoneError
from .dataset import public_contracts,rows
from .estimation import read
from .microstructure import Micro,liquidate
from .oms import TERMINAL
from .outcomes import Path as Tape
from .policy import Policy
from .portfolio import Portfolio
from .settings import load


class MemoryStore:
    def __init__(self,clock): self.state=None; self.events=[]; self.clock=clock
    def load(self): return deepcopy(self.state)
    def save(self,state,kind,**body): self.state=deepcopy(state); self.event(kind,**body)
    def event(self,kind,**body): self.events.append((int(self.clock()*1000),kind,deepcopy(body)))


class Exchange:
    def __init__(self,paths,clock,latency=250,optimistic=False,cash=594574):
        self.paths,self.clock,self.latency,self.optimistic=paths,clock,latency,optimistic
        self.orders={}; self.pending=[]; self.inventory={}; self.cash=D(cash)
    def submit(self,order):
        o=deepcopy(order); o.update(status='LIVE',executed_qty='0',average_executed_price='0',fee='0',remain_qty=o['qty'],
                                   order_id=o['cid'],quote_currency='KRW',target_currency=o['coin'],ahead=None,activated=False)
        self.orders[o['cid']]=o; self.pending.append((self.clock()*1000+self.latency,'submit',o['cid']))
        return dict(order_id=o['cid'])
    def detail(self,coin,cid): return deepcopy(self.orders[cid])
    def cancel(self,coin,cid):
        if not any(k=='cancel' and c==cid for _,k,c in self.pending): self.pending.append((self.clock()*1000+self.latency,'cancel',cid))
        return dict(result='success')
    def balances(self): return [dict(currency=c,available=str(q),limit='0') for c,q in self.inventory.items()]
    def fill(self,o,q,price):
        prior=D(o['executed_qty']); q=min(D(str(q)),D(o['qty'])-prior)
        if q<=0: return
        gross=prior*D(o['average_executed_price'])+q*D(str(price)); total=prior+q
        o.update(executed_qty=str(total),average_executed_price=str(gross/total),remain_qty=str(D(o['qty'])-total),
                 status='FILLED' if total==D(o['qty']) else 'PARTIALLY_FILLED')
        sign=1 if o['side']=='BUY' else -1
        self.inventory[o['coin']]=self.inventory.get(o['coin'],D(0))+sign*q
        self.cash-=sign*q*D(str(price))
    def sell(self,o,book,limit=0):
        for px,qty in book['bids']:
            if px<float(limit): break
            self.fill(o,qty,px)
            if o['status']=='FILLED': break
    def settle(self,t):
        due=[r for r in self.pending if r[0]<=t]; self.pending=[r for r in self.pending if r[0]>t]
        for at,action,cid in sorted(due):
            o=self.orders[cid]
            if o['status'] in TERMINAL: continue
            if action=='cancel': o.update(status='CANCELED',remain_qty='0'); continue
            book=self.paths[o['coin']].book_at(at)
            if not book: o.update(status='REJECTED',remain_qty='0'); continue
            o['activated']=True
            if o['side']=='BUY':
                if float(o['price'])>=book['asks'][0][0]: o.update(status='REJECTED',remain_qty='0')
                else: o['ahead']=sum(q for p,q in book['bids'] if p>=float(o['price']))
            elif o['type']=='LIMIT':
                # Post-only sale joins the visible queue at or below its price.
                if float(o['price'])<=book['bids'][0][0]: o.update(status='REJECTED',remain_qty='0')
                else: o['ahead']=sum(q for p,q in book['asks'] if p<=float(o['price']))
            elif o['type']=='MARKET':
                self.sell(o,book,o.get('limit_price',0)); o.update(status='FILLED' if D(o['executed_qty'])==D(o['qty']) else 'CANCELED',remain_qty='0')
            else: o['status']='NOT_TRIGGERED'
    def event(self,coin,event):
        self.settle(event['t'])
        for o in self.orders.values():
            if o['coin']!=coin or o['status'] in TERMINAL or not o['activated']: continue
            if o['side']=='BUY':
                if self.optimistic and event['kind']=='book': o['ahead']=min(o['ahead'],sum(q for p,q in event['bids'] if p>=float(o['price'])))
                if event['kind']=='trade' and not event['buy'] and event['price']<=float(o['price']):
                    consumed=min(o['ahead'],event['qty']); o['ahead']-=consumed
                    self.fill(o,event['qty']-consumed,float(o['price']))
            elif o['type']=='LIMIT':
                if event['kind']=='trade' and event['buy'] and event['price']>=float(o['price']):
                    consumed=min(o['ahead'],event['qty']); o['ahead']-=consumed
                    self.fill(o,event['qty']-consumed,float(o['price']))
            elif o['type']=='STOP_LIMIT' and event['kind']=='book':
                if o['status']!='NOT_TRIGGERED' or event['bids'][0][0]<=float(o['trigger_price']):
                    o['status']='LIVE'; self.sell(o,event,o['price'])


def run(folder,model_path,*,latency_ms=250,optimistic=False,gate=None,cost_bp=0.,cadence_ms=1000,control_from=None):
    folder=Path(folder); doc=read(model_path,int(time.time()*1000))
    meta=json.loads((folder/'dataset.json').read_text()); contracts,units=public_contracts(meta['spec']['contracts'])
    with gzip.open(folder/'paths.json.gz','rt') as stream: raw=json.load(stream)
    paths={c:Tape(v) for c,v in raw.items()}; events=sorted([(r['t'],c,i,r) for c,rr in raw.items() for i,r in enumerate(rr)])
    start=doc['trained_until']+1; end=meta['end']-max(doc['ttl_grid']+doc['horizon_grid'])*1000-3000
    cfg=load(Path(__file__).with_name('config.json')); cfg.update(mode='live',funding_confirmed=True,policy='quantitative',learning_enabled=True)
    clock=[events[0][0]/1000]; store=MemoryStore(lambda:clock[0]); client=Exchange(paths,lambda:clock[0],latency_ms,optimistic)
    portfolio=Portfolio(cfg,client,store,clock=lambda:clock[0]); portfolio.sync_cash(client.cash)
    policy=Policy(doc,cfg); micros={}; index=0; decisions=Counter(); peak=float(portfolio.equity); drawdown=0.; max_concurrent=0; n=0; censored=0
    schedule=None
    if control_from:
        reference=json.loads(Path(control_from).read_text())
        if reference['model']!=doc['digest']: raise ValueError('control model mismatch')
        schedule=Counter(r['t'] for r in reference['attempt_history'])
    previous_event=events[0][0]
    for t in range((events[0][0]//500+1)*500,end+3001,500):
        clock[0]=t/1000
        while index<len(events) and events[index][0]<=t:
            at,coin,ident,event=events[index]; index+=1; client.event(coin,event)
            if at-previous_event>120000: micros={}
            previous_event=at
            micro=micros.setdefault(coin,Micro(coin))
            common=dict(quote_currency='KRW',target_currency=coin,timestamp=event.get('exchange_t',at),id=ident+1)
            if event['kind']=='book': data=dict(common,bids=[dict(price=p,qty=q) for p,q in event['bids']],asks=[dict(price=p,qty=q) for p,q in event['asks']]); channel='ORDERBOOK'
            else: data=dict(common,price=event['price'],qty=event['qty'],is_seller_maker=event['buy']); channel='TRADE'
            micro.feed(channel,data,at)
        client.settle(t)
        if t<start: continue
        snaps={}
        for coin,micro in micros.items():
            if not micro.bids: continue
            ladder=units.get(coin,{}).get('rows',[dict(range_min=0,price_unit=contracts[coin]['price_unit'])])
            tick=float(max((r for r in ladder if float(r['range_min'])<=micro.bids[0][0]),key=lambda r:float(r['range_min']))['price_unit'])
            snap=micro.snapshot(t,tick)
            if snap: snaps[coin]=snap
        for coin in list(portfolio.campaigns):
            c=portfolio.campaigns[coin]; snap=snaps.get(coin); decision=dict(hold=False)
            if snap and float(c['qty'])>0:
                decision=policy.continuation(snap,float(c['qty']))
                px=liquidate(snap['bids'],float(c['qty']))
                if c['plan'].get('take_profit') and px is not None and px>=float(c['plan']['take_profit']): decision=dict(hold=True,take_profit=True)
            if not snap: censored+=1
            portfolio.book(coin).drive(bid=snap['bid'] if snap else None,fresh=bool(snap),quantitative_decision=decision,stopping=t>end)
        portfolio.state['capital_at']=clock[0]
        if t<=end and t%cadence_ms==0 and (schedule is None or schedule[t]):
            ranked=[]
            for coin,snap in snaps.items():
                if coin in portfolio.campaigns or (gate and not snap['baselines'].get(gate)): continue
                contract=contracts[coin]; ladder=units.get(coin,{}).get('rows',[dict(range_min=0,price_unit=contract['price_unit'])])
                result=policy.assess(snap,contract,ladder,dict(maker=0,taker=0),equity=portfolio.equity,
                                     cash=D(portfolio.state['cash_krw'])-portfolio.reserved_cash(),risk_remaining=portfolio.remaining_risk(),
                                     learning_spent=float(D(portfolio.state['research_loss_day'])+portfolio.committed_risk()))
                if schedule is not None:
                    # Time-matched neutral control: remove modeled-return ranking,
                    # retain market support/minimum/risk feasibility, use fixed
                    # 4s entry / 8s hold / 1tick and the smallest feasible size.
                    actions=[a for a in result.get('actions',[]) if a['supported'] and a['ttl']==4 and a['horizon']==8 and a.get('target_ticks')==1]
                    if actions:
                        a=min(actions,key=lambda a:a['quantity']); entry=snap['bid']
                        rank=int.from_bytes(hashlib.sha256(f'{t}:{coin}:neutral-v1'.encode()).digest()[:8],'big')
                        plan=dict(reason=None,qty=str(a['quantity']),entry=str(entry),stop=a['stop'],stop_limit=a['stop_limit'],maker='0',taker='0',
                                  policy='quantitative',model=doc['digest'],entry_ttl_s=4,hold_limit_s=12,horizon_s=8,target_ticks=1,
                                  take_profit=str(entry+snap['tick']),research=True,expected_net_bp=a['net_bp'],p_fill=a['p_fill'])
                        result=dict(accepted=True,reason='time_matched_neutral',best=dict(score=rank),plan=plan)
                    else: result=dict(accepted=False,reason='control_infeasible')
                decisions[result['reason']]+=1
                if result['accepted']: ranked.append((result['best']['score'],coin,result['plan'],snap))
            ranked=sorted(ranked,reverse=True,key=lambda r:r[0])
            if schedule is not None: ranked=ranked[:schedule[t]]
            for _,coin,plan,snap in ranked:
                if portfolio.enter(coin,plan,snap['features'],contracts[coin]['min_order_amount']): n+=1
        wealth=float(portfolio.equity); peak=max(peak,wealth); drawdown=max(drawdown,(peak-wealth)/peak)
        max_concurrent=max(max_concurrent,len(portfolio.campaigns))
    closed=[(t,b['campaign']) for t,k,b in store.events if k=='CLOSE']; fills=[b for _,k,b in store.events if k=='FILL']
    net=float(portfolio.state['realized']); turnover=sum(float(b['gross']) for b in fills); stressed=net-turnover*cost_bp/10000
    return dict(model=doc['digest'],scenario=dict(latency_ms=latency_ms,optimistic_queue=optimistic,extra_fee_bp_per_side=cost_bp,gate=gate,cadence_ms=cadence_ms,time_matched_control=bool(schedule)),
                start=start,end=end,decisions=dict(decisions),attempts=n,no_fill=sum(k=='NO_FILL' for _,k,_ in store.events),
                campaigns=len(closed),wins=sum(float(c['net'])>0 for _,c in closed),realized_krw=net,stress_net_krw=stressed,
                max_drawdown=drawdown,max_concurrent=max_concurrent,halt=portfolio.state['halt'],residual_positions=deepcopy(portfolio.campaigns),
                data_gap_drive_count=censored,log_growth=math.log(max(.001,(594574+stressed)/594574)),
                outcomes=[dict(t=t,coin=c['coin'],net=float(c['net']),reason=c['exit_reason']) for t,c in closed],
                attempt_history=[dict(t=t,coin=b['coin']) for t,k,b in store.events if k=='CAMPAIGN_INTENT'],
                limitations=['public queue counterfactual, not exchange fills','gate comparisons restrict the same C2 policy; not a full Hunter B backtest',
                             'fee stress is post-trade attribution and does not change admission','500ms supervisor cadence; pending API latency is simulated'])


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--dataset',required=True); p.add_argument('--model',required=True); p.add_argument('--output',required=True)
    p.add_argument('--latency-ms',type=int,default=250); p.add_argument('--optimistic',action='store_true'); p.add_argument('--gate',choices=['pre_gate','c1_110','b_111']); p.add_argument('--cost-bp',type=float,default=0); p.add_argument('--control-from')
    args=p.parse_args(); result=run(args.dataset,args.model,latency_ms=args.latency_ms,optimistic=args.optimistic,gate=args.gate,cost_bp=args.cost_bp,control_from=args.control_from)
    Path(args.output).write_text(json.dumps(result,indent=2)+'\n'); print(json.dumps({k:v for k,v in result.items() if k not in ('outcomes','residual_positions','attempt_history')}),flush=True)


if __name__=='__main__': main()
