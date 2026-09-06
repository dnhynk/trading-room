"""One causal maker-to-taker counterfactual used for labels and shadow decisions.

No exchange API exists here. Queue cancellations never improve our queue position.
Each candidate is a separate counterfactual, not additive simulated capital.
"""
from copy import deepcopy
import math

from track_c.market.microstructure import liquidate


class Attempt:
    def __init__(self, action, cfg, initial, exit_model=None):
        self.a, self.cfg = deepcopy(action), cfg
        self.exit_model = exit_model
        self.start = action['t_ms']
        self.arrival = self.start+cfg['latency_ms']
        self.deadline = self.start+action['ttl_s']*1000
        self.cancel_at = None
        self.pending_exit = None
        self.requested = None
        self.active = False
        self.entry_done = False
        self.ahead = 0.
        self.bought = self.sold = self.gross = self.fees = 0.
        self.first_fill = None
        self.fair_at_fill = 0.
        self.external_pnl = self.relative_pnl = 0.
        self.attribution_complete = True
        self.done = False
        self.censored = False
        self.reason = None
        self.end = None
        self.book = None
        self.last_ref = initial['reference']
        self.last_t = self.start
        self.update_book(dict(kind='book', t=initial['book_ms'], bids=initial['bids'], asks=initial['asks']))
        self.fill_events = 0

    @property
    def qty(self): return max(0., self.bought-self.sold)

    def update_book(self, event):
        self.book = dict(t=event['t'], bids=[list(r) for r in event['bids']], asks=[list(r) for r in event['asks']])

    def fresh(self, now):
        return self.book is not None and 0 <= now-self.book['t'] <= self.cfg['book_max_age_ms']

    def cancel(self, now):
        if not self.entry_done and self.cancel_at is None: self.cancel_at = now+self.cfg['cancel_latency_ms']

    def finish(self, now, reason, censored=False):
        self.done, self.end, self.reason = True, now, reason
        self.censored |= censored

    def advance(self, now):
        if now < self.last_t: raise ValueError('noncausal attempt clock')
        self.last_t = now
        if self.done: return
        if not self.active and not self.entry_done and now >= self.arrival:
            if not self.fresh(self.arrival) or self.a['price'] >= self.book['asks'][0][0]:
                self.entry_done = True
                self.finish(self.arrival, 'arrival_reject', censored=not self.fresh(self.arrival))
                return
            self.active = True
            self.ahead = sum(q for p,q in self.book['bids'] if p == self.a['price'])
        if not self.entry_done and now >= self.deadline: self.cancel(self.deadline)
        if self.cancel_at is not None and now >= self.cancel_at:
            self.entry_done, self.active = True, False
        if self.pending_exit and now >= self.pending_exit['at']:
            order = self.pending_exit
            self.pending_exit = None
            self.sell(order['at'], order['limit'])
            if self.qty <= self.a['qty_step']*.1: self.finish(order['at'], order['reason'])
        if self.entry_done and not self.bought: self.finish(now, 'no_fill')
        elif self.entry_done and self.fresh(now) and self.qty*self.book['bids'][0][0] < self.a['minimum']:
            self.finish(now, 'residual')

    def event(self, event):
        self.advance(event['t'])
        if self.done: return
        if event['kind'] == 'book': self.update_book(event)
        elif self.active and not event['buy'] and event['price'] <= self.a['price']:
            if event['price'] < self.a['price']: self.ahead = 0.
            ahead = min(self.ahead, event['qty'])
            self.ahead -= ahead
            qty = min(self.a['qty']-self.bought, event['qty']-ahead)
            # Exchange quantity grids apply to our fills too; aggregate public trade
            # volume can leave fractions below the legal unit, which are not invented.
            qty = math.floor((qty+1e-14)/self.a['qty_step'])*self.a['qty_step']
            if qty > 0:
                fair = self.last_ref.get('fair')
                if fair is None or event['t']-self.last_ref.get('t_ms',0) > self.cfg['leader_max_age_ms']:
                    self.attribution_complete = False
                    fair = self.a['reference']
                self.fair_at_fill += qty*fair
                self.bought += qty
                self.fees += qty*self.a['price']*self.cfg['fee_bp']/10000
                self.fill_events += 1
                if self.first_fill is None: self.first_fill = event['t']
                if self.bought >= self.a['qty']-self.a['qty_step']*.1:
                    self.entry_done, self.active = True, False

    def sell(self, now, limit):
        if not self.fresh(now):
            self.censored = True
            return
        for level in self.book['bids']:
            price, displayed = level
            if price < limit: break
            available = displayed*self.cfg['depth_haircut']
            q = min(self.qty, available)
            q = math.floor((q+1e-14)/self.a['qty_step'])*self.a['qty_step']
            if q <= 0: continue
            # Consume our own simulated sale until the next actual book update.
            level[1] = max(0., displayed-q/self.cfg['depth_haircut'])
            self.sold += q
            self.gross += price*q
            self.fees += price*q*self.cfg['fee_bp']/10000
            fair_in = self.fair_at_fill/self.bought
            fair_out = self.last_ref.get('fair', fair_in)
            if now-self.last_ref.get('t_ms',0) > self.cfg['leader_max_age_ms']: self.attribution_complete = False
            self.external_pnl += q*(fair_out-fair_in)
            self.relative_pnl += q*((price-fair_out)-(self.a['price']-fair_in))
            if self.qty <= self.a['qty_step']*.1: break

    def decide(self, now, s):
        self.advance(now)
        if self.done: return
        if s: self.last_ref = s['reference']
        ref = (s or {}).get('reference', {})
        fresh = bool(s and self.fresh(now))
        if not self.entry_done and (not fresh or not ref.get('ready') or ref.get('lower',0) <= self.a['price']):
            self.cancel(now)
        if self.qty and fresh and self.qty*min(s['bid'],self.a['stop_limit']) >= self.a['minimum']*1.05:
            self.cancel(now)
        if self.first_fill is None: return
        age = (now-self.first_fill)/1000
        # Recovery is a price-contingent opportunity, not a latched emergency.
        # Revalidate it after a cancel race; risk exits can always supersede it.
        reason = self.requested if self.requested != 'recovery' else None
        if not fresh or not ref.get('ready'): reason = reason or 'data_loss'
        elif s['bid'] <= self.a['stop']: reason = reason or 'stop'
        elif ref.get('m10',0) <= -self.cfg['common_drop_ticks'] or ref['upper'] < self.a['price']:
            reason = reason or 'collapse'
        if age >= self.a['hold_s']: reason = reason or 'timeout'
        bids = [(p,q*self.cfg['depth_haircut']) for p,q in self.book['bids']] if fresh else []
        vwap = liquidate(bids, self.qty) if self.qty else None
        limit = 0.
        if not reason and vwap is not None:
            if vwap >= ref['lower']-self.a['tick']:
                reason, limit = 'recovery', max(0., ref['lower']-self.a['tick'])
            elif self.exit_model:
                value = self.exit_model.continuation(self.a, age, now, vwap)
                if value.get('ready') and not value['hold']: reason = 'value'
        if reason:
            # Cancel and settle entry before selling. Fills during the cancel race
            # remain owned inventory and are included in the pending sale.
            self.requested = reason
            self.cancel(now)
            if self.entry_done and self.qty and self.pending_exit is None:
                self.pending_exit = dict(at=now+self.cfg['latency_ms'], limit=limit, reason=reason)
        elif self.requested == 'recovery':
            self.requested = None

    def result(self, now=None):
        now = self.end if self.done else now
        if now is None: raise ValueError('unfinished path needs an explicit censor time')
        fresh = self.fresh(now)
        mark = self.qty*self.book['bids'][0][0] if fresh else 0.
        cost = self.bought*self.a['price']
        net = self.gross+mark-cost-self.fees
        # No sale is fabricated for subminimum inventory. Cash-recovery stress
        # prices every remaining unit at zero and feeds the conservative selector.
        cash_net = self.gross-cost-self.fees
        duration = (now-self.first_fill)/1000 if self.first_fill is not None else 0.
        reason = self.reason or 'boundary'
        cause = 'recovery' if reason == 'recovery' else 'timeout' if reason == 'timeout' else 'collapse'
        return dict(action=self.a, episode_id=self.a['episode_id'], start_ms=self.start, end_ms=now,
                    censored=self.censored or not self.done, reason=reason, cause=cause,
                    first_fill_ms=self.first_fill, filled_qty=self.bought, sold_qty=self.sold, residual_qty=self.qty,
                    residual_value_krw=mark, residual_cost_krw=self.qty*self.a['price'],
                    net_krw=net, cash_net_krw=cash_net, gross_exit_krw=self.gross, fees_krw=self.fees,
                    net_bp=cash_net/self.a['notional']*10000, fill_fraction=self.bought/self.a['qty'],
                    # Entry fees are sunk at the continuation comparison. Charge
                    # them in net_bp, but do not charge them again for waiting.
                    terminal_bp=((self.gross-self.fees+cost*self.cfg['fee_bp']/10000)/cost-1)*10000 if cost else 0.,
                    duration_s=duration, occupied_s=(now-self.start)/1000, fill_events=self.fill_events,
                    external_pnl_krw=self.external_pnl, relative_pnl_krw=self.relative_pnl,
                    attribution_complete=self.attribution_complete,
                    exit_model=self.exit_model.doc['digest'] if self.exit_model else 'structural',
                    exit_trained_until=self.exit_model.doc['trained_until'] if self.exit_model else -1)
