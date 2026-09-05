"""Serialized multi-symbol ownership over one durable cash and risk ledger."""
from collections.abc import MutableMapping
from decimal import Decimal as D
import datetime as dt
import time

from .oms import OMS, TERMINAL


class BookState(MutableMapping):
    """Views share cash/PnL; campaign and orders are scoped to one symbol."""
    def __init__(self, portfolio, coin): self.owner,self.coin=portfolio,coin
    def __getitem__(self,key):
        state=self.owner.state
        if key=='campaign': return state['campaigns'].get(self.coin)
        if key=='orders': return {k:o for k,o in state['orders'].items() if o['coin']==self.coin}
        if key=='cooldown': return 0
        return state[key]
    def __setitem__(self,key,value):
        state=self.owner.state
        if key=='campaign':
            if value is None: state['campaigns'].pop(self.coin,None)
            else: state['campaigns'][self.coin]=value
        elif key=='orders':
            # OMS mutates order values directly, but insertion needs a real map.
            for oid in list(state['orders']):
                if state['orders'][oid]['coin']==self.coin: del state['orders'][oid]
            state['orders'].update(value)
        elif key!='cooldown': state[key]=value
    def __delitem__(self,key): raise TypeError('state fields cannot be deleted')
    def __iter__(self): return iter(self.owner.state)
    def __len__(self): return len(self.owner.state)


class OrderView(MutableMapping):
    def __init__(self,state,coin): self.state,self.coin=state,coin
    def __getitem__(self,key):
        row=self.state['orders'][key]
        if row['coin']!=self.coin: raise KeyError(key)
        return row
    def __setitem__(self,key,value):
        if value['coin']!=self.coin: raise ValueError('cross-symbol order')
        self.state['orders'][key]=value
    def __delitem__(self,key): self.__getitem__(key); del self.state['orders'][key]
    def __iter__(self): return (k for k,v in self.state['orders'].items() if v['coin']==self.coin)
    def __len__(self): return sum(1 for _ in self)


class ScopedState(BookState):
    def __getitem__(self,key):
        return OrderView(self.owner.state,self.coin) if key=='orders' else super().__getitem__(key)


class BookStore:
    def __init__(self,owner,coin): self.owner,self.coin=owner,coin
    def save(self,state,kind,**fields):
        c=state['campaign']
        fields.setdefault('coin',self.coin)
        if c: fields.setdefault('campaign_id',c['id'])
        self.owner.store.save(self.owner.state,kind,**fields)
    def event(self,kind,**fields):
        fields.setdefault('coin',self.coin)
        self.owner.store.event(kind,**fields)


class Book(OMS):
    def __init__(self,owner,coin):
        self.owner=owner
        self.config,self.client,self.clock=owner.config,owner.client,owner.clock
        self.state,self.store=ScopedState(owner,coin),BookStore(owner,coin)
    @property
    def poll_interval(self): return self.owner.poll_interval
    @poll_interval.setter
    def poll_interval(self,value): self.owner.poll_interval=value
    @property
    def equity(self): return self.owner.equity
    def roll_day(self): self.owner.roll_day()
    def remaining_risk(self,mark=None): return self.owner.daily_remaining()


class Portfolio:
    poll_interval=0.0
    def __init__(self,config,client,store,*,clock=time.time):
        self.config,self.client,self.store,self.clock=config,client,store,clock
        state=store.load()
        if state is None or state.get('version') in (1,2):
            old=OMS(config,client,store,clock=clock)
            if old.campaign or old.active(): raise RuntimeError('C2 migration requires flat C1')
            state=dict(old.state); state.update(version=3,campaigns={},campaign=None)
            store.save(state,'PORTFOLIO_MIGRATION',from_version=2,to_version=3)
        if state.get('version')!=3: raise RuntimeError('unsupported portfolio ledger')
        if any(o['coin'] not in state['campaigns'] for o in state['orders'].values() if o['status'] not in TERMINAL):
            raise RuntimeError('orphan portfolio order')
        state.setdefault('residuals',{})
        self.state=state; self.books={}
        self.roll_day()
    @property
    def campaigns(self): return self.state['campaigns']
    @property
    def campaign(self): return next(iter(self.campaigns.values()),None)  # legacy status compatibility only
    @property
    def equity(self):
        return max(D(0),D(self.state['cash_krw'])+sum((D(c['qty'])*D(c['mark']) for c in self.campaigns.values()),D(0)))
    def book(self,coin):
        if coin not in self.books: self.books[coin]=Book(self,coin)
        return self.books[coin]
    def active(self,role=None):
        return [o for o in self.state['orders'].values() if o['status'] not in TERMINAL and (role is None or o['role']==role)]
    def reserved_cash(self):
        return sum((max(D(0),D(o['qty'])-D(o['filled']))*D(o['price']) for o in self.active('entry')),D(0))
    def committed_risk(self):
        return sum((D(c['plan']['qty'])*(D(c['plan']['entry'])-D(c['stop_limit'])) for c in self.campaigns.values()),D(0))
    def daily_remaining(self):
        unrealized=sum((min(D(0),D(c['qty'])*D(c['mark'])-D(c['cost'])) for c in self.campaigns.values()),D(0))
        return max(D(0),self.equity*D(self.config['daily_loss_fraction'])+D(self.state['day_realized'])+unrealized)
    def remaining_risk(self):
        return max(D(0),min(self.daily_remaining(),self.equity*D(self.config['risk_fraction']))-self.committed_risk())
    def roll_day(self):
        today=dt.datetime.fromtimestamp(self.clock(),dt.timezone.utc).date().isoformat()
        if self.state['day']!=today:
            self.state.update(day=today,day_start=str(self.equity),day_realized='0',research_loss_day='0')
            if self.state['halt']=='DAILY_LOSS': self.state['halt']=None
            self.store.save(self.state,'DAY',day=today)
    def halt(self,reason):
        if self.state['halt']!=reason:
            self.state['halt']=reason; self.store.save(self.state,'HALT',reason=reason)
    def sync_cash(self,total):
        # Exact capital changes can be attributed safely after fills are settled.
        if self.campaigns or self.active(): return False
        actual=D(str(total)); initial=not self.state['capital_initialized']
        delta=D(0) if initial else actual-D(self.state['cash_krw'])
        if initial: self.state.update(initial_equity=str(actual),day_start=str(actual))
        self.state.update(cash_krw=str(actual),capital_initialized=True,capital_at=self.clock(),external_flows=str(D(self.state['external_flows'])+delta))
        self.store.save(self.state,'CAPITAL_INITIALIZED' if initial else 'EXTERNAL_CAPITAL' if delta else 'CAPITAL_SYNC',balance=str(actual),external_delta=str(delta))
        return True
    def enter(self,coin,plan,feature,minimum):
        self.roll_day()
        if coin in self.campaigns: return False
        required=D(plan['qty'])*D(plan['entry'])
        risk=D(plan['qty'])*(D(plan['entry'])-D(plan['stop_limit']))
        if required>max(D(0),D(self.state['cash_krw'])*D(self.config['cash_fraction'])-self.reserved_cash()) or risk>self.remaining_risk(): return False
        if plan.get('research') and D(self.state['research_loss_day'])+self.committed_risk()+risk>self.equity*D(self.config['risk_fraction']): return False
        return self.book(coin).enter(coin,plan,feature,minimum)
    def request_exit(self,reason):
        for coin in list(self.campaigns): self.book(coin).request_exit(reason)
