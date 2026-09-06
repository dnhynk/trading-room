"""Bounded market replay storage and online summaries; no live exchange actions."""
from collections import Counter, defaultdict, deque
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import zlib

from track_c.market.microstructure import Micro
from track_c.execution.oms import TERMINAL

KST_MS = 9*3600000


class EventSpool:
    """Stable disk sort, preserving input order among same-time same-venue rows.

    Normalization has the same continuous Micro state across file boundaries as
    the old whole-tape replay. Only accepted events reach the execution stream.
    """
    def __init__(self, coinone, leaders, coins, stale_ms, quality):
        self.temp = tempfile.TemporaryDirectory(prefix='track-c-replay-')
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
        assert target.parent==Path(tempfile.gettempdir()).resolve() and target.name.startswith('track-c-replay-')
        self.temp.cleanup()


