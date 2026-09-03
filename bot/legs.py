"""Where the deceleration detectors fire relative to the real extremes — the evidence table for upgrading the speed model.
  python -m bot.legs FILE... [--sym TRUMPUSDT] [--atr 3.0] [--quiet] [--day YYYYMMDD]
Legs = zigzag swings of >= --atr x ATR(1m) on the per-second mid (default: sig.dip_min_atr). For every leg bottom (a dip, the long
book's add) and every leg top (a pop, the short book's add / the long book's trim) the table gives the leg's depth, duration, speed
and the extreme of the engine's per-second velocity v (sigma units of 1-second returns) inside it, then for each detector the first
firing after the leg started: latency from the extreme (seconds; negative = fired before the extreme) and distance from the extreme
(% of price, and in units of the add step max(step_add_pct, step_add_atr x ATR15)). Detectors: the engine's DIP_SLOWING /
POP_STALLING by source (v = velocity model, 1m = candle rule, s8 = the third source, counted whether it trades or is recorded-only;
"any" = the signals the Strategy sees, shadow ones excluded), and offline candidate horizon speeds c_h = (mid - mid h seconds ago) /
ATR for h = 8/30/60/120 s, "decelerated" (causally) when the leg has fallen >= --atr x ATR and the fall's speed drops to <= 30% of
its running maximum (c8 = the same rule as the engine's s8 but measured from the confirmed pivot instead of the running swing high).
A leg has several pauses and every detector fires at pauses, so the table accounts for the ladder: "units" = firings the step gate
lets through before the extreme (each >= a step under the previous one — units a campaign spends on the leg), "last-pre" = the
distance of the last of those from the extreme, "post lat" = the first firing after the extreme, "actionable" = some let-through
firing within half a step of the extreme (the add that buys the bottom), "premature" = the first firing came before the extreme.
Forward = best bounce within 15 min.
--quiet prints the summaries only; --day keeps earlier files as warm-up and reports the legs of that UTC day."""
import json, os, statistics, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.replay import lines
from bot.signal import Features, STRAT
from bot.ws import load_params

H = (8, 30, 60, 120); DECEL = 0.3; WINDOW = 600; FWD = 900

def collect(files, sym, sig):
    feat = Features(sig); secs, sigs = [], []
    for path in files:
        for recv, raw in lines(path):
            if f'"instId":"{sym}"' not in raw or '"local"' in raw: continue
            try: j = json.loads(raw)
            except ValueError: continue                  # torn line: skip
            out = feat.feed(j); f = feat.f
            if f.get("t") and f.get("atr") and (not secs or secs[-1][0] != f["t"]):
                secs.append((f["t"], f["mid"], f["atr"], f["v"], f.get("atr15") or f["atr"]))
            for x in out:
                if x["sig"] in ("DIP_SLOWING", "POP_STALLING"): sigs.append((x["t"], x["sig"], x.get("src", "?"), x["mid"], bool(x.get("shadow"))))
    return secs, sigs

def pivots(secs, k, s):
    """Confirmed extremes of the s-oriented series (s=+1: bottoms are lows; s=-1: bottoms are highs) under a k x ATR reversal."""
    piv, dirn, hi, lo = [], 0, 0, 0
    for i, (t, m, atr, v, a15) in enumerate(secs):
        y, th = s * m, k * atr
        if dirn >= 0 and y > s * secs[hi][1]: hi = i
        if dirn <= 0 and y < s * secs[lo][1]: lo = i
        if dirn >= 0 and y <= s * secs[hi][1] - th: piv.append(("H", hi)); lo, dirn = i, -1
        elif dirn <= 0 and y >= s * secs[lo][1] + th: piv.append(("L", lo)); hi, dirn = i, 1
    return piv

def horizon_fires(secs, s, ih, il, h, k):
    """Causal candidate detector at horizon h, every firing inside the leg window: once the leg has fallen >= k x ATR (the engine's
    depth condition), a firing is the first second where the h-second fall speed drops to <= DECEL x its running maximum; after a
    firing the push must rebuild to >= half the leg's strongest push before the next one, and 60 s must pass. Returns [(t, mid)]."""
    yH = s * secs[ih][1]; lim = min(il + WINDOW, len(secs) - 1); best, imax, legmax, cool, fires = 0.0, None, 0.0, -1, []
    def sp(i):
        j = i - h
        if j < 0 or secs[i][0] - secs[j][0] != h: return None
        return -s * (secs[i][1] - secs[j][1]) / secs[i][2]          # > 0 while falling in s-space
    for i in range(ih, lim + 1):
        x = sp(i)
        if x is None: continue
        legmax = max(legmax, x)
        if x > best: best, imax = x, i; continue
        if best >= 0.5 * legmax > 0 and i > imax and i >= cool and (yH - s * secs[i][1]) / secs[i][2] >= k and x <= DECEL * best:
            fires.append((secs[i][0], secs[i][1])); best, imax, cool = 0.0, None, i + 60
    return fires

def account(fires, s, tL, pxL, step):
    """What a ladder makes of a detector's firings on one leg: units = firings the step gate lets through (each >= step under the
    previous one), before or at the bottom; the distance of the last of those from the bottom; the first firing after the bottom;
    actionable = some let-through firing within half a step of the extreme. None when nothing fired in the window."""
    if not fires: return None
    let, prev = [], None
    for t, mid in fires:
        if prev is None or s * (prev - mid) / mid * 100 >= step: let.append((t, mid)); prev = mid
    dist = lambda mid: s * (mid - pxL) / pxL * 100
    pre = [(t, mid) for t, mid in let if t <= tL]; post = [(t, mid) for t, mid in fires if t > tL]
    return dict(first=(fires[0][0] - tL, dist(fires[0][1])), units=len(pre), last_pre=dist(pre[-1][1]) if pre else None,
                post=(post[0][0] - tL, dist(post[0][1])) if post else None, actionable=min(dist(m) for _, m in let) <= step / 2)

def legs(secs, sigs, k, s, strat, day=None):
    name = "DIP_SLOWING" if s > 0 else "POP_STALLING"; piv = pivots(secs, k, s); rows = []
    sig_t = [(t, src, mid, sh) for t, sg, src, mid, sh in sigs if sg == name]
    for (kh, ih), (kl, il) in zip(piv, piv[1:]):
        if kh != "H" or kl != "L": continue
        t0, tL, pxL, atrL, a15 = secs[ih][0], secs[il][0], secs[il][1], secs[il][2], secs[il][4]
        if day and time.strftime("%Y%m%d", time.gmtime(tL)) != day: continue
        depth = s * (secs[ih][1] - pxL) / pxL * 100; dur = max(tL - t0, 1)
        step = max(strat["step_add_pct"], strat["step_add_atr"] * a15 / pxL * 100) if strat["step_add_atr"] > 0 else strat["step_add_pct"]
        if strat.get("step_add_max_pct"): step = min(step, strat["step_add_max_pct"])   # as the engine (signal.add_step)
        vext = min(s * secs[i][3] for i in range(ih, il + 1))
        det = {}
        for src in ("any", "v", "1m", "s8"):
            det[src] = account([(t, mid) for t, sc, mid, sh in sig_t if t0 < t <= tL + WINDOW and ((not sh) if src == "any" else sc == src)], s, tL, pxL, step)
        for h in H:
            det[f"c{h}"] = account(horizon_fires(secs, s, ih, il, h, k), s, tL, pxL, step)
        fwd = max((s * (secs[i][1] - pxL) / pxL * 100 for i in range(il + 1, min(il + FWD, len(secs) - 1) + 1)), default=None)
        rows.append(dict(t=tL, px=pxL, depth=depth, depth_atr=s * (secs[ih][1] - pxL) / atrL, dur=dur, speed=depth / dur * 60, vext=vext, step=step, det=det, fwd=fwd))
    return rows

def summarize(rows, label):
    keys = ["any", "v", "1m", "s8"] + [f"c{h}" for h in H]
    print(f"  {label}: {len(rows)} legs, depth med {statistics.median(r['depth'] for r in rows):.2f}% ({statistics.median(r['depth_atr'] for r in rows):.1f} ATR), "
          f"dur med {statistics.median(r['dur'] for r in rows):.0f}s, speed med {statistics.median(r['speed'] for r in rows):.2f}%/min, "
          f"|v| extreme med {statistics.median(abs(r['vext']) for r in rows):.2f} sigma (legs with |v|<1: {sum(1 for r in rows if abs(r['vext']) < 1) / len(rows) * 100:.0f}%), "
          f"bounce 15m med {statistics.median(r['fwd'] for r in rows if r['fwd'] is not None):+.2f}%" if rows else f"  {label}: no legs")
    if not rows: return
    print(f"  {'detector':<9}{'caught':>8}{'1st lat':>9}{'1st dist':>10}{'units':>7}{'last-pre':>10}{'post lat':>10}{'actionable':>11}{'premature':>10}")
    med = lambda xs: statistics.median(xs) if xs else float("nan")
    for k in keys:
        hits = [r["det"][k] for r in rows if r["det"].get(k) is not None]
        if not hits: print(f"  {k:<9}{'0/%d' % len(rows):>8}"); continue
        print(f"  {k:<9}{'%d/%d' % (len(hits), len(rows)):>8}{med([h['first'][0] for h in hits]):>8.0f}s{med([h['first'][1] for h in hits]):>9.2f}%"
              f"{med([h['units'] for h in hits]):>7.1f}{med([h['last_pre'] for h in hits if h['last_pre'] is not None]):>9.2f}%"
              f"{med([h['post'][0] for h in hits if h['post']]):>9.0f}s{sum(1 for h in hits if h['actionable']) / len(rows) * 100:>10.0f}%"
              f"{sum(1 for h in hits if h['first'][0] < 0) / len(hits) * 100:>9.0f}%")

def run(files, sym, sig, k, quiet, day):
    p = load_params() or {}; sig = {**(p.get("sig") or {}), **(sig or {})}; strat = {**STRAT, **(p.get("strat") or {})}
    k = k or sig.get("dip_min_atr", 3.0)
    secs, sigs = collect(files, sym, sig)
    g = lambda d: "        -       " if d is None else f"{d['first'][0]:+5.0f}s {d['first'][1]:+.2f}% u{d['units']}{'*' if d['actionable'] else ' '}"
    for s, label in ((1, "DIPS (bottoms; long add)"), (-1, "POPS (tops; long trim / short add)")):
        rows = legs(secs, sigs, k, s, strat, day)
        if not quiet:
            print(f"--- {label}: extreme time, depth, duration, speed, v extreme | first firing: latency, distance, units let through (* = one within half a step) | v | 1m | s8 | c8 | c30 | c60 | c120 | bounce")
            for r in rows:
                d = r["det"]
                print(f"{time.strftime('%m-%d %H:%M:%S', time.gmtime(r['t']))} {r['px']:.4f} {r['depth']:5.2f}% {r['depth_atr']:4.1f}A {r['dur']:5.0f}s {r['speed']:5.2f}%/m v{r['vext']:+5.2f} | "
                      f"{g(d['v'])} | {g(d['1m'])} | {g(d['s8'])} | {g(d['c8'])} | {g(d['c30'])} | {g(d['c60'])} | {g(d['c120'])} | {r['fwd'] if r['fwd'] is None else round(r['fwd'], 2)}")
        summarize(rows, label)
    print(f"--- {len(secs)} seconds, {len(sigs)} signals, legs >= {k} x ATR")

if __name__ == "__main__":
    args = sys.argv[1:]; files, sym, sig, k, quiet, day = [], "TRUMPUSDT", {}, None, False, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--sym": sym = args[i + 1]; i += 2
        elif a == "--sig": kk, v = args[i + 1].split("="); sig[kk] = float(v); i += 2
        elif a == "--atr": k = float(args[i + 1]); i += 2
        elif a == "--quiet": quiet = True; i += 1
        elif a == "--day": day = args[i + 1]; i += 2
        else: files.append(a); i += 1
    if not files: print(__doc__); sys.exit(0)
    run(files, sym, sig, k, quiet, day)
