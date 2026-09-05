"""Bounded market replay storage and online summaries; no live exchange actions."""
from collections import Counter, defaultdict, deque
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import zlib

from .microstructure import Micro
from .oms import TERMINAL
from .simulation import Exchange

KST_MS = 9*3600000


class EventSpool:
    """Stable disk sort, preserving input order among same-time same-venue rows.

    Normalization has the same continuous Micro state across file boundaries as
    the old whole-tape replay. Only accepted events reach the execution stream.
    """
    def __init__(self, coinone, leaders, coins, stale_ms, quality):
        self.temp = tempfile.TemporaryDirectory(prefix='c3-replay-')
        self.db = sqlite3.connect(str(Path(self.temp.name)/'events.sqlite'))
        self.count = 0
        self.stale_ms = stale_ms
        try:
            self.db.execute('pragma journal_mode=OFF')  # Disposable local work file.
            self.db.execute('pragma temp_store=FILE')
            self.db.execute('pragma cache_size=-8192')
            self.db.execute('create table raw(seq integer primary key,t integer,priority integer,kind text,payload text)')
            batch=[]
            def add(t,priority,kind,payload):
                batch.append((t,priority,kind,zlib.compress(json.dumps(payload,separators=(',',':')).encode(),1)))
                if len(batch)>=1000:
                    self.db.executemany('insert into raw(t,priority,kind,payload) values(?,?,?,?)',batch)
                    batch.clear()
            for at,msg in coinone:
                data=msg.get('data') or {}
                if data.get('target_currency') in coins:
                    add(at,0,'coinone',(data['target_currency'],msg.get('channel'),data))
            for row in leaders:
                if row[0]=='b' and row[3] in coins:
                    add(row[1],1,'leader',row)
                elif row[0]=='s':
                    add(row[1],1,'connection',row)
                    quality['recorded_connection_events']+=1
            if batch:
                self.db.executemany('insert into raw(t,priority,kind,payload) values(?,?,?,?)',batch)
            self.db.commit()
            self.db.execute('create index raw_order on raw(t,priority,seq)')
            last_coinone=last_leader=None; first=None
            # First causal pass obtains overlap and quality. Reuse the same
            # compressed raw spool for execution instead of storing two copies.
            for at,priority,kind,body in self._normalized(quality):
                if kind=='coinone': last_coinone=at
                elif kind=='leader': last_leader=at
                if first is None: first=at
                self.count+=1
            if first is None: raise ValueError('no replayable events')
            if last_coinone is None or last_leader is None or min(last_coinone,last_leader)<=first:
                raise ValueError('no overlapping venue coverage')
            self.start,self.end=first,min(last_coinone,last_leader)
            self.bytes=Path(self.temp.name,'events.sqlite').stat().st_size
        except BaseException:
            self.close()
            raise

    def __iter__(self):
        return self._normalized()

    def _normalized(self,quality=None):
        warm={}
        for at,priority,kind,payload in self.db.execute('select t,priority,kind,payload from raw order by t,priority,seq'):
            body=json.loads(zlib.decompress(payload))
            if kind=='coinone':
                coin,channel,data=body
                if coin not in warm: warm[coin]=Micro(coin,stale_ms=self.stale_ms,legacy_features=False)
                event=warm[coin].feed(channel,data,at)
                if event is None: continue
                body.append(event)
            yield at,priority,kind,body
        if quality is not None:
            for micro in warm.values():
                for key,value in micro.quality.items(): quality['coinone_'+key]+=value

    def __enter__(self): return self
    def __exit__(self,*_): self.close()
    def close(self):
        self.db.close()
        target=Path(self.temp.name).resolve()
        assert target.parent==Path(tempfile.gettempdir()).resolve() and target.name.startswith('c3-replay-')
        self.temp.cleanup()


class BookWindow:
    """Book as-of lookup for current step and pending arrivals, with one left anchor."""
    def __init__(self,stale_ms):
        self.books=deque(); self.stale_ms=stale_ms
        self.max_books=0
    def append(self,event):
        if event['kind']=='book':
            self.books.append(event); self.max_books=max(self.max_books,len(self.books))
    def book_at(self,t):
        book=next((b for b in reversed(self.books) if b['t']<=t),None)
        if book and 0<=t-book['t']<=self.stale_ms and 0<=t-book.get('exchange_t',book['t'])<=self.stale_ms:
            return book
        return None
    def trim(self,t):
        while len(self.books)>1 and self.books[1]['t']<=t:
            self.books.popleft()


class ReplayExchange(Exchange):
    def prune(self,retained):
        expired={cid for cid,o in self.orders.items() if o['status'] in TERMINAL and cid not in retained}
        self.pending=[p for p in self.pending if p[2] not in expired]
        for cid in expired: del self.orders[cid]


class OutcomeStore:
    """Keep active state and compact reported outcomes, never all MARK/OMS events."""
    def __init__(self,clock):
        self.state=None; self.clock=clock
        self.attempts=0; self.turnover=0.; self.actual_buys=0.
        self.bought=defaultdict(float); self.outcomes=[]; self.counts=Counter()
    def load(self): return deepcopy(self.state)
    def save(self,state,kind,**body):
        self.state=deepcopy(state); self.event(kind,**body)
    def event(self,kind,**body):
        self.counts[kind]+=1
        if kind=='CAMPAIGN_INTENT': self.attempts+=1
        elif kind=='FILL':
            self.turnover+=float(body['gross'])
            if body['role']=='entry':
                self.bought[body.get('campaign_id')]+=float(body['gross'])
                self.actual_buys+=float(body['gross'])
        elif kind in ('CLOSE','NO_FILL'):
            c=body['campaign']; gross=self.bought.pop(c['id'],0.)
            notional=gross+float((c.get('residual') or {}).get('cost',0))
            if kind!='CLOSE' or not notional: return
            t=int(self.clock()*1000)
            self.outcomes.append(dict(t=t,id=c['id'],day=(t+KST_MS)//86400000,coin=c['coin'],
                net_krw=float(c['net']),net_bp=float(c['net'])/notional*1e4,
                inventory_flat=not bool(float(c['qty'])),capital_involved_krw=notional,first_fill=c['first_fill'],
                reason=c['exit_reason'],bought=float(c['bought']),sold=float(c['sold']),residual=deepcopy(body.get('residual')),
                dev=c['plan'].get('dev_ticks'),m30=c['plan'].get('m30'),m10=c['plan'].get('m10'),flow32=c['plan'].get('flow32'),
                stop_mode=c['plan'].get('stop_mode'),entry_stop=c['plan'].get('stop'),
                minimum_size_uplift_krw=c['plan'].get('minimum_size_uplift_krw'),
                minimum_risk_excess_krw=c['plan'].get('minimum_risk_excess_krw')))


class WealthSummary:
    def __init__(self,start,cash,step,start_ms=None,end_ms=None):
        self.previous_t=start; self.previous_w=cash; self.step=step
        self.peak=cash; self.drawdown=0.; self.daily={}; self.last=None
        self.anchors={t:None for t in range(start_ms,end_ms+1,86400000)} if start_ms is not None else {}
        if end_ms is not None: self.anchors[end_ms]=None
        self.boundaries=sorted(self.anchors); self.anchor_index=0
        self.start_ms,self.end_ms=start_ms,end_ms
    def observe(self,t,wealth):
        while self.anchor_index<len(self.boundaries) and self.boundaries[self.anchor_index]<t:
            self.anchors[self.boundaries[self.anchor_index]]=self.last[1] if self.last else None
            self.anchor_index+=1
        if self.anchor_index<len(self.boundaries) and self.boundaries[self.anchor_index]==t:
            self.anchors[t]=wealth; self.anchor_index+=1
        day=str((t+KST_MS)//86400000)
        row=self.daily.setdefault(day,dict(first_t=self.previous_t,last_t=t,net_krw=0.,opening_equity_krw=self.previous_w))
        row['last_t']=t; row['net_krw']+=wealth-self.previous_w
        self.previous_t,self.previous_w=t,wealth; self.last=(t,wealth)
        self.peak=max(self.peak,wealth); self.drawdown=max(self.drawdown,self.peak-wealth)
    def finish(self):
        for key,row in self.daily.items():
            boundary=int(key)*86400000-KST_MS
            row['complete']=row['first_t']<=boundary and row['last_t']>=boundary+86400000-self.step
        blocks={}
        if self.start_ms is not None and self.last:
            for i,start in enumerate(range(self.start_ms,self.end_ms,86400000)):
                end=min(start+86400000,self.end_ms)
                left,right=self.anchors[start],self.anchors[end]
                complete=left is not None and right is not None and self.last[0]>=end
                blocks[str(i)]=dict(start_ms=start,end_ms=end,complete=complete,net_krw=right-left if complete else None)
        return self.daily,blocks
