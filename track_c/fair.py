"""Causal cross-venue reference; cointegration and profitability remain hypotheses.

The legacy name microprice means a static queue-weighted midpoint, not the estimated
Markov micro-price or a proved conditional expectation. Ratios use past seconds.
"""
from collections import deque
import math
import statistics


class Leader:
    __slots__ = ('price', 'recv', 'ratios', 'median')

    def __init__(self):
        self.price, self.recv, self.ratios, self.median = None, 0, deque(), None


def microprice(bid, ask, bid_qty=None, ask_qty=None):
    """Static queue-weighted midpoint; plain midpoint without sizes."""
    if bid_qty is None or ask_qty is None or bid_qty < 0 or ask_qty < 0 or bid_qty + ask_qty <= 0:
        return (bid + ask) / 2
    return (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty)


class FairValue:
    def __init__(self, *, window_s=300, min_samples=60, leader_max_age_ms=30000, sample_ms=1000, flip=False, weights=None, price='microprice',
                 momentum_window_s=30, momentum_recent_s=10):
        self.window_ms, self.min_samples, self.max_age, self.sample_ms, self.flip = window_s * 1000, min_samples, leader_max_age_ms, sample_ms, flip
        if price not in ('microprice', 'mid'):
            raise ValueError('leader price must be microprice or mid')
        self.weights, self.price = dict(weights or {}), price
        if any(not math.isfinite(w) or w < 0 for w in self.weights.values()) or (self.weights and sum(self.weights.values()) <= 0):
            raise ValueError('invalid leader weights')
        self.leaders = {}
        self.last_sample = None
        self.momentum_window, self.momentum_recent = momentum_window_s, momentum_recent_s
        self.history, self.history_venues = deque(), None

    def reset_momentum(self):
        self.history.clear()
        self.history_venues = None

    def momentum(self, now, fair, tick, venues):
        """One causal sample per second; never bridge a gap or a venue-set change."""
        second = int(now // 1000)
        if venues != self.history_venues or (self.history and (now < self.history[-1][0] or second-int(self.history[-1][0]//1000) > 1)):
            self.reset_momentum()
        self.history_venues = venues
        if not self.history or second > self.history[-1][0]//1000:
            self.history.append((now, fair))
        while len(self.history)>1 and self.history[1][0] <= now-self.momentum_window*1000:
            self.history.popleft()
        result = {}
        for name,window in (('m30',self.momentum_window),('m10',self.momentum_recent)):
            # As-of lookup never reads a sample later than the requested horizon.
            past = next((value for at,value in reversed(self.history) if at <= now-window*1000), None)
            result[name] = (fair-past)/tick if past is not None else None
        return result

    def leader_quote(self, venue, recv, bid, ask, bid_qty=None, ask_qty=None):
        if not all(math.isfinite(x) for x in (recv, bid, ask)) or not (0 < bid < ask):
            return
        if any(x is not None and (not math.isfinite(x) or x < 0) for x in (bid_qty, ask_qty)):
            return
        leader = self.leaders.setdefault(venue, Leader())
        if recv < leader.recv:
            return
        leader.price = microprice(bid, ask, bid_qty, ask_qty) if self.price == 'microprice' else (bid + ask) / 2
        leader.recv = recv

    def disconnect(self, venue):
        """A quote cannot survive a known connection break."""
        if venue in self.leaders:
            self.leaders[venue].price = None
        if self.history_venues and venue in self.history_venues:
            self.reset_momentum()

    def evaluate(self, now, mid, tick):
        """Fair value at `now` from leaders quoted within max_age; None when no leader qualifies."""
        if not all(math.isfinite(x) for x in (now, mid, tick)) or not (mid > 0 and tick > 0):
            self.reset_momentum()
            return None
        sample = self.last_sample is None or now // self.sample_ms > self.last_sample // self.sample_ms
        fairs = []
        venues = []
        for venue, leader in self.leaders.items():
            if leader.price is None or not 0 <= now - leader.recv <= self.max_age:
                continue
            if sample:
                while leader.ratios and leader.ratios[0][0] < now - self.window_ms:
                    leader.ratios.popleft()
                leader.median = statistics.median(r for _, r in leader.ratios) if len(leader.ratios) >= self.min_samples else None
                leader.ratios.append((now, mid / leader.price))
            if leader.median is not None:
                weight = self.weights.get(venue, 0.0) if self.weights else 1.0
                if weight > 0:
                    fairs.append((weight, leader.price * leader.median))
                    venues.append(venue)
        if sample:
            self.last_sample = now
        total = sum(w for w, _ in fairs)
        if not fairs or total <= 0:
            self.reset_momentum()
            return None
        fair = sum(w * f for w, f in fairs) / total
        dev = (fair - mid) / tick
        return dict(fair=fair, dev_ticks=-dev if self.flip else dev, leaders=len(fairs),
                    **self.momentum(now, fair, tick, tuple(sorted(venues))))
