"""The estimator against the live ledger, symbol by symbol and UTC day by day — the paired table NEXT 6 (1, 2) and 8 judge on.
Estimator side: every scan's rows kept in logs/scan-history.jsonl (p_up, trials_h, edge, impact, entry), the day's median per symbol.
Live side: track_a.cycles — completed lot cycles that day: n, cycles per engine-hour (hours = span from the day's first cycle open to its
last close, floored at 1 h), win rate, mean gross win / loss % of notional. Columns to read: win - p_up (the estimator's pessimism;
NEXT 6.1) and cyc/h / tr/h (how many trials the engine converts; NEXT 6.2 — the product form assumes a constant).
  python -m track_a.pair [--day YYYYMMDD] [--sym SYMBOL]
Read-only: logs/scan-history.jsonl, logs/events.jsonl."""
from common.paths import runtime_root
import json, os, sys, time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(runtime_root(ROOT), "logs", "scan-history.jsonl")

def _utc_day(t):
    return time.strftime("%Y%m%d", time.gmtime(time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S"))))

def _secs(t): return time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S"))

def estimator(day=None, sym=None, path=HIST):
    """{(symbol, day): {p_up, trials_h, edge, impact, entry, scans}} — medians over the day's scans."""
    acc = defaultdict(lambda: defaultdict(list))
    if not os.path.exists(path): return {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try: rec = json.loads(line)
            except Exception: continue
            d = _utc_day(rec["t"])
            if day and d != day: continue
            for r in rec.get("rows") or []:
                if sym and r["symbol"] != sym: continue
                k = (r["symbol"], d)
                for f in ("p_up", "trials_h", "edge", "impact"):
                    if r.get(f) is not None: acc[k][f].append(float(r[f]))
                acc[k]["entry"].append(1.0 if r.get("entry") else 0.0)
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
    return {k: {f: med(v[f]) for f in ("p_up", "trials_h", "edge", "impact", "entry")} | dict(scans=len(v["p_up"])) for k, v in acc.items()}

def live(done, day=None, sym=None):
    """{(symbol, day): {n, hours, cyc_h, win, gwin, gloss}} from track_a.cycles' completed cycles (closed that UTC day)."""
    g = defaultdict(list)
    for c in done:
        s = c.get("symbol")
        if sym and s != sym: continue
        d = _utc_day(c["t1"])
        if day and d != day: continue
        g[(s, d)].append(c)
    out = {}
    for k, cs in g.items():
        t0 = min(_secs(c["t0"]) for c in cs); t1 = max(_secs(c["t1"]) for c in cs); hours = max((t1 - t0) / 3600, 1.0)
        gr = [(c["gross"] / (c["qty"] * c["entry"]) * 100, c["net"], c["net"] / (c["qty"] * c["entry"]) * 100) for c in cs if c["qty"] and c["entry"]]
        wins = [a for a, n, _ in gr if n > 0]; losses = [a for a, n, _ in gr if n <= 0]
        net_c = sum(x for _, _, x in gr) / len(gr) if gr else None
        out[k] = dict(n=len(cs), hours=hours, cyc_h=len(cs) / hours, win=len(wins) / len(gr) if gr else None,
                      gwin=sum(wins) / len(wins) if wins else None, gloss=abs(sum(losses) / len(losses)) if losses else None,
                      net_c=net_c, edge_h=net_c * len(cs) / hours if net_c is not None else None)   # edge_h: the score's unit, % of one unit's notional per engine-hour
    return out

def table(est, lv):
    """One row per symbol x day, then a BASKET row per day (the average live book) with the estimator's #1 of that day marked '*':
    the switch-to-single test (RULES select 절) is '#1 beats BASKET on edge/h for weeks'."""
    f = lambda x, w, p=2: f"{x:>{w}.{p}f}" if isinstance(x, (int, float)) and x is not None else f"{'-':>{w}}"
    lines = [f"  {'symbol':<11}{'day':<9}{'scans':>6}{'p_up':>6}{'tr/h':>6}{'edge':>7}{'imp%':>7}{'entry':>6} | {'n':>4}{'hours':>6}{'cyc/h':>6}{'win':>6}{'gwin%':>7}{'gloss%':>7}{'net%':>7}{'edge/h':>8} | {'win-p_up':>9}{'cyc/tr':>7}"]
    days = sorted({k[1] for k in set(est) | set(lv)})
    for d in days:
        keys = sorted(k for k in set(est) | set(lv) if k[1] == d)
        best = max((k for k in keys if est.get(k, {}).get("edge") is not None), key=lambda k: est[k]["edge"], default=None)
        for k in keys:
            e, l = est.get(k, {}), lv.get(k, {})
            dw = (l["win"] - e["p_up"]) if l.get("win") is not None and e.get("p_up") is not None else None
            ratio = (l["cyc_h"] / e["trials_h"]) if l.get("cyc_h") is not None and e.get("trials_h") else None
            lines.append(f"  {(k[0] + ('*' if k == best else '')):<11}{k[1]:<9}{e.get('scans', 0):>6}{f(e.get('p_up'), 6)}{f(e.get('trials_h'), 6)}{f(e.get('edge'), 7, 3)}{f(e.get('impact'), 7, 3)}{f(e.get('entry'), 6, 1)} | "
                         f"{l.get('n', 0):>4}{f(l.get('hours'), 6, 1)}{f(l.get('cyc_h'), 6)}{f(l.get('win'), 6)}{f(l.get('gwin'), 7, 3)}{f(l.get('gloss'), 7, 3)}{f(l.get('net_c'), 7, 3)}{f(l.get('edge_h'), 8, 4)} | {f(dw, 9)}{f(ratio, 7)}")
        ls = [lv[k] for k in keys if k in lv and lv[k].get("net_c") is not None]
        if ls:
            n = sum(l["n"] for l in ls)
            lines.append(f"  {'BASKET':<11}{d:<9}{'':>6}{'':>6}{'':>6}{'':>7}{'':>7}{'':>6} | {n:>4}{'':>6}{f(sum(l['cyc_h'] for l in ls) / len(ls), 6)}{f(sum(l['win'] * l['n'] for l in ls) / n, 6)}{'':>7}{'':>7}"
                         f"{f(sum(l['net_c'] * l['n'] for l in ls) / n, 7, 3)}{f(sum(l['edge_h'] for l in ls) / len(ls), 8, 4)} |")
    return "\n".join(lines)

def main(argv):
    from track_a.cycles import build
    a = argv; day = a[a.index("--day") + 1] if "--day" in a else None; sym = a[a.index("--sym") + 1] if "--sym" in a else None
    est = estimator(day, sym)
    try: done = build(sym=sym)[0]
    except FileNotFoundError: done = []
    lv = live(done, day, sym)
    print(f"# estimator (scan-history, since 2026-09-02) vs live cycles — {len(est)} symbol-days with scans, {len(lv)} with cycles")
    print(table(est, lv))

if __name__ == "__main__":
    main(sys.argv[1:])
