"""Bounded, walk-forward parameter tuning on recordings. Report first; --apply only when every acceptance rule passes.
  python -m track_a.tune [--days 7] [--sym A,B] [--keys dip_min_atr,v_fast] [--workers 6] [--apply]
Data: the last --days of recordings (data/ws/pub-*.jsonl[.gz]), split walk-forward — train = all days but the last, validate = the
last day (with a single day: train = first 2/3 of the files, validate = the rest). Symbols: params.books (the basket) unless --sym;
a candidate is scored on every symbol's tape at live sizing (track_a.backtest's equity sizing, one equity for the whole run) and the
metrics are summed — the rules are common to the basket, so a change has to pay across it, not on one symbol.
Search: a coordinate neighbourhood, not a grid — for each key in --keys (default: rotate through SPACE by weekday) the incumbent
value moves one grid step down or up. Each nightly run can therefore change each key by at most one step (slow drift) and only
inside the grid bounds. Objective = train pnl − train max_dd (USDT).
Acceptance (all required): train cycles ≥ MIN_CYCLES; candidate objective ≥ incumbent × (1 + MIN_GAIN) on train; validate total
≥ incumbent's validate total; the candidate's own grid neighbours (one further step) score ≥ the incumbent on train (a plateau,
not a spike). Otherwise the report says "keep".
Output: logs/tune-YYYYMMDD.json and a printed table. --apply writes the accepted values into params.json (hot-reloaded by cycle.py)."""
from common.paths import runtime_root
import glob, json, os, sys, time
from multiprocessing import Pool
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from track_a.backtest import run_files, latest_equity
from common.ws import load_params, PARAMS, portfolio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPACE = {  # (section, grid) — coarse on purpose; add keys here to make them tunable
    "dip_min_atr": ("sig", [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]),
    "v_fast": ("sig", [0.5, 0.7, 1.0, 1.3, 1.6, 2.0]),
    "v_slow": ("sig", [0.15, 0.2, 0.3, 0.45, 0.6]),
    "hold_s": ("sig", [2, 3, 5, 8]),
    "c1_on": ("sig", [0, 1]),
    "pop_min_pct": ("strat", [0.25, 0.3, 0.4, 0.6, 0.8]),
    "unit_min_pct": ("strat", [0.1, 0.15, 0.25, 0.4]),
    "step_add_atr": ("strat", [0.3, 0.4, 0.5, 0.7, 1.0]),
    "gap_rebuy_pct": ("strat", [0.15, 0.3, 0.5, 0.8]),
    # derisk_pct / derisk_core_frac / brk_atr / stop_buffer_atr: out of the grid while the de-risk family is switched off (params derisk_pct 0,
    # user decision 2026-09-02, NEXT 3) — inert keys would only burn the nightly budget; put them back when the family returns
    "gate_relax": ("strat", [0.25, 0.35, 0.5, 0.7]),          # how much one refused bounce lowers the trim gate (0 would switch the mechanism off: not offered)
    "trim_retrace_atr": ("strat", [0.3, 0.5, 0.8, 1.2]),      # retrace that confirms a top (0 = no wick protection: not offered, user decision)
    "trim_taker_after_s": ("strat", [5, 10, 20, 60]),          # the trim's maker -> taker clock (2026-08-30 tape: the clock barely matters, the slip trigger carries the value)
    "trim_taker_slip_pct": ("strat", [0.05, 0.1, 0.2, 0.4]),   # ... and the slip under the pull price that says "the stall has turned"
    "rg_drift": ("sig", [4.0, 5.0, 6.0, 8.0, 10.0]),           # circuit breaker: false positives cost cycles, misses cost a cascade; the objective sees both
    "rg_counter_max": ("sig", [0, 1, 2]),
    "rg_window": ("sig", [45, 60, 90, 120, 180]),
    "rg_drift_min_pct": ("sig", [1.0, 2.0, 3.0, 5.0]),         # a one-way that matters: the window's net move in % (0 would restore the ATR-only reading: not offered)
    "against_regime_mult": ("strat", [0.25, 0.5, 0.75, 1.0]),  # AGAINST scales the add (0 would restore the veto: not offered, user decision 2026-08-30)
    "retrace_frac": ("strat", [0.2, 0.33, 0.5]),               # the share of the bounce a top must give back (0 would restore the ATR-only wiggle: not offered)
    "c1_dev": ("sig", [0.25, 0.3, 0.4, 0.5, 0.7]),             # the 1m rule's depth in % — 3.3..10 ATR across the basket (NEXT 1 unit mismatch): the basket, not one symbol, judges it
    "rg_leg_pct": ("sig", [1.5, 2.0, 3.0]),                    # the current-leg read's depth (NEXT 1): live experiment from 2026-09-02, numbers judged here
}
MIN_CYCLES, MIN_GAIN = 30, 0.10
PRIORITY = ("retrace_frac", "rg_leg_pct", "c1_dev")   # keys under live experiment (NEXT 1, 5; 2026-09-02): judged every night on top of the two rotated keys, not once in ten days

def arg(flag, default):
    return type(default)(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default

def objective(m): return m["total"] - m["max_dd"]

def evaluate(job):
    """One candidate on one file set: the basket's symbols summed (pnl, total, cycles; max_dd summed = the conservative bound)."""
    files, syms, sig, strat, equity = job
    ms = [run_files(files, sym, sig, strat, equity=equity) for sym in syms]
    return dict(pnl=round(sum(m["pnl"] for m in ms), 3), total=round(sum(m["total"] for m in ms), 3), max_dd=round(sum(m["max_dd"] for m in ms), 3),
                cycles=sum(m["cycles"] for m in ms), per={s: dict(pnl=m["pnl"], total=m["total"], cycles=m["cycles"]) for s, m in zip(syms, ms)})

def main():
    days, workers = arg("--days", 7), arg("--workers", 6)
    p = load_params() or {}; sig0, strat0 = dict(p.get("sig") or {}), dict(p.get("strat") or {})
    syms = arg("--sym", "").split(",") if "--sym" in sys.argv else [s for s in portfolio(p) if s]
    equity = latest_equity()                                  # one wallet for every candidate: the sizing must not differ between them
    files = sorted(glob.glob(os.path.join(runtime_root(ROOT), "data", "ws", "pub-*.jsonl*")))
    files = [f for f in files if not (f.endswith(".jsonl") and os.path.exists(f + ".gz"))]
    active = [f for f in files if f.endswith(".jsonl") and time.time() - os.path.getmtime(f) < 900]   # still being written: every candidate must see the same tape
    files = [f for f in files if f not in active]
    if active: print(f"excluded {len(active)} file(s) still being recorded")
    days_avail = sorted({os.path.basename(f)[4:12] for f in files})[-days:]
    files = [f for f in files if os.path.basename(f)[4:12] in days_avail]
    if len(days_avail) >= 2:
        train = [f for f in files if os.path.basename(f)[4:12] != days_avail[-1]]; valid = [f for f in files if os.path.basename(f)[4:12] == days_avail[-1]]
    else:
        k = max(1, len(files) * 2 // 3); train, valid = files[:k], files[k:]
    rotated = [list(SPACE)[(time.localtime().tm_yday * 2 + i) % len(SPACE)] for i in range(2)]
    keys = arg("--keys", "").split(",") if "--keys" in sys.argv else [k for k in PRIORITY if k in SPACE] + [k for k in rotated if k not in PRIORITY]
    print(f"train {len(train)} files, validate {len(valid)} files, keys {keys}, symbols {syms}, equity {equity}", flush=True)
    # candidates: incumbent, and for each key its one-step neighbours (plus two-step neighbours for the plateau check)
    cands = {"incumbent": (sig0, strat0)}
    for k in keys:
        sec, grid = SPACE[k]; cur = (sig0 if sec == "sig" else strat0).get(k, grid[len(grid) // 2])
        i = min(range(len(grid)), key=lambda j: abs(grid[j] - cur))
        for step in (-2, -1, 1, 2):
            j = i + step
            if 0 <= j < len(grid):
                s2, t2 = dict(sig0), dict(strat0); (s2 if sec == "sig" else t2)[k] = grid[j]
                cands[f"{k}={grid[j]}"] = (s2, t2)
    if not valid:
        print("no validation files (need >= 2 days, or >= 2 files): report only, nothing can be accepted"); MIN_ACCEPT = False
    else: MIN_ACCEPT = True
    from track_a.backtest import load_seconds, seed_history, contract_meta
    for sym in syms:
        contract_meta(sym)                                # the quantity step, cached once
        for f in train + valid: load_seconds(f, sym)      # build the per-second caches once, before the workers read them
        for fs in (train, valid):                         # and each set's REST seed once: a burst of worker fetches could be rate-limited and leave some candidates unseeded (incomparable)
            secs = load_seconds(fs[0], sym) if fs else []
            if secs: seed_history(sym, secs[0][0])
    jobs = [(train, syms, s, t, equity) for s, t in cands.values()] + [(valid or train, syms, s, t, equity) for s, t in cands.values()]
    with Pool(min(workers, len(jobs))) as pool: res = pool.map(evaluate, jobs)
    n = len(cands); names = list(cands)
    tr = dict(zip(names, res[:n])); va = dict(zip(names, res[n:]))
    inc_tr, inc_va = tr["incumbent"], va["incumbent"]
    print(f"{'candidate':<20}{'train pnl':>10}{'dd':>7}{'cyc':>5}{'obj':>8}{'valid tot':>10}{'cyc':>5}  verdict")
    accepted = {}
    for name in names:
        m, v = tr[name], va[name]; obj = objective(m); verdict = ""
        if name != "incumbent":
            k, val = name.split("="); sec, grid = SPACE[k]; val = float(val)
            i = grid.index(val) if val in grid else min(range(len(grid)), key=lambda j: abs(grid[j] - val))
            cur = (sig0 if sec == "sig" else strat0).get(k, grid[len(grid) // 2]); ci = min(range(len(grid)), key=lambda j: abs(grid[j] - cur))
            one_step = abs(i - ci) == 1
            j = i + (1 if i > ci else -1)
            further = f"{k}={grid[j]}" if 0 <= j < len(grid) else None
            plateau = further is not None and further in tr and objective(tr[further]) >= objective(inc_tr)   # at the grid edge there is no plateau evidence: not accepted
            ok = (MIN_ACCEPT and one_step and m["cycles"] >= MIN_CYCLES and obj >= objective(inc_tr) + abs(objective(inc_tr)) * MIN_GAIN + 1e-9
                  and v["total"] >= inc_va["total"] and plateau)
            verdict = "ACCEPT" if ok else ("-" if not one_step else "keep" + ("(n<%d)" % MIN_CYCLES if m["cycles"] < MIN_CYCLES else ""))
            if ok and (k not in accepted or obj > accepted[k][1]): accepted[k] = (grid[i], obj, sec)
        print(f"{name:<20}{m['pnl']:10.2f}{m['max_dd']:7.2f}{m['cycles']:5d}{obj:8.2f}{v['total']:10.2f}{v['cycles']:5d}  {verdict}")
    if len(accepted) > 1:      # keys were judged one at a time: the combination must pass the same bar before it is applied together
        s2, t2 = dict(sig0), dict(strat0)
        for k, (val, _, sec) in accepted.items(): (s2 if sec == "sig" else t2)[k] = val
        ctr, cva = evaluate((train, syms, s2, t2, equity)), evaluate((valid or train, syms, s2, t2, equity))
        best_single = max(objective(tr[f"{k}={v[0]}"]) for k, v in accepted.items())
        if not (objective(ctr) >= best_single and cva["total"] >= inc_va["total"] and ctr["cycles"] >= MIN_CYCLES):
            print(f"combined {list(accepted)} fails together (train obj {objective(ctr):.2f} vs best single {best_single:.2f}, valid {cva['total']:.2f}): keeping only the best single key")
            kbest = max(accepted, key=lambda k: accepted[k][1]); accepted = {kbest: accepted[kbest]}
        else: print(f"combined {list(accepted)}: train obj {objective(ctr):.2f}, valid {cva['total']:.2f} -> ok")
    report = dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), symbols=syms, equity=equity, days=days_avail, keys=keys, train=tr, valid=va, accepted={k: v[0] for k, v in accepted.items()})
    os.makedirs(os.path.join(runtime_root(ROOT), "logs"), exist_ok=True)
    with open(os.path.join(runtime_root(ROOT), "logs", f"tune-{time.strftime('%Y%m%d')}.json"), "w", encoding="utf-8") as f: json.dump(report, f)
    print("accepted:", {k: v[0] for k, v in accepted.items()} or "none")
    if accepted and "--apply" in sys.argv:
        p = load_params() or {}
        for k, (val, _, sec) in accepted.items(): p.setdefault(sec, {})[k] = val
        with open(PARAMS, "w", encoding="utf-8") as f: json.dump(p, f, indent=2)
        print("applied to params.json")

if __name__ == "__main__":
    main()
