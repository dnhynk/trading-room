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
    def __init__(self, *, window_s=300, min_samples=60, leader_max_age_ms=30000, sample_ms=1000, flip=False, weights=None, price='microprice'):
        self.window_ms, self.min_samples, self.max_age, self.sample_ms, self.flip = window_s * 1000, min_samples, leader_max_age_ms, sample_ms, flip
        if price not in ('microprice', 'mid'):
            raise ValueError('leader price must be microprice or mid')
        self.weights, self.price = dict(weights or {}), price
        if any(not math.isfinite(w) or w < 0 for w in self.weights.values()) or (self.weights and sum(self.weights.values()) <= 0):
            raise ValueError('invalid leader weights')
        self.leaders = {}
        self.last_sample = None

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

    def evaluate(self, now, mid, tick):
        """Fair value at `now` from leaders quoted within max_age; None when no leader qualifies."""
        if not all(math.isfinite(x) for x in (now, mid, tick)) or not (mid > 0 and tick > 0):
            return None
        sample = self.last_sample is None or now // self.sample_ms > self.last_sample // self.sample_ms
        fairs = []
        for venue, leader in self.leaders.items():
            if leader.price is None or not 0 <= now - leader.recv <= self.max_age:
                continue
            if sample:
                while leader.ratios and leader.ratios[0][0] < now - self.window_ms:
                    leader.ratios.popleft()
                leader.median = statistics.median(r for _, r in leader.ratios) if len(leader.ratios) >= self.min_samples else None
                leader.ratios.append((now, mid / leader.price))
            if leader.median is not None:
                fairs.append((self.weights.get(venue, 0.0) if self.weights else 1.0, leader.price * leader.median))
        if sample:
            self.last_sample = now
        total = sum(w for w, _ in fairs)
        if not fairs or total <= 0:
            return None
        fair = sum(w * f for w, f in fairs) / total
        dev = (fair - mid) / tick
        return dict(fair=fair, dev_ticks=-dev if self.flip else dev, leaders=len(fairs))
