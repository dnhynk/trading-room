"""Offline price-discovery diagnostics for the C3 fair value (no live effect).

Per coin, on a 1-second grid of log prices (Coinone mid, leader micro-prices):
  z_i(t) = p_c(t) - p_i(t) - m_i(t), m_i = past-only rolling median premium (300 s)
  Error-correction regressions (Engle-Granger 1987): dp(t+1) = a + alpha * z(t) + e
    alpha_c < 0 : Coinone adjusts toward leader i (half-life = ln2 / -alpha_c seconds)
    alpha_i ~ 0 : leader does not adjust (Coinone contributes ~no price discovery)
  Gonzalo-Granger (1995) common-factor weights of the pair: w = alpha_perp / sum.
  Hasbrouck (1995) information-share bounds from the two Cholesky orderings.
  Joint regression dp_c(t+1) = a + sum_i beta_i z_i(t): beta_i / sum beta gives the
  leader weights that best predict Coinone's next move (candidate `leader_weights`).

python -m track_c.fairfit --coinone <files> --leaders <files> --coins BTC ETH XRP SOL --output fairfit.json
"""
import argparse
from bisect import bisect_right
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics

from .c3_replay import coinone_rows
from .fair import microprice
from .leaders import rows as leader_rows


def series_grid(points, t0, t1, max_age=30000):
    """points: sorted (t, value). Returns last value at each grid second or nan."""
    ts = [p[0] for p in points]
    out = []
    for t in range(t0, t1, 1000):
        i = bisect_right(ts, t) - 1
        out.append(points[i][1] if i >= 0 and t - points[i][0] <= max_age else math.nan)
    return out


def ols(y, xs):
    """Least squares with intercept; returns coefficients, residuals, r2."""
    import numpy as np
    X = np.column_stack([np.ones(len(y))] + [np.asarray(x) for x in xs])
    y = np.asarray(y)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    r2 = 1 - resid.var() / y.var() if y.var() > 0 else float('nan')
    return beta, resid, r2


def pair_analysis(pc, pi, window=300, min_samples=60):
    import numpy as np
    n = len(pc)
    z = np.full(n, np.nan)
    diff = np.array(pc) - np.array(pi)
    for t in range(n):
        w = diff[max(0, t - window):t]
        w = w[~np.isnan(w)]
        if len(w) >= min_samples and not np.isnan(diff[t]):
            z[t] = diff[t] - np.median(w)
    dc = np.diff(pc); di = np.diff(pi)
    m = ~(np.isnan(z[:-1]) | np.isnan(dc) | np.isnan(di))
    if m.sum() < 200:
        return None
    bc, rc, r2c = ols(dc[m], [z[:-1][m]])
    bi, ri, r2i = ols(di[m], [z[:-1][m]])
    alpha_c, alpha_i = float(bc[1]), float(bi[1])
    # Gonzalo-Granger weights: alpha_perp = (alpha_i, -alpha_c) normalized to sum 1.
    denom = alpha_i - alpha_c
    gg = dict(coinone=alpha_i / denom, leader=-alpha_c / denom) if abs(denom) > 1e-12 else None
    # Hasbrouck information shares of the pair from residual covariance, both orderings.
    omega = np.cov(np.vstack([rc, ri]))
    shares = None
    if gg:
        psi = np.array([gg['coinone'], gg['leader']])
        total = float(psi @ omega @ psi)
        if total > 0:
            def share(order):
                perm = np.array(order)
                F = np.linalg.cholesky(omega[np.ix_(perm, perm)])
                contrib = (psi[perm] @ F) ** 2 / total
                out = [0.0, 0.0]
                for k, idx in enumerate(perm):
                    out[idx] = float(contrib[k])
                return out
            a, b = share([0, 1]), share([1, 0])
            shares = dict(coinone=[min(a[0], b[0]), max(a[0], b[0])], leader=[min(a[1], b[1]), max(a[1], b[1])])
    horizons = {}
    for h in (5, 10, 30):
        fut = np.array(pc[h:]) - np.array(pc[:-h])
        zz = z[:-h]
        mm = ~(np.isnan(zz) | np.isnan(fut))
        if mm.sum() > 200:
            b, _, r2 = ols(fut[mm], [zz[mm]])
            horizons[h] = dict(beta=float(b[1]), r2=float(r2), n=int(mm.sum()))
    half_life = math.log(2) / -alpha_c if -1 < alpha_c < 0 else None
    return dict(n=int(m.sum()), alpha_coinone=alpha_c, alpha_leader=alpha_i, r2_coinone=float(r2c), r2_leader=float(r2i), half_life_s=half_life,
                gonzalo_granger=gg, information_share_bounds=shares, z_sd_bp=float(np.nanstd(z) * 1e4), horizons=horizons)


def joint_weights(pc, leaders, window=300, min_samples=60):
    """dp_c(t+1) on all leaders' error-correction terms; normalized negative betas."""
    import numpy as np
    zs = {}
    for name, pi in leaders.items():
        diff = np.array(pc) - np.array(pi)
        z = np.full(len(pc), np.nan)
        for t in range(len(pc)):
            w = diff[max(0, t - window):t]
            w = w[~np.isnan(w)]
            if len(w) >= min_samples and not np.isnan(diff[t]):
                z[t] = diff[t] - np.median(w)
        zs[name] = z
    dc = np.diff(pc)
    m = ~np.isnan(dc)
    for z in zs.values():
        m &= ~np.isnan(z[:-1])
    if m.sum() < 200 or not zs:
        return None
    names = list(zs)
    beta, _, r2 = ols(dc[m], [zs[n][:-1][m] for n in names])
    raw = {n: float(-beta[i + 1]) for i, n in enumerate(names)}
    positive = {n: max(0.0, v) for n, v in raw.items()}
    total = sum(positive.values())
    return dict(betas=raw, weights={n: v / total for n, v in positive.items()} if total > 0 else None, r2=float(r2), n=int(m.sum()))


def analyse(coinone, leaders, coins, *, window=300, min_samples=60):
    books = defaultdict(list)
    for recv, msg in coinone_rows(coinone):
        data = msg.get('data') or {}
        coin = data.get('target_currency')
        if coin not in coins or msg.get('channel') != 'ORDERBOOK':
            continue
        try:
            bid = max(float(r['price']) for r in data['bids'] if float(r['qty']) > 0)
            ask = min(float(r['price']) for r in data['asks'] if float(r['qty']) > 0)
        except (ValueError, KeyError, TypeError):
            continue
        if 0 < bid < ask:
            books[coin].append((recv, math.log((bid + ask) / 2)))
    lead = defaultdict(lambda: defaultdict(list))
    for path in leaders:
        for row in leader_rows(path):
            if row[0] == 'b' and row[3] in coins:
                lead[row[3]][row[2]].append((row[1], math.log(microprice(row[5], row[7], row[6], row[8]))))
    report = {}
    for coin in coins:
        if coin not in books or coin not in lead:
            continue
        t0 = (max([books[coin][0][0]] + [v[0][0] for v in lead[coin].values()]) // 1000 + 1) * 1000
        t1 = (min([books[coin][-1][0]] + [v[-1][0] for v in lead[coin].values()]) // 1000) * 1000
        if t1 - t0 < 600000:
            continue
        pc = series_grid(sorted(books[coin]), t0, t1)
        pls = {venue: series_grid(sorted(points), t0, t1) for venue, points in lead[coin].items()}
        pairs = {venue: pair_analysis(pc, pl, window, min_samples) for venue, pl in pls.items()}
        report[coin] = dict(seconds=(t1 - t0) // 1000, pairs=pairs, joint=joint_weights(pc, pls, window, min_samples))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--coinone', nargs='+', required=True)
    p.add_argument('--leaders', nargs='+', required=True)
    p.add_argument('--coins', nargs='+', default=['BTC', 'ETH', 'XRP', 'SOL'])
    p.add_argument('--output')
    args = p.parse_args()
    report = analyse(args.coinone, args.leaders, args.coins)
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    for coin, r in report.items():
        print(f"{coin}: {r['seconds']}s joint={json.dumps(r['joint'])}")
        for venue, pr in r['pairs'].items():
            if not pr:
                print(f"   {venue}: insufficient"); continue
            print(f"   {venue}: alpha_c={pr['alpha_coinone']:+.4f} alpha_l={pr['alpha_leader']:+.4f} half_life={pr['half_life_s'] and round(pr['half_life_s'],1)}s "
                  f"GG={pr['gonzalo_granger']} IS={pr['information_share_bounds']} z_sd={pr['z_sd_bp']:.2f}bp horizons={ {h:(round(v['beta'],3),round(v['r2'],3)) for h,v in pr['horizons'].items()} }")


if __name__ == '__main__':
    main()
