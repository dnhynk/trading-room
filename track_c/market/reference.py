"""Past-only, quiet-sample basis with explicit cross-venue uncertainty."""
from collections import deque
import math
from statistics import median


class Reference:
    def __init__(self, cfg):
        self.cfg = cfg
        self.quotes = {}
        self.basis = {v: deque() for v in ('U', 'B')}
        self.history = deque()
        self.last_second = None
        self.dislocated_since = None
        self.broken = False
        self.quiet_since = None
        self.recalibration = {v:deque() for v in ('U','B')}
        self.previous_mid = None
        self.previous_external = None
        self.regime = 0

    def quote(self, row):
        if row[0] == 's':
            if row[4] == 'disconnected':
                self.quotes.pop(row[2], None)
                self.history.clear()
            return
        if row[0] != 'b' or row[2] not in self.basis: return
        _, recv, venue, _, exchange, bid, bq, ask, aq, *_ = row
        values = (recv, exchange, bid, bq, ask, aq)
        if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
                or not 0 < bid < ask or min(bq, aq) < 0
                or not 0 <= recv-exchange <= self.cfg['leader_max_age_ms']): return
        if venue in self.quotes and recv < self.quotes[venue]['recv']: return
        self.quotes[venue] = dict(recv=recv, exchange=exchange, mid=(bid+ask)/2, half=(ask-bid)/2)

    def evaluate(self, now, mid, tick, sell_pressure=0., frozen=False):
        bad = dict(ready=False, reason='reference_unavailable', t_ms=now)
        if not all(math.isfinite(v) and v > 0 for v in (mid, tick)):
            return bad
        if set(self.quotes) != {'U', 'B'} or any(
                not 0 <= now-q['recv'] <= self.cfg['leader_max_age_ms'] or
                not 0 <= now-q['exchange'] <= self.cfg['leader_max_age_ms'] for q in self.quotes.values()):
            self.history.clear()
            return bad
        sample = self.last_second is None or now//1000 > self.last_second
        external = sum(q['mid'] for q in self.quotes.values())/2
        parts = {}
        for v, q in self.quotes.items():
            rows = self.basis[v]
            while rows and rows[0][0] < now-self.cfg['basis_window_s']*1000: rows.popleft()
            # Current-second samples may never be their own pricing evidence.
            past = [b for at, b in rows if at//1000 < now//1000]
            if len(past) >= self.cfg['basis_min_samples']:
                b = median(past)
                mad = median(abs(x-b) for x in past)*1.4826
                px = q['mid']*math.exp(b)
                uncertainty = px*math.expm1(min(.1, 2*mad))+q['half']*math.exp(b)
                parts[v] = dict(fair=px, lower=px-uncertainty, upper=px+uncertainty,
                                basis=b, mad=mad, source_ms=q['recv'])
        out = bad
        quiet = False
        if len(parts) == 2:
            fair = sum(p['fair'] for p in parts.values())/2
            dev = (fair-mid)/tick
            disagreement = abs(parts['U']['fair']-parts['B']['fair'])/tick
            quiet = abs(dev) < self.cfg['basis_quiet_ticks'] and sell_pressure < .25 and not frozen
            displaced = abs(dev) >= self.cfg['entry_ticks']
            if displaced:
                if self.dislocated_since is None: self.dislocated_since = now
                if now-self.dislocated_since >= self.cfg['basis_break_s']*1000: self.broken = True
            else:
                self.dislocated_since = None
            # A break needs a full quiet window before admission. No forced rebasing.
            if quiet:
                if self.quiet_since is None: self.quiet_since = now
                if now-self.quiet_since >= self.cfg['basis_min_samples']*1000: self.broken = False
            else: self.quiet_since = None
            if self.history and (now < self.history[-1][0] or now-self.history[-1][0] > 3000):
                self.history.clear()
            changes = {}
            for seconds in (10, 30):
                prior = next((p for t, p in reversed(self.history) if t <= now-seconds*1000), None)
                changes['m'+str(seconds)] = (fair-prior)/tick if prior is not None else None
            if sample: self.history.append((now, fair))
            while self.history and self.history[0][0] < now-31000: self.history.popleft()
            reason = ('basis_break' if self.broken else 'reference_disagreement'
                      if disagreement > self.cfg['disagreement_ticks'] else
                      'reference_warmup' if any(v is None for v in changes.values()) else None)
            out = dict(ready=reason is None, reason=reason, t_ms=now, fair=fair,
                       lower=min(p['lower'] for p in parts.values()), upper=max(p['upper'] for p in parts.values()),
                       dev_ticks=dev, disagreement_ticks=disagreement, parts=parts, regime_id=self.regime, **changes)
        # Initial calibration requires low sell pressure; once estimated, only quiet
        # residuals can update it. The displacement that triggered an episode is excluded.
        if sample:
            # A lasting basis shift starts a new calibration regime only after
            # independently quiet local/external moves and no active shock.
            # It never grants entry on the reset sample or learns an active shock.
            stable = (not frozen and sell_pressure < .25 and self.previous_mid is not None
                      and abs(mid-self.previous_mid)/tick <= self.cfg['basis_quiet_ticks']
                      and abs(external-self.previous_external)/tick <= self.cfg['basis_quiet_ticks'])
            if self.broken and stable:
                for v,q in self.quotes.items(): self.recalibration[v].append((now,math.log(mid/q['mid'])))
                if len(self.recalibration['U']) >= self.cfg['basis_min_samples']:
                    self.basis = {v:deque(rows) for v,rows in self.recalibration.items()}
                    self.recalibration = {v:deque() for v in self.basis}
                    self.broken, self.dislocated_since = False, None
                    self.history.clear()
                    self.regime += 1
                    out.update(ready=False,reason='basis_recalibrated',regime_id=self.regime)
            else:
                for rows in self.recalibration.values(): rows.clear()
            if (len(parts) < 2 and not frozen and sell_pressure < .25) or quiet:
                for v, q in self.quotes.items():
                    if not self.basis[v] or self.basis[v][-1][0] != now:
                        self.basis[v].append((now, math.log(mid/q['mid'])))
            self.last_second = now//1000
            self.previous_mid, self.previous_external = mid, external
        return out
