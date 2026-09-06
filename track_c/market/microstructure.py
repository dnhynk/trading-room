"""Causal receive-time market state shared by research and production."""
from collections import Counter, deque
import math
import statistics
from common.signal import Features as BFeatures

SCHEMA = 'coinone-micro-v3'
WINDOWS = (2, 8, 32, 120)  # basis functions; coefficients are fitted, not entry thresholds
FEATURES = ('b_velocity','b_buy_fraction','b_sell_decay','spread_bp','tick_bp','spread_ticks','imbalance','imbalance5','microprice_bp','log_bid_queue',
            'log_depth_bid','log_depth_ask','book_age_s','trade_age_s','book_updates_s','fresh_fraction') + tuple(
                f'{name}_{window}' for window in WINDOWS for name in ('return_bp','vol_bp','flow','log_volume','trade_rate','ofi'))


def levels(rows, reverse=False):
    result=[]
    for row in rows:
        p,q=(float(row[k]) for k in ('price','qty')) if isinstance(row,dict) else map(float,row[:2])
        if not math.isfinite(p+q) or p<=0 or q<0:
            raise ValueError('invalid level')
        if q: result.append((p,q))
    if not result or len({p for p,_ in result})!=len(result):
        raise ValueError('empty or duplicate levels')
    return sorted(result,reverse=reverse)


def liquidate(bids, quantity):
    """Actual visible sell VWAP; missing depth is unavailable, never extrapolated."""
    remaining=quantity; total=0.
    for price,size in bids:
        take=min(remaining,size); total+=take*price; remaining-=take
        if remaining<=max(1e-12,quantity*1e-10): return total/quantity
    return None


class Micro:
    def __init__(self, coin, *, stale_ms=1500, legacy_features=True):
        self.coin,self.stale_ms=coin,stale_ms
        self.legacy_features=legacy_features
        self.bids,self.asks=[],[]
        self.book_ms=self.book_exchange=self.last_ms=self.trade_ms=0
        self.book_id=-1; self.trade_exchange=-1
        self.prices,self.trades,self.flows,self.book_times=deque(),deque(),deque(),deque()
        self.trade_ids,self.seen=deque(),set()
        self.quality=Counter(); self.started=None
        self.b_features=BFeatures({}) if legacy_features else None; self.b_signals=[]
        self.trade_candle=None

    def reset(self):
        coin,limit=self.coin,self.stale_ms
        self.__init__(coin,stale_ms=limit,legacy_features=self.legacy_features)

    def feed(self, channel, data, recv):
        """Returns an accepted event for queue labeling, otherwise None."""
        try:
            if data.get('quote_currency')!='KRW' or data.get('target_currency')!=self.coin:
                raise ValueError('identity')
            ts=int(data['timestamp'])
            if recv<self.last_ms or not 0<=recv-ts<=self.stale_ms:
                self.quality['late_or_stale']+=1; return None
            if channel=='ORDERBOOK':
                ident=int(data['id'])
                if ident<=self.book_id: self.quality['duplicate_book']+=1; return None
                bids,asks=levels(data['bids'],True),levels(data['asks'])
                if bids[0][0]>=asks[0][0]: raise ValueError('crossed')
                if self.bids:
                    bp,bq=bids[0]; ap,aq=asks[0]; pb,pbq=self.bids[0]; pa,paq=self.asks[0]
                    ofi=(bq if bp>=pb else 0)-(pbq if bp<=pb else 0)-(aq if ap<=pa else 0)+(paq if ap>=pa else 0)
                    self.flows.append((recv,ofi/max((bq+aq+pbq+paq)/2,1e-12)))
                self.bids,self.asks=bids,asks; self.book_id=ident
                self.book_ms,self.book_exchange=recv,ts
                mid=(bids[0][0]+asks[0][0])/2
                self.prices.append((recv,mid)); self.book_times.append(recv)
                event=dict(t=recv,exchange_t=ts,kind='book',bids=bids,asks=asks)
            elif channel=='TRADE':
                ident=str(data['id'])
                if ident in self.seen or ts<self.trade_exchange:
                    self.quality['duplicate_or_backwards_trade']+=1; return None
                price,quantity=float(data['price']),float(data['qty'])
                if type(data['is_seller_maker']) is not bool or not math.isfinite(price+quantity) or min(price,quantity)<=0:
                    raise ValueError('trade')
                buy=data['is_seller_maker']
                self.seen.add(ident); self.trade_ids.append(ident)
                if len(self.trade_ids)>50000: self.seen.remove(self.trade_ids.popleft())
                self.trades.append((recv,quantity,price,buy)); self.trade_ms=recv; self.trade_exchange=ts
                event=dict(t=recv,exchange_t=ts,kind='trade',price=price,qty=quantity,buy=buy)
            else: return None
            self.started=recv if self.started is None else self.started
            self.last_ms=recv
            if channel=='ORDERBOOK':
                message=dict(arg=dict(channel='books15'),ts=recv,data=[dict(ts=recv,bids=self.bids,asks=self.asks)])
            else:
                message=dict(arg=dict(channel='trade'),ts=recv,data=[dict(side='buy' if buy else 'sell',size=quantity,price=price)])
            if self.b_features is not None:
                self.b_signals=[s for s in self.b_signals if recv-(s['t']+1)*1000<=self.stale_ms]
                self.b_signals+=self.b_features.feed(message)
            if channel=='TRADE' and self.b_features is not None:
                # Closed trade-built minute candles make the inherited B inputs
                # observable without a REST candle fetched after the decision.
                slot=recv//60000*60000
                if self.trade_candle is None or self.trade_candle[0]!=slot:
                    self.trade_candle=[slot,price,price,price,price,quantity]
                else:
                    c=self.trade_candle; c[2]=max(c[2],price); c[3]=min(c[3],price); c[4]=price; c[5]+=quantity
                self.b_features._candle(self.trade_candle)
            for items in (self.prices,self.trades,self.flows):
                while len(items)>1 and items[1][0]<recv-121000: items.popleft()
            while self.book_times and self.book_times[0]<recv-121000: self.book_times.popleft()
            self.quality['accepted']+=1
            return event
        except (KeyError,ValueError,TypeError,OverflowError):
            self.quality['invalid']+=1; return None

    def fresh(self, now):
        return bool(self.bids and 0<=now-self.book_ms<=self.stale_ms and 0<=now-self.book_exchange<=self.stale_ms)

    def snapshot(self, now, tick):
        if not self.fresh(now) or tick<=0: return None
        bid,bq=self.bids[0]; ask,aq=self.asks[0]; mid=(bid+ask)/2
        depth_b=sum(p*q for p,q in self.bids[:5]); depth_a=sum(p*q for p,q in self.asks[:5])
        volume_b=sum(q for _,q in self.bids[:5]); volume_a=sum(q for _,q in self.asks[:5])
        recent=[t for t in self.book_times if t>=now-32000]
        covered=sum(min(self.stale_ms,max(0,b-a)) for a,b in zip(recent,recent[1:]+[now])) if recent else 0
        bf=self.b_features.f if self.b_features is not None else {}
        f=dict(b_velocity=float(bf.get('v') or 0),b_buy_fraction=float(bf.get('bs10') or 0),b_sell_decay=float(bool(bf.get('sell_decay'))),
               spread_bp=(ask-bid)/mid*10000,tick_bp=tick/mid*10000,spread_ticks=(ask-bid)/tick,
               imbalance=(bq-aq)/(bq+aq),imbalance5=(volume_b-volume_a)/(volume_b+volume_a),
               microprice_bp=((ask*bq+bid*aq)/(bq+aq)/mid-1)*10000,
               log_bid_queue=math.log1p(bid*bq),log_depth_bid=math.log1p(depth_b),log_depth_ask=math.log1p(depth_a),
               book_age_s=(now-self.book_ms)/1000,trade_age_s=min(120,(now-self.trade_ms)/1000) if self.trade_ms else 120,
               book_updates_s=len(recent)/32,fresh_fraction=min(1,covered/32000))
        for window in WINDOWS:
            beginning=now-window*1000
            prices=[(t,p) for t,p in self.prices if t>=beginning]
            older=[p for t,p in self.prices if t<beginning]
            base=older[-1] if older else prices[0][1] if prices else mid
            returns=[math.log(p1/p0)*10000 for (_,p0),(_,p1) in zip(prices,prices[1:])]
            rows=[row for row in self.trades if beginning<=row[0]<=now]
            buy=sum(q*p for _,q,p,b in rows if b); sell=sum(q*p for _,q,p,b in rows if not b)
            f.update({f'return_bp_{window}':math.log(mid/base)*10000,
                      f'vol_bp_{window}':math.sqrt(sum(r*r for r in returns)),
                      f'flow_{window}':(buy-sell)/max(buy+sell,1e-12),f'log_volume_{window}':math.log1p(buy+sell),
                      f'trade_rate_{window}':len(rows)/window,
                      f'ofi_{window}':sum(v for t,v in self.flows if beginning<=t<=now)})
        if not all(math.isfinite(v) for v in f.values()): return None
        signals=[s for s in self.b_signals if 0<=now-(s['t']+1)*1000<=self.stale_ms and not s.get('shadow') and s['sig']=='DIP_SLOWING']
        return dict(schema=SCHEMA,coin=self.coin,t=now,book_t=self.book_ms,bid=bid,ask=ask,tick=tick,
                    bids=list(self.bids),asks=list(self.asks),features=f,history_s=(now-self.started)/1000,
                    baseline_context='trade_built_closed_candles' if self.b_features is not None else 'not_used_by_rule',baseline_ready=bool(self.b_features and self.b_features.atr),
                    baselines=dict(pre_gate=bool(signals),c1_110=any(s.get('src')=='v' and s.get('bs10',0)>.5 for s in signals),
                                   b_111=any(s.get('src')=='v' and s.get('bs10',0)>.5 and s.get('sell_decay') for s in signals)))
