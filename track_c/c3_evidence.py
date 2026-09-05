"""Fixed-window C3 assessment from paired marked-wealth replays; never changes live size.

Day-block intervals are approximate under dependence/nonstationarity. A positive
result is evidence for review, not proof or an automatic deployment instruction.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import statistics

KST = dt.timezone(dt.timedelta(hours=9))


def sign_tail(positive, n):
    """Exact one-sided sign-test tail under independent equiprobable signs."""
    return sum(math.comb(n, k) for k in range(positive, n+1)) / 2**n if n else None


def block_lower(values, alpha, *, seed=20260905, draws=20000):
    """Fixed two-day circular blocks; do not select the block size on PnL."""
    import numpy as np
    values = np.asarray(values, dtype=float)
    if len(values) < 2: return None
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(values), size=(draws, (len(values)+1)//2))
    indices = np.stack((starts, (starts+1) % len(values)), axis=-1).reshape(draws, -1)[:, :len(values)]
    return float(np.quantile(values[indices].mean(axis=1), alpha))


def assess(protocol, base, flip, unconditional):
    issues = []
    reports = (base, flip, unconditional)
    if [r.get('control') for r in reports] != ['none', 'flip', 'unconditional']:
        issues.append('controls_missing_or_mislabeled')
    if any(r.get('schema') != 2 for r in reports):
        return dict(verdict='HOLD', issues=['replay_schema_not_wealth_aware'])
    for r in reports:
        if r['identity']['source'] != protocol['source']:
            issues.append('source_changed_after_registration')
        if r['rule'] != protocol['rule']:
            issues.append('rule_version_changed')
        expected = dict(protocol['config'])
        if r['control'] == 'unconditional':
            expected.update(entry_ticks=-1e9, cancel_ticks=-1e9, defend_ticks=-1e9)
        if r['identity']['config'] != expected:
            issues.append('settings_changed_after_registration')
        if r['scenario'] != protocol['scenario']:
            issues.append('execution_scenario_changed')
        if r['identity']['data'] != base['identity']['data'] or (r['start'], r['end']) != (base['start'], base['end']):
            issues.append('controls_not_time_matched')
        if r.get('halt') or r.get('open_campaigns') or abs(r.get('accounting_error_krw', 1)) > 1e-6:
            issues.append('unresolved_execution_or_accounting')
        if r.get('quality', {}).get('malformed_coinone_rows'):
            issues.append('malformed_tape')
    start = dt.datetime.fromisoformat(protocol['start_kst']).replace(tzinfo=KST)
    first = int(start.timestamp()*1000 + 9*3600000) // 86400000
    days = [str(first+i) for i in range(protocol['days'])]
    complete = [d for d in days if all(r['daily_wealth'].get(d, {}).get('complete') for r in reports)]
    if len(complete) != len(days):
        issues.append('fixed_prospective_window_incomplete')
    if issues:
        return dict(verdict='HOLD', issues=sorted(set(issues)), complete_days=len(complete), required_days=len(days))
    values = [[r['daily_wealth'][d]['net_krw'] for d in days] for r in reports]
    comparisons = dict(net=values[0], versus_flip=[a-b for a,b in zip(values[0],values[1])],
                       versus_unconditional=[a-b for a,b in zip(values[0],values[2])])
    # All three claims must pass; Bonferroni correct the nominal tail probability.
    alpha = protocol['family_alpha'] / len(comparisons)
    evidence = {}
    for name, series in comparisons.items():
        nonzero = [x for x in series if x != 0]
        evidence[name] = dict(mean_daily_krw=statistics.mean(series), lower_daily_krw=block_lower(series, alpha),
                              positive_days=sum(x>0 for x in series), total_days=len(series),
                              sign_p=sign_tail(sum(x>0 for x in nonzero), len(nonzero)))
    passed = all(r['mean_daily_krw']>0 and r['lower_daily_krw']>0 for r in evidence.values())
    return dict(verdict='REVIEW_ONLY' if passed else 'HOLD', evidence=evidence, complete_days=len(days),
                automatic_size_change=False, nominal_family_alpha=protocol['family_alpha'],
                limitations=['two-day block approximation on a short dependent sample',
                             'sign test concerns median and assumes independent signs, not expected profit',
                             'queue and latency counterfactual must also agree with actual fills',
                             'endpoint is fixed; extending or changing it after inspection is a new experiment'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--protocol', required=True)
    p.add_argument('--base', required=True)
    p.add_argument('--flip', required=True)
    p.add_argument('--unconditional', required=True)
    p.add_argument('--output')
    args = p.parse_args()
    paths = [args.protocol, args.base, args.flip, args.unconditional]
    result = assess(*(json.loads(Path(path).read_text()) for path in paths))
    result['artifacts'] = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
    if args.output: Path(args.output).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
