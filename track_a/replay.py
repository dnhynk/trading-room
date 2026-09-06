"""Replay recordings through common/signal.py.
  python -m track_a.replay FILE... [--sym TRUMPUSDT] [--sig k=v ...] [--all] [--by src,sell_decay,cvd_div,vp_va] [--quiet]
Prints one line per DIP_SLOWING / POP_STALLING (BREAKDOWN/BREAKOUT with --all) with the features that produced it and the forward
path: mid change after 1/5/15 min and the max favorable / adverse excursion within 15 min (in % of mid, favorable = the direction
the signal implies), plus "hold vs sell": the pnl of holding the side the stall would have sold (a long after POP_STALLING) with a
trailing stop of 0.5 / 1.5 ATR for up to 30 min — positive means holding the core beat selling it at the stall (the average/direction
concept's question, split by structure with --by side_hint_1h), then a summary per signal type, and with --by a summary split by the given feature values (booleans and
small integers; floats are split at their median). --quiet prints only the summaries. Thresholds are tuned against this table."""
import gzip, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.ws import load_params
from common.signal import Features

def lines(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        for line in f:
            i = line.find("\t")
            if i > 0: yield int(line[:i]), line[i + 1:]

def hold_pnl(m0, path, d, trail):
    """% pnl of holding direction d from m0 with a trailing stop `trail` (price units) under the running favourable extreme; exit at
    the trail hit or the end of the path. Against 0 (= sold at the stall) it says whether holding the core beat selling it."""
    if not path or trail <= 0: return None
    ext = m0
    for m in path:
        if d * (m - ext) > 0: ext = m
        if d * (ext - m) >= trail: return d * (m - m0) / m0 * 100
    return d * (path[-1] - m0) / m0 * 100

def summarize(rows, label):
    for name in ("DIP_SLOWING", "POP_STALLING"):
        rs = [fwd for x, fwd in rows if x["sig"] == name and fwd.get("15m") is not None]
        if not rs: continue
        mean = lambda k: sum(r[k] for r in rs) / len(rs)
        win = sum(1 for r in rs if r["mfe"] >= 0.5) / len(rs) * 100
        hs = [r for r in rs if r.get("hold05") is not None]
        hold = (f" | hold vs sell: trail.5={sum(r['hold05'] for r in hs) / len(hs):+.2f}%({sum(1 for r in hs if r['hold05'] > 0) / len(hs) * 100:.0f}% won) "
                f"trail1.5={sum(r['hold15'] for r in hs) / len(hs):+.2f}%({sum(1 for r in hs if r['hold15'] > 0) / len(hs) * 100:.0f}% won)") if hs else ""
        print(f"  {label:<28} {name:<12} n={len(rs):3d} 1m={mean('1m'):+.2f}% 5m={mean('5m'):+.2f}% 15m={mean('15m'):+.2f}% mfe={mean('mfe'):+.2f}% mae={mean('mae'):+.2f}% hit={win:.0f}%{hold}")

def split_by(rows, key):
    vals = [x.get(key) for x, _ in rows if x.get(key) is not None]
    if not vals: return
    if all(isinstance(v, (bool, int, str)) for v in vals) and len(set(vals)) <= 6:
        for v in sorted(set(vals), key=str): summarize([r for r in rows if r[0].get(key) == v], f"{key}={v}")
    else:
        med = sorted(vals)[len(vals) // 2]
        summarize([r for r in rows if r[0].get(key) is not None and r[0][key] < med], f"{key}<{med:.3g}")
        summarize([r for r in rows if r[0].get(key) is not None and r[0][key] >= med], f"{key}>={med:.3g}")

def run(files, sym, sig, show_all, by=(), quiet=False, day=None):
    """day='YYYYMMDD' keeps earlier files as warm-up only: their signals are not reported."""
    feat = Features({**((load_params() or {}).get("sig") or {}), **(sig or {})}); sigs, mids = [], []   # the running signal values, as nightly evaluates what trades
    for path in files:
        for recv, raw in lines(path):
            if f'"instId":"{sym}"' not in raw or '"local"' in raw: continue
            try: j = json.loads(raw)
            except ValueError: continue                  # torn line: skip
            out = feat.feed(j)
            if feat.f.get("t") and (not mids or mids[-1][0] != feat.f["t"]): mids.append((feat.f["t"], feat.f["mid"]))
            sigs += out
    idx = {t: i for i, (t, _) in enumerate(mids)}
    rows = []
    for x in sigs:
        if x["sig"] in ("BREAKDOWN", "BREAKOUT") and not show_all: continue
        if day and time.strftime("%Y%m%d", time.gmtime(x["t"])) != day: continue
        i = idx.get(x["t"]); m0 = x["mid"]; sgn = 1 if x["sig"] in ("DIP_SLOWING", "BREAKOUT") else -1
        fwd = {}
        if i is not None:
            for k, secs in (("1m", 60), ("5m", 300), ("15m", 900)):
                j = i + secs; fwd[k] = (mids[j][1] / m0 - 1) * 100 * sgn if j < len(mids) else None
            path = [m for _, m in mids[i + 1:i + 901]]
            fwd["mfe"] = max((m / m0 - 1) * 100 * sgn for m in path) if path else None
            fwd["mae"] = min((m / m0 - 1) * 100 * sgn for m in path) if path else None
            path30 = [m for _, m in mids[i + 1:i + 1801]]; atr = x.get("atr") or 0.0      # holding the side the stall would have sold (a long after POP_STALLING) for up to 30 min
            fwd["hold05"], fwd["hold15"] = hold_pnl(m0, path30, -sgn, 0.5 * atr), hold_pnl(m0, path30, -sgn, 1.5 * atr)
        rows.append((x, fwd))
    g = lambda v, w=6: f"{v:+{w}.2f}" if isinstance(v, (int, float)) and v is not None else " " * (w - 1) + "-"
    for x, fwd in ([] if quiet else rows):
        ts = time.strftime("%m-%d %H:%M:%S", time.gmtime(x["t"]))
        print(f"{ts} {x['sig']:<12} {x.get('src', '-'):<2} mid={x['mid']:.5g} D={x.get('D', 0):.2f} U={x.get('U', 0):.2f} v={x['v']:+.2f} a={x['a']:+.2f} "
              f"minv={x.get('minv', 0):+.2f} maxv={x.get('maxv', 0):+.2f} div={int(bool(x.get('cvd_div')))}/{int(bool(x.get('cvd_div_bear')))} "
              f"refill={x.get('bid_refill', 0):.2f}/{x.get('ask_refill', 0):.2f} imb={x.get('imb', 0):+.2f} bs10={x.get('bs10', 0):.2f} "
              f"dec={x.get('dec', 0):.2f} vol60={x.get('vol60', 0):.1f} vr={x.get('vr', 0):.1f} lw={x.get('lw', 0):.2f} uw={x.get('uw', 0):.2f} "
              f"roc3={x.get('roc3', 0):+.2f} sell30={x.get('sell30', 0):.1f} buy30={x.get('buy30', 0):.1f} "
              f"vp: dens={g(x.get('vp_dens'), 5)} va={x.get('vp_va', '-')} sup={g(x.get('vp_sup'), 5)} res={g(x.get('vp_res'), 5)} "
              f"rg: er={x.get('rg_er', 0):.2f} drift={x.get('rg_drift', 0):+.1f} up/dn={x.get('rg_up', 0)}/{x.get('rg_dn', 0)} "
              f"| fwd 1m={g(fwd.get('1m'))} 5m={g(fwd.get('5m'))} 15m={g(fwd.get('15m'))} mfe={g(fwd.get('mfe'))} mae={g(fwd.get('mae'))}")
    print(f"--- {len(mids)} seconds, {len(feat.candles)} candles, tick={feat.tick}, {len(rows)} signals shown, "
          f"{sum(1 for x in sigs if x['sig'] == 'BREAKDOWN')} BREAKDOWN, {sum(1 for x in sigs if x['sig'] == 'BREAKOUT')} BREAKOUT")
    summarize(rows, "all")
    for key in ("src",) + tuple(by): split_by(rows, key)
    return rows

if __name__ == "__main__":
    args = sys.argv[1:]; files, sym, sig, show_all, by, quiet, day = [], "TRUMPUSDT", {}, False, (), False, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--sym": sym = args[i + 1]; i += 2
        elif a == "--sig": k, v = args[i + 1].split("="); sig[k] = float(v); i += 2
        elif a == "--all": show_all = True; i += 1
        elif a == "--by": by = tuple(args[i + 1].split(",")); i += 2
        elif a == "--quiet": quiet = True; i += 1
        elif a == "--day": day = args[i + 1]; i += 2
        else: files.append(a); i += 1
    if not files: print(__doc__); sys.exit(0)
    run(files, sym, sig, show_all, by, quiet, day)
