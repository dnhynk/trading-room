"""Receive-time episode states, with no forward path in the signal API."""
from collections import deque, Counter
from copy import deepcopy
from decimal import Decimal as D, ROUND_CEILING
import math

from track_c.market.microstructure import Micro, liquidate
from track_c.market.prices import price_down, price_up
from track_c.execution.sizing import floor, price_floor, price_unit
from track_c.market.reference import Reference
from track_c.market.risk import LocalRisk


def state_key(s):
    # Small predeclared partitions; no tree/threshold search on outcome labels.
    dev=s['reference'].get('dev_ticks',0.)
    return ':'.join((s['coin'], 'deep' if dev >= 4 else 'near' if dev >= 2 else 'rich',
                     'sell' if s['flow'] < -.5 else 'mixed',
                     'local' if s.get('entry_eligible') else 'ineligible'))


class Market:
    def __init__(self, coin, cfg):
        self.coin, self.cfg = coin, cfg
        self.micro = Micro(coin, stale_ms=cfg['book_max_age_ms'], legacy_features=False)
        self.reference = Reference(cfg)
        self.risk = LocalRisk(cfg)
        self.sells = deque()
        self.episode = None
        self.latest = None
        self.last_sample = None
        self.sequence = 0

    def feed(self, channel, data, recv):
        prior = (self.micro.bids[0] if self.micro.bids and
                 0 <= recv-self.micro.book_ms <= self.cfg['book_max_age_ms'] else None)
        event = self.micro.feed(channel, data, recv)
        if event: self.risk.observe(event)
        if event and event['kind'] == 'trade' and not event['buy'] and prior:
            self.sells.append((recv, event['qty']/max(prior[1], 1e-15)))
            if self.episode: self.episode['last_sell_ms'] = recv
        return event

    def snapshot(self, now, contract, units):
        if (not self.micro.bids or not 0 <= now-self.micro.book_ms <= self.cfg['sampling_book_age_ms']
                or not contract or not units):
            self.latest = None
            self.reference.history.clear()
            return None
        bid, ask = self.micro.bids[0][0], self.micro.asks[0][0]
        tick = float(price_unit(units, D(str(bid))))
        if self.episode and (now-self.episode['last_sell_ms'] > self.cfg['episode_quiet_s']*1000 or
                             now-self.episode['start_ms'] > self.cfg['episode_max_s']*1000):
            self.episode = None
        while self.sells and self.sells[0][0] < now-2000: self.sells.popleft()
        pressure = sum(v for _, v in self.sells)
        ref = self.reference.evaluate(now, (bid+ask)/2, tick, pressure, bool(self.episode))
        rows = [r for r in self.micro.trades if now-32000 <= r[0] <= now]
        buy = sum(q for _, q, _, b in rows if b)
        sell = sum(q for _, q, _, b in rows if not b)
        risk = self.risk.evaluate(now,tick)
        s = dict(coin=self.coin, t_ms=now, bid=bid, ask=ask, tick=tick,
                 bids=list(self.micro.bids), asks=list(self.micro.asks), book_ms=self.micro.book_ms,
                 reference=ref, pressure=pressure, flow=(buy-sell)/max(buy+sell, 1e-15),
                 buy_volume=buy, sell_volume=sell, risk=risk, contract=contract, units=units,
                 episode_id=None, new_episode=False, entry_fresh=self.micro.fresh(now))
        common_fall = (ref.get('m10') is not None and ref['m10'] <= -self.cfg['common_drop_ticks'])
        s['entry_eligible'] = bool(ref['ready'] and ref['dev_ticks'] >= self.cfg['entry_ticks'] and not common_fall)
        # Log ALL local sell shocks, including rejected reference/discount states.
        # Admission is a separate decision and cannot censor its own training data.
        if not self.episode and pressure >= self.cfg['sell_depth_ratio']:
            self.sequence += 1
            self.episode = dict(id=f'{self.coin}-{now}-{self.sequence}', start_ms=now, last_sell_ms=now)
            s['new_episode'] = True
        if self.episode: s['episode_id'] = self.episode['id']
        self.latest = s
        return s

    def export(self):
        def packed(obj):
            result={}
            for k,v in obj.__dict__.items():
                if k in ('cfg','b_features'):continue
                if isinstance(v,(deque,set)):v=list(v)
                elif isinstance(v,dict):v={x:list(y) if isinstance(y,deque) else y for x,y in v.items()}
                result[k]=deepcopy(v)
            return result
        return dict(coin=self.coin,micro=packed(self.micro),reference=packed(self.reference),risk=packed(self.risk),
                    sells=list(self.sells),episode=deepcopy(self.episode),sequence=self.sequence)

    @classmethod
    def restore(cls, state, cfg):
        m=cls(state['coin'],cfg)
        for name in ('micro','reference','risk'):
            obj=getattr(m,name)
            for k,value in state[name].items():
                original=getattr(obj,k)
                if isinstance(original,deque):value=deque(value)
                elif isinstance(original,set):value=set(value)
                elif isinstance(original,Counter):value=Counter(value)
                elif isinstance(original,dict):
                    value={x:deque(y) if isinstance(original.get(x),deque) else y for x,y in value.items()}
                setattr(obj,k,deepcopy(value))
        m.micro.bids=[tuple(row) for row in m.micro.bids]
        m.micro.asks=[tuple(row) for row in m.micro.asks]
        m.sells,m.episode,m.sequence=deque(state['sells']),deepcopy(state['episode']),state['sequence']
        return m


def candidates(s, cfg, cash, risk_remaining, *, research=False):
    """Grid floor never rounds through depth, cash, or portfolio risk caps."""
    if (not s or not s.get('entry_fresh',True) or 'fair' not in s['reference']
            or not (s.get('risk') or {}).get('ready')): return []
    if not research and (not s['reference']['ready'] or not s.get('entry_eligible',True)): return []
    contract, units = s['contract'], s['units']
    if contract.get('trade_status',1) != 1 or contract.get('maintenance_status',0) != 0: return []
    if 'order_types' in contract and not {'limit','market','stop_limit'} <= set(contract['order_types']): return []
    if (s['ask']-s['bid'])/s['tick'] > 2+1e-8: return []
    step, minimum = D(contract['qty_unit']), D(contract['min_order_amount'])
    base = D(str(cfg['notional_krw']))
    haircut_bids = [(p, q*cfg['depth_haircut']) for p, q in s['bids']]
    depth = sum(q for _, q in haircut_bids[:5])*cfg['depth_fraction']
    # Both entry flow and subsequent buyer capacity constrain the quantity.
    flow = min(s['sell_volume'], s['buy_volume'])*cfg['flow_fraction']
    found = []
    for offset in cfg['price_offsets']:
        px = price_down(units, s['bid'], -offset) if offset < 0 else price_up(units, s['bid'], offset)
        if px <= 0 or px >= D(str(s['ask'])): continue
        if not research and s['reference']['lower'] <= float(px): continue
        distance = max(D(str(s['risk']['distance_price'])), D(str(s['tick'])), D(str(s['ask']-s['bid'])))
        stop = price_floor(units, px-distance)
        stop_limit = price_down(units, stop, 1)
        if not 0 < stop_limit < stop < px: continue
        legal = (max(D(contract.get('min_qty','0')), minimum*D('1.05')/stop_limit)/step).to_integral_value(rounding=ROUND_CEILING)*step
        cap = floor(min(base/px, D(str(cash*cfg['cash_fraction']))/px,
                        D(str(risk_remaining))/(px-stop_limit), D(str(depth)), D(str(flow)),
                        D(contract['max_qty']), D(contract.get('max_order_amount','Infinity'))/px), step)
        seen = set()
        for size in cfg['size_modes']:
            q = legal if size == 'minimum' else cap
            if q < legal or q > cap or q in seen: continue
            seen.add(q)
            vwap = liquidate(haircut_bids, float(q))
            if vwap is None: continue
            found.append(dict(id=f'{offset}:{size}', offset=offset, size=size, price=float(px), qty=float(q),
                              stop=float(stop), stop_limit=float(stop_limit), minimum=float(minimum),
                              qty_step=float(step), tick=s['tick'], ttl_s=cfg['ttl_s'], hold_s=cfg['hold_s'],
                              key=state_key(s), notional=float(q*px), nominal_loss=float(q*(px-stop_limit)),
                              episode_id=s['episode_id'], coin=s['coin'], t_ms=s['t_ms'],
                              reference=s['reference']['fair'], dev_ticks=s['reference']['dev_ticks'],
                              spread_ticks=(s['ask']-s['bid'])/s['tick'], pressure=s['pressure'],
                              liquidation_vwap=vwap))
    return found
