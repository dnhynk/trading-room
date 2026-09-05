"""Causal risk scale and historical continuation-policy diagnostics for C3.

The sparse RMS price change is a risk proxy, not a noise-corrected volatility
estimator. Path analogues estimate a stated continuation policy, not a proved
optimal stopping value. See EXIT-C3-20260906.md for assumptions and limitations.
No fitting, threshold search, I/O, or exchange action occurs in this module.
"""
from collections import deque
from bisect import bisect_right
from dataclasses import dataclass
import math
import statistics


DEFAULTS = dict(stop_mode='fixed', stop_tail_probability=.05,
                stop_vol_window_s=300, stop_vol_stride_s=10,
                value_exit=False, value_window_s=21600,
                value_min_paths=20, value_confidence=.95)


def validate_exit_settings(cfg):
    c = dict(DEFAULTS, **{k: cfg[k] for k in DEFAULTS if k in cfg})
    if c['stop_mode'] not in ('fixed', 'volatility'):
        raise ValueError('stop_mode must be fixed or volatility')
    if type(c['value_exit']) is not bool:
        raise ValueError('value_exit must be boolean')
    for key, lo, hi in (('stop_vol_window_s', 60, 3600), ('stop_vol_stride_s', 2, 60),
                        ('value_window_s', 3600, 86400), ('value_min_paths', 20, 1000)):
        if type(c[key]) is not int or not lo <= c[key] <= hi:
            raise ValueError('invalid '+key)
    if c['stop_vol_window_s'] % c['stop_vol_stride_s'] or c['stop_vol_window_s'] // c['stop_vol_stride_s'] < 20:
        raise ValueError('volatility needs at least 20 complete, equal sparse intervals')
    for key, lo, hi in (('stop_tail_probability', 0, .5), ('value_confidence', .5, 1)):
        x = c[key]
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not lo < x < hi:
            raise ValueError('invalid '+key)
    hold = cfg.get('hold_s', 180)
    if type(hold) is not int or not 1 <= hold <= 300:
        raise ValueError('invalid holding horizon')
    if c['value_window_s'] < c['value_min_paths'] * (hold + 1):
        raise ValueError('value window cannot contain required nonoverlapping paths')
    return c


def mean_uncertainty(values, confidence=.95):
    """Mean + heuristic HAC/ordinary-SE buffer; not a sequential confidence bound."""
    n = len(values)
    if n < 2 or not all(math.isfinite(x) for x in values):
        raise ValueError('finite repeated path outcomes required')
    mean = statistics.fmean(values)
    centered = [v - mean for v in values]
    gamma0 = sum(v*v for v in centered) / n
    lags = min(n-1, max(1, int(n ** (1/3))))
    long_var = gamma0
    for lag in range(1, lags+1):
        covariance = sum(centered[i]*centered[i-lag] for i in range(lag, n)) / n
        long_var += 2 * (1-lag/(lags+1)) * covariance
    # Never use negative dependence to claim greater precision than ordinary SE.
    se = math.sqrt(max(gamma0, long_var, 0.) / (n-1))
    upper = mean + statistics.NormalDist().inv_cdf(confidence) * se
    return dict(mean_ticks=mean, se_ticks=se, upper_ticks=upper, hac_lags=lags,
                uncertainty='heuristic_hac_normal_buffer_not_sequential_test')


def asof_value(times, values, at):
    index = bisect_right(times, at)-1
    return values[index] if index >= 0 else None


@dataclass(frozen=True)
class Point:
    at: int
    mid: float
    fair: float
    bid: float
    ask: float
    tick: float
    m30: float | None
    m10: float | None
    venues: tuple
    flip: bool = False

    @property
    def basis(self):
        return self.venues, self.tick, self.flip

    @property
    def regime(self):
        if self.m30 is None or not math.isfinite(self.m30):
            return None
        return (self.fair <= self.mid if self.flip else self.fair >= self.mid), self.m30 >= 0


@dataclass(frozen=True)
class CompletedPath:
    points: tuple

    @property
    def start(self):
        return self.points[0]

    @property
    def end(self):
        return self.points[-1]


class ExitModel:
    def __init__(self, cfg, *, flip=False):
        self.cfg = dict(cfg, **validate_exit_settings(cfg))
        self.flip = flip
        self.hold_s = int(cfg.get('hold_s', 180))
        self.history, self.paths, self.active = deque(), deque(), []
        self.current = None
        self.last_seen = None
        self._risk_at, self._risk = None, None

    def reset_current(self):
        """Break partial paths at gaps; completed, timestamped paths remain auditable."""
        self.history.clear()
        self.active.clear()
        self.current = None
        self._risk_at, self._risk = None, None

    def observe(self, now, *, mid, fair, bid, ask, tick, m30, m10, venues):
        numbers = (now, mid, fair, bid, ask, tick)
        if any(x is None or not math.isfinite(x) for x in numbers) or not 0 < bid < ask or tick <= 0 or fair <= 0:
            self.reset_current()
            return self.risk(now)
        now = int(now)
        if self.last_seen is not None and now < self.last_seen:
            self.paths.clear()  # Time rollback must not leave future training labels.
            self.reset_current()
        self.last_seen = now
        point = Point(now, mid, fair, bid, ask, tick, m30, m10, tuple(venues), self.flip)
        if self.current and point.basis != self.current.basis:
            self.reset_current()
        if self.history and now//1000 - self.history[-1].at//1000 > 1:
            self.reset_current()
        self.current = point
        cutoff = now - self.cfg['value_window_s'] * 1000
        while self.paths and self.paths[0].start.at < cutoff:
            self.paths.popleft()
        if not self.history or now//1000 > self.history[-1].at//1000:
            self.history.append(point)
            window = max(self.cfg['stop_vol_window_s'], self.cfg.get('momentum_window_s', 30)) * 1000
            while len(self.history) > 1 and self.history[1].at <= now-window:
                self.history.popleft()
            self._risk_at, self._risk = None, None
            if self.cfg['value_exit']:
                if not self.active:
                    if point.regime is not None:
                        self.active.append(point)
                else:
                    self.active.append(point)
                    if now - self.active[0].at >= self.hold_s * 1000:
                        self.paths.append(CompletedPath(tuple(self.active)))
                        self.active.clear()  # Endpoint is not reused as the next start.
        return self.risk(now)

    def risk(self, now):
        unavailable = dict(ready=False, reason='volatility_unavailable', n_returns=0,
                           distance_price=None, sigma_price_sqrt_s=None)
        if not self.history or self.current is None or now is None or not math.isfinite(now):
            return unavailable
        end = self.history[-1]
        if not 0 <= now-end.at <= self.cfg.get('quote_max_age_ms', 1500):
            return unavailable
        if self._risk_at == end.at:
            out = dict(self._risk)
        else:
            window = self.cfg['stop_vol_window_s'] * 1000
            stride = self.cfg['stop_vol_stride_s'] * 1000
            if self.history[0].at > end.at-window:
                return unavailable
            points, index = list(self.history), len(self.history)-1
            selected = []
            for at in range(end.at, end.at-window-1, -stride):
                while index >= 0 and points[index].at > at:
                    index -= 1
                if index < 0:
                    return unavailable
                selected.append(points[index])
            elapsed = (selected[0].at-selected[-1].at) / 1000
            if elapsed <= 0:
                return unavailable
            mid_var = sum((a.mid-b.mid)**2 for a, b in zip(selected, selected[1:])) / elapsed
            fair_var = sum((a.fair-b.fair)**2 for a, b in zip(selected, selected[1:])) / elapsed
            sigma = math.sqrt(max(mid_var, fair_var))
            out = dict(ready=True, reason=None, n_returns=len(selected)-1,
                       sample_start_ms=selected[-1].at, sample_end_ms=end.at,
                       sigma_price_sqrt_s=sigma, mid_variance_price_s=mid_var,
                       fair_variance_price_s=fair_var, horizon_s=self.hold_s,
                       tail_probability=self.cfg['stop_tail_probability'])
            self._risk_at, self._risk = end.at, dict(out)
        z = statistics.NormalDist().inv_cdf(1-self.cfg['stop_tail_probability']/2)
        out['distance_price'] = max(self.current.tick, self.current.ask-self.current.bid,
                                    z*out['sigma_price_sqrt_s']*math.sqrt(self.hold_s))
        return out

    def continuation(self, now, *, bid, ask, tick, stop, stop_limit, take, age_s, unconditional=False):
        unavailable = dict(ready=False, exit=False, reason='value_unavailable', n_paths=0,
                           mean_ticks=None, se_ticks=None, upper_ticks=None)
        if not self.cfg['value_exit'] or unconditional:
            return dict(unavailable, reason='value_disabled')
        values = (now, bid, ask, tick, stop, stop_limit, take, age_s)
        try:
            values = tuple(float(v) for v in values)
        except (TypeError, ValueError):
            return unavailable
        if not all(math.isfinite(v) for v in values):
            return unavailable
        now, bid, ask, tick, stop, stop_limit, take, age_s = values
        if not (0 < stop_limit < stop < take and 0 < bid < ask and tick > 0 and 0 <= age_s < self.hold_s):
            return unavailable
        point = self.current
        if point is None or point.regime is None or point.tick != tick or not 0 <= now-point.at <= self.cfg.get('quote_max_age_ms', 1500):
            return unavailable
        if bid != point.bid or ask != point.ask:
            return dict(unavailable, reason='value_book_changed')
        remaining = self.hold_s-age_s
        cutoff = now-self.cfg['value_window_s']*1000
        paths = [p for p in self.paths if p.start.at >= cutoff and p.end.at < now
                 and p.start.basis == point.basis and p.start.regime == point.regime]
        base = dict(unavailable, n_paths=len(paths), completed_paths=len(self.paths), remaining_s=remaining,
                    regime=list(point.regime), last_source_end_ms=max((p.end.at for p in paths), default=None))
        if len(paths) < self.cfg['value_min_paths']:
            return dict(base, reason='value_samples')
        immediate = bid-(stop-stop_limit)
        outcomes = [self._rollout(p, point, bid, ask, tick, stop, take, remaining, stop-stop_limit)-immediate for p in paths]
        stats = mean_uncertainty([v/tick for v in outcomes], self.cfg['value_confidence'])
        return dict(base, **stats, ready=True, exit=stats['upper_ticks'] <= 0, reason=None,
                    fill_assumption='optimistic_ask_touch', confidence=self.cfg['value_confidence'])

    def _rollout(self, path, current, bid, ask, tick, stop, take, remaining, slippage):
        """Optimistic passive-fill analogue; not an exchange execution simulator."""
        if ask >= take:
            return take
        history_times = [p.at-current.at for p in self.history]
        history_fairs = [p.fair for p in self.history]
        if not history_times or history_times[-1] < 0:
            history_times.append(0)
            history_fairs.append(current.fair)
        virtual_times, virtual_fairs = [0], [current.fair]
        last_bid = bid
        for point in path.points[1:]:
            elapsed = (point.at-path.start.at)/1000
            if elapsed > remaining:
                break
            dt_ms = point.at-path.start.at
            future_bid = bid + point.bid-path.start.bid
            future_ask = future_bid + point.ask-point.bid
            future_fair = current.fair + point.fair-path.start.fair
            virtual_times.append(dt_ms)
            virtual_fairs.append(future_fair)
            last_bid = max(0., future_bid)
            if future_ask >= take:  # Favor holding: no queue wait at the take quote.
                return take
            if future_bid <= stop:
                return max(0., future_bid-slippage)
            moments = []
            for window in (self.cfg.get('momentum_window_s', 30), self.cfg.get('momentum_recent_s', 10)):
                past_dt = dt_ms-window*1000
                past = (asof_value(virtual_times, virtual_fairs, past_dt) if past_dt >= 0
                        else asof_value(history_times, history_fairs, past_dt))
                moments.append((future_fair-past)/tick if past is not None else None)
            m30, m10 = moments
            falling = (m30 is not None and m10 is not None and m30 <= -float(self.cfg.get('momentum_veto_ticks', 1))
                       and m10 <= m30*float(self.cfg.get('momentum_decel_share', .3333)))
            dev = (future_fair-(future_bid+future_ask)/2)/tick
            if self.flip:
                dev = -dev
            if falling or dev < float(self.cfg.get('defend_ticks', -.5)):
                return max(0., future_bid-slippage)
        return max(0., last_bid-slippage)
