"""Small regularized competing-risk/conditional cash models with support gates.

Uncertainty is a time-cluster normal approximation, not an anytime-valid bound.
Rows from different actions in one episode never increase an action's sample size.
"""
from collections import Counter, defaultdict
import math
from statistics import NormalDist, fmean

from track_c.learning.config import digest


def cluster_interval(rows, field, cfg, comparisons=1):
    values = [float(r[field]) for r in rows]
    if not values or not all(math.isfinite(v) for v in values):
        return dict(mean=None, lower=None, upper=None, blocks=0, n=len(values))
    mean = fmean(values)
    clusters = defaultdict(float)
    for r, v in zip(rows, values): clusters[int(r['start_ms'])//cfg['block_ms']] += v-mean
    k = len(clusters)
    if k < cfg['min_blocks']:
        return dict(mean=mean, lower=None, upper=None, blocks=k, n=len(rows))
    se = math.sqrt(k/(k-1)*sum(x*x for x in clusters.values()))/len(values)
    z = NormalDist().inv_cdf(1-cfg['alpha']/max(2, comparisons+1))
    # Deliberate finite-block inflation, still an approximation under dependence.
    width = z*math.sqrt(k/max(1, k-2))*se
    return dict(mean=mean, lower=mean-width, upper=mean+width, blocks=k, n=len(rows), se=se)


def usable(rows, until):
    found = set()
    for r in rows:
        if r['start_ms'] >= until or r['end_ms'] >= until or r['censored']: continue
        identity = (r['episode_id'], r['action']['id'])
        if identity in found: raise ValueError('duplicate episode/action label')
        found.add(identity)
        yield r


class CashModel:
    def __init__(self, document, cfg):
        self.doc, self.cfg = document, cfg
        if document.get('digest') != digest({k:v for k,v in document.items() if k != 'digest'}):
            raise ValueError('C4 model digest mismatch')
        if document.get('config') != digest(cfg): raise ValueError('C4 model configuration mismatch')
        self.groups = document['groups']

    @classmethod
    def fit(cls, rows, until, cfg, exit_digest):
        groups = defaultdict(list)
        for r in usable(rows, until):
            if r['exit_model'] != exit_digest: raise ValueError('entry labels used another exit policy')
            if r['start_ms'] <= r.get('exit_trained_until', -1): raise ValueError('exit training leaked into entry labels')
            a = r['action']
            groups[a['key']+'|'+a['id']].append(r)
        doc = dict(kind='c4-cash-v1', trained_until=until, exit_model=exit_digest,
                   config=digest(cfg), groups=dict(groups))
        doc['digest'] = digest(doc)
        return cls(doc, cfg)

    def predict(self, action, now, comparisons=6):
        bad = dict(ready=False, reason='model_support', score_krw=None)
        if now <= self.doc['trained_until']: return dict(bad, reason='future_model')
        rows = self.groups.get(action['key']+'|'+action['id'], [])
        filled = [r for r in rows if r['filled_qty'] > 0]
        bounds = cluster_interval(rows, 'net_bp', self.cfg, comparisons)
        result = dict(bad, **bounds, attempts=len(rows), fills=len(filled),
                      p_fill=(len(filled)+.5)/(len(rows)+1),
                      fill_conditioned_bp=fmean(r['net_bp']/r['fill_fraction'] for r in filled) if filled else None,
                      mean_fill_fraction=fmean(r['fill_fraction'] for r in rows) if rows else None)
        if len(rows) < self.cfg['min_attempts'] or len(filled) < self.cfg['min_fills'] or bounds['lower'] is None:
            return result
        # No extrapolation to new sizes, discounts, shock intensities or spread regimes.
        for field in ('notional', 'dev_ticks', 'pressure', 'spread_ticks'):
            values = [r['action'][field] for r in rows]
            if not min(values)-1e-8 <= action[field] <= max(values)+1e-8:
                return dict(result, reason='outside_support', outside=field)
        ordered = sorted(r['net_bp'] for r in filled)
        tail = max(0., -fmean(ordered[:max(1, math.ceil(.1*len(ordered)))]))
        occupied = fmean(r['occupied_s'] for r in rows)
        capital_cost = self.cfg['capital_cost_bp_hour']*occupied/3600
        lower = bounds['lower']-self.cfg['risk_aversion']*tail-capital_cost
        return dict(result, ready=True, reason='positive_value' if lower > 0 else 'nonpositive_value',
                    score_krw=lower*action['notional']/10000, conservative_bp=lower,
                    tail_loss_bp=tail, capital_cost_bp=capital_cost)


class HazardModel:
    def __init__(self, document, cfg):
        self.doc, self.cfg = document, cfg
        if document.get('digest') != digest({k:v for k,v in document.items() if k != 'digest'}):
            raise ValueError('C4 hazard digest mismatch')
        if document.get('config') != digest(cfg): raise ValueError('C4 hazard configuration mismatch')

    @classmethod
    def fit(cls, rows, until, cfg):
        groups = defaultdict(list)
        for r in usable(rows, until):
            if r['exit_model'] != 'structural': raise ValueError('hazard labels must use structural exits')
            if r['filled_qty'] > 0: groups[r['action']['key']+'|'+r['action']['id']].append(r)
        doc = dict(kind='c4-competing-risks-v1', trained_until=until, config=digest(cfg), groups=dict(groups))
        doc['digest'] = digest(doc)
        return cls(doc, cfg)

    def survival(self, action):
        rows = self.doc['groups'].get(action['key']+'|'+action['id'], [])
        survival = 1.
        incidence = dict(recovery=0., collapse=0., timeout=0.)
        output = []
        left = 0
        for right in self.cfg['hazard_seconds']:
            at_risk = [r for r in rows if r['duration_s'] >= left]
            causes = Counter(r['cause'] for r in at_risk if r['duration_s'] < right or right == self.cfg['hold_s'])
            n, prior = len(at_risk), self.cfg['hazard_prior']
            probs = {c:(causes[c]+prior/4)/(n+prior) for c in incidence}
            for cause, p in probs.items(): incidence[cause] += survival*p
            survival *= 1-sum(probs.values())
            output.append(dict(seconds=right, at_risk=n, hazard=probs, survival=survival, incidence=dict(incidence)))
            left = right
        return output

    def continuation(self, action, age, now, vwap):
        if now <= self.doc['trained_until']: return dict(ready=False, reason='future_model')
        # Landmarked on actual fill age: paths already terminated are not survivors.
        rows = [r for r in self.doc['groups'].get(action['key']+'|'+action['id'], []) if r['duration_s'] > age]
        bounds = cluster_interval(rows, 'terminal_bp', self.cfg, comparisons=6)
        if len(rows) < self.cfg['min_fills'] or bounds['lower'] is None:
            return dict(ready=False, reason='continuation_support', survivors=len(rows))
        current_bp = (vwap*(1-self.cfg['fee_bp']/10000)/action['price']-1)*10000
        remaining = fmean(r['duration_s']-age for r in rows)
        charge = self.cfg['capital_cost_bp_hour']*remaining/3600
        delta = bounds['lower']-current_bp-charge
        return dict(ready=True, hold=delta > 0, incremental_lower_bp=delta, survivors=len(rows), **bounds)
