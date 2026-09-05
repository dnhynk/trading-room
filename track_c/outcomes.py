"""Passive-order counterfactuals with explicit queue ambiguity and censoring."""
from bisect import bisect_left, bisect_right
import math
from .microstructure import liquidate

ACTION_FEATURES=('log_quantity_krw','queue_to_quantity','log_ttl','log_horizon','participation','target_ticks')


def action_features(snapshot, quantity, ttl, horizon, target_ticks=0):
    f=dict(snapshot['features'])
    notional=quantity*snapshot['bid']; queue=snapshot['bids'][0][1]
    f.update(log_quantity_krw=math.log1p(notional),queue_to_quantity=math.log1p(queue/quantity),
             log_ttl=math.log1p(ttl),log_horizon=math.log1p(horizon),
             participation=notional/max(math.expm1(f['log_volume_32']),notional),target_ticks=float(target_ticks))
    return f


class Path:
    def __init__(self, events, *, stale_ms=1500):
        self.events=events; self.times=[r['t'] for r in events]; self.stale_ms=stale_ms
        self.books=[r for r in events if r['kind']=='book']; self.book_times=[r['t'] for r in self.books]

    def book_at(self, t):
        i=bisect_right(self.book_times,t)-1
        if i<0: return None
        book=self.books[i]
        return book if 0<=t-book['t']<=self.stale_ms and 0<=t-book.get('exchange_t',book['t'])<=self.stale_ms else None

    def label(self, snapshot, quantity, ttl, horizon, *, latency_ms=250, maker=0., taker=0., optimistic=False, target_ticks=0):
        decision=snapshot['t']; arrival=decision+latency_ms
        deadline=arrival+round(ttl*1000); book=self.book_at(arrival)
        base=dict(t=decision,coin=snapshot['coin'],quantity=quantity,ttl=ttl,horizon=horizon,entry=snapshot['bid'],
                  x=action_features(snapshot,quantity,ttl,horizon,target_ticks),end=deadline+round(horizon*1000)+latency_ms,target_ticks=target_ticks,
                  queue_scenario='optimistic' if optimistic else 'conservative')
        if not book or not self.events or base['end']>self.times[-1]: return dict(base,censored='arrival_or_right_edge')
        entry=snapshot['bid']
        if entry>=book['asks'][0][0]: return dict(base,censored=None,filled=0.,fill_fraction=0.,net_bp=0.,adverse_bp=0.,post_only_reject=True)
        ahead=sum(q for p,q in book['bids'] if p>=entry); filled=0.; first=None
        for event in self.events[bisect_right(self.times,arrival):bisect_right(self.times,deadline)]:
            if event['kind']=='book' and optimistic:
                ahead=min(ahead,sum(q for p,q in event['bids'] if p>=entry))
            elif event['kind']=='trade' and not event['buy'] and event['price']<=entry:
                consumed=min(ahead,event['qty']); ahead-=consumed
                dq=min(quantity-filled,event['qty']-consumed)
                if dq>0:
                    first=event['t'] if first is None else first; filled+=dq
                    if filled>=quantity*(1-1e-10): break
        if not filled:
            # A stale path cannot demonstrate an unfilled order: it is censored.
            if any(self.book_at(t) is None for t in range(arrival,deadline+1,1000)):
                return dict(base,censored='entry_path_gap')
            return dict(base,censored=None,filled=0.,fill_fraction=0.,net_bp=0.,adverse_bp=0.)
        # Exit timing starts at the actual first fill, not signal creation.
        exit_t=max(deadline,first+round(horizon*1000))+latency_ms
        target=entry+target_ticks*snapshot['tick']; target_hit=False
        if target_ticks:
            for check in self.books[bisect_left(self.book_times,max(deadline,first)):bisect_right(self.book_times,exit_t)]:
                px=liquidate(check['bids'],filled)
                if px is None or px<target: continue
                executable=self.book_at(check['t']+2*latency_ms)
                after=liquidate(executable['bids'],filled) if executable else None
                if after is not None and after>=target:
                    exit_t=check['t']+2*latency_ms; target_hit=True; break
        base['end']=exit_t
        exit_book=self.book_at(exit_t)
        if not exit_book: return dict(base,censored='exit_quote_gap')
        walk=[self.book_at(t) for t in range(first,exit_t+1,1000)]
        if any(row is None for row in walk): return dict(base,censored='holding_path_gap')
        prices=[liquidate(row['bids'],filled) for row in walk]+[liquidate(exit_book['bids'],filled)]
        if any(p is None for p in prices): return dict(base,censored='exit_depth')
        fraction=filled/quantity; exit_px=prices[-1]
        net=(exit_px*(1-taker)-entry*(1+maker))/entry*10000*fraction
        minimum=snapshot.get('minimum',5000)
        dust=filled*min(prices)<minimum
        # Unliquidatable partials are not invented as winning trades.
        if dust: net=-10000*fraction
        return dict(base,censored=None,filled=1.,fill_fraction=fraction,fill_t=first,exit_px=exit_px,
                    net_bp=net,adverse_bp=max(0,(entry-min(prices))/entry*10000),dust=dust,target_hit=target_hit)

    def continuation(self, snapshot, quantity, horizon, *, latency_ms=250, taker=0.):
        now=self.book_at(snapshot['t']+latency_ms); end=snapshot['t']+round(horizon*1000)+latency_ms
        later=self.book_at(end)
        if not now or not later or any(self.book_at(t) is None for t in range(snapshot['t'],end+1,1000)): return None
        current=liquidate(now['bids'],quantity); future=liquidate(later['bids'],quantity)
        if current is None or future is None: return None
        return dict(t=snapshot['t'],end=end,coin=snapshot['coin'],x=action_features(snapshot,quantity,0,horizon),
                    net_bp=(future-current)*(1-taker)/current*10000)
