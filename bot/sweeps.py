"""Stop-hunt evidence: how often price sweeps under a confirmed 15m/1H pivot low (over a pivot high) and reclaims it, how deep, and
what follows — the two faces of stop hunting for the cycle: (defence) our stop sits pivot - stop_buffer_atr x ATR15, so a reclaimed
sweep deeper than the buffer is a hunt that would have stopped us; (offence) a sweep-and-reclaim is the sharpest form of the
post-crash deceleration and a candidate third detector.  python -m bot.sweeps FILE... [--sym TRUMPUSDT] [--quiet] [--day YYYYMMDD]
Per second the engine's own levels are used (Features.htf_lows / htf_highs, the levels the structural stop is chosen from). A sweep
starts when the mid crosses under the nearest pivot low below the price (over the nearest high above it), tracks the extreme depth
in ATR15 and in % of price, and ends when the mid is back on the level side (reclaimed) or after 30 min (a real break).
Per sweep: level, depth (ATR15, %), time under, reclaimed, and for reclaimed ones the forward path from the reclaim second (mid change
after 5/15 min, max favourable/adverse within 15 min). Summary: sweeps per hour, reclaim rate, depth median, the share of reclaimed
sweeps deeper than 0.3 / 0.5 / 0.8 / 1.2 ATR15 (the buffer a hunt-proof stop needs), forward returns after a reclaim vs after a break."""
import json, os, statistics, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.replay import lines
from bot.signal import Features
from bot.ws import load_params

BUFFERS = (0.3, 0.5, 0.8, 1.2); TIMEOUT = 1800; FWD = 900

def collect(files, sym, sig):
    feat = Features(sig); secs = []
    for path in files:
        for recv, raw in lines(path):
            if f'"instId":"{sym}"' not in raw or '"local"' in raw: continue
            feat.feed(json.loads(raw)); f = feat.f
            if f.get("t") and f.get("atr") and (not secs or secs[-1][0] != f["t"]):
                secs.append((f["t"], f["mid"], f.get("atr15") or f["atr"], tuple(f.get("htf_lows") or ()), tuple(f.get("htf_highs") or ())))
    return secs

MIN_AGE = 900   # a level counts only after it has stood for 15 min: the in-progress leg's low moves with the price and is not a stop anyone rests on

def sweeps(secs, s, day=None):
    """s=+1: sweeps under pivot lows (long side); s=-1: over pivot highs (short side, mirrored)."""
    out, cur, seen = [], None, {}
    for i, (t, mid, a15, lows, highs) in enumerate(secs):
        lv = lows if s > 0 else highs
        for x in lv: seen.setdefault(x, t)
        if cur is None:
            below = [x for x in lv if s * (x - mid) > 0 and t - seen[x] >= MIN_AGE]   # confirmed levels already crossed by the price
            if below:
                level = min(below) if s > 0 else max(below)           # the nearest one (the first crossed)
                cur = dict(i0=i, t0=t, level=level, depth=0.0, a15=a15 or 0.0)
        if cur is not None:
            d = s * (cur["level"] - mid); cur["depth"] = max(cur["depth"], d)
            reclaimed = s * (mid - cur["level"]) >= 0
            if reclaimed or t - cur["t0"] >= TIMEOUT:
                fwd = {}
                if reclaimed:
                    m0 = mid; path = [x[1] for x in secs[i + 1:i + FWD + 1]]
                    for k, n in (("5m", 300), ("15m", 900)):
                        fwd[k] = s * (secs[i + n][1] / m0 - 1) * 100 if i + n < len(secs) else None
                    fwd["mfe"] = max((s * (x / m0 - 1) * 100 for x in path), default=None); fwd["mae"] = min((s * (x / m0 - 1) * 100 for x in path), default=None)
                cur.update(t1=t, reclaimed=reclaimed, depth_atr=cur["depth"] / cur["a15"] if cur["a15"] else None, depth_pct=cur["depth"] / cur["level"] * 100, fwd=fwd)
                if not day or time.strftime("%Y%m%d", time.gmtime(cur["t0"])) == day: out.append(cur)
                cur = None
    return out

def summarize(rows, label, hours):
    if not rows: print(f"  {label}: no sweeps"); return
    rec = [r for r in rows if r["reclaimed"]]; brk = [r for r in rows if not r["reclaimed"]]
    med = lambda xs: statistics.median(xs) if xs else float("nan")
    dep = [r["depth_atr"] for r in rec if r["depth_atr"] is not None]
    print(f"  {label}: {len(rows)} sweeps ({len(rows) / max(hours, 1e-9):.2f}/h), reclaimed {len(rec)} ({len(rec) / len(rows) * 100:.0f}%), "
          f"depth med {med(dep):.2f} ATR15 ({med([r['depth_pct'] for r in rec]):.2f}%), time under med {med([r['t1'] - r['t0'] for r in rec]):.0f}s")
    print("  reclaimed sweeps deeper than the buffer (our stop would have been hunted): " + "  ".join(f"{b:.1f}ATR:{sum(1 for x in dep if x > b) / len(dep) * 100:.0f}%" for b in BUFFERS) if dep else "")
    for name, rs in (("after reclaim", rec), ("after break (no reclaim in 30 min)", brk)):
        fw = [r["fwd"] for r in rs if r.get("fwd") and r["fwd"].get("15m") is not None]
        if fw: print(f"  {name}: n={len(fw)} 5m={med([f['5m'] for f in fw if f['5m'] is not None]):+.2f}% 15m={med([f['15m'] for f in fw]):+.2f}% mfe={med([f['mfe'] for f in fw]):+.2f}% mae={med([f['mae'] for f in fw]):+.2f}%")

def main():
    args = sys.argv[1:]; files, sym, quiet, day = [], "TRUMPUSDT", False, None; i = 0
    while i < len(args):
        a = args[i]
        if a == "--sym": sym = args[i + 1]; i += 2
        elif a == "--quiet": quiet = True; i += 1
        elif a == "--day": day = args[i + 1]; i += 2
        else: files.append(a); i += 1
    if not files: print(__doc__); sys.exit(0)
    sig = (load_params() or {}).get("sig") or {}
    secs = collect(files, sym, sig); hours = (secs[-1][0] - secs[0][0]) / 3600 if len(secs) > 1 else 0
    for s, label in ((1, "UNDER pivot lows (long stop / long entry)"), (-1, "OVER pivot highs (short stop / short entry)")):
        rows = sweeps(secs, s, day)
        if not quiet:
            for r in rows:
                fw = r.get("fwd") or {}
                print(f"{time.strftime('%m-%d %H:%M:%S', time.gmtime(r['t0']))} lvl {r['level']:.4f} depth {r['depth_atr'] if r['depth_atr'] is not None else float('nan'):.2f}ATR {r['depth_pct']:.2f}% "
                      f"{'reclaimed' if r['reclaimed'] else 'BREAK'} after {r['t1'] - r['t0']:.0f}s" + (f" | fwd 15m {fw['15m']:+.2f}% mfe {fw['mfe']:+.2f}% mae {fw['mae']:+.2f}%" if fw.get("15m") is not None else ""))
        summarize(rows, label, hours)
    print(f"--- {len(secs)} seconds, levels = the engine's 15m/1H pivots, stop buffer in params {(load_params() or {}).get('strat', {}).get('stop_buffer_atr', 0.3)} ATR15")

if __name__ == "__main__":
    main()
