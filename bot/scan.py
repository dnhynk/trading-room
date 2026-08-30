"""Symbol selector for 순환매: rank liquid USDT-M perpetuals by two-way opportunity at the engine's own scale and by a candle-level
replay of the engine itself; flag pump-and-dump shapes over several days — never a single crash day. Report only; bot/select.py switches.
  python -m bot.scan [--top 15] [--days 3] [--min-vol 50e6] [--json] [--sym TRUMPUSDT,ENAUSDT]
Universe: contracts with symbolStatus normal and 24h quote volume >= --min-vol (a gate, not a multiplier). Windows: --days windows of
24h of closed 1m candles (public REST history), most recent first. Per window:
  legs/h   zigzag legs of >= sig.dip_min_atr x ATR(1m) per hour       how often the engine's deceleration depth is offered
  leg%     median leg size (%)                                        what one cycle can harvest (must clear the fee floor)
  bounce   median fraction of a leg retraced within 30 min after it   two-way-ness: a one-way tape retraces little
  ER       |net| / path over the window                               drift dominance (one-way = high)
  concept  legs/h x leg% x bounce x (1 - ER)                          a monotone product, no fitted weights
  proxy    the engine replayed on the candles: bot.signal.candle_rule (the live 1m rule), the step ladder, gap_rebuy, the unit/core
           trim gates with relaxation, the retrace top, the 15m/1H structural stop with the money cap, 3 stops/day -> halt; fills at
           the close, maker fee on adds, taker on trims; no v rule, no de-risk. Net pnl in % of one unit's notional per window.
Ranking value = the median over windows (a typical day, not yesterday). Symbols are ranked by proxy; concept is shown and is
bot/select.py's second condition (a less two-way symbol never wins on the proxy alone).
Flags (listed apart, never ranked): tick% > 0.05, spread > 10 bp, |funding| >= 0.1% per 8h, pump shape on >= 2 windows (the hour
carrying the most volume >= 35% of the window while moving the price >= 4%), a pump (an UP move of >= +25% in a window or >= +40%
over the windows, however two-way the swings on the way — 작전 코인 is 순환매 지옥, user 2026-08-29; PROMUSDT +54%/day slipped
through a bounce-based rule on 2026-08-30), ER >= 0.35 in the latest window (one-way right now; re-judged next scan). A crash day
is NOT a flag: the coin that fell 10% in an hour is often the best two-way tape afterwards (user 2026-08-29); its losses are the
stop's business.
Also per symbol: the 1H structure side (`side`, the side a switch starts on), funding, OI. --json writes logs/scan.json for select."""
import json, os, statistics, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import Bitget, PRODUCT
from bot.signal import (STRAT, SIG, zigzag_pivots, wilder_atr, structure_side, pivot_levels, structural_level, candle_features, candle_rule,
                        apply_fill, pos_stats)
from bot.ws import load_params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN = 1440; MAKER, TAKER, SLIP = 0.0002, 0.0006, 0.0005
TRIM_FEE = 0.0004      # live trims are maker first, taker after 10 s: about half and half on 2026-08-29, so the average fee

def arg(flag, default):
    return type(default)(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default

def fetch_1m(b, sym, minutes):
    """The last `minutes` closed 1m candles, oldest first (public history, 200 per call)."""
    rows, now = {}, int(time.time() * 1000); cursor = now
    while len(rows) < minutes:
        batch = [r for r in b.history_candles(sym, "1m", cursor, 200) if r["ts"] + 60_000 <= cursor]
        if not batch: break
        rows.update({r["ts"]: r for r in batch}); cursor = batch[0]["ts"]; time.sleep(0.06)
    return [rows[k] for k in sorted(rows) if k + 60_000 <= now][-minutes:]

def bars_1h(c15):
    h1 = {}
    for v in c15:
        r = h1.setdefault(v["ts"] // 3_600_000, dict(ts=v["ts"] // 3_600_000 * 3_600_000, o=v["o"], h=v["h"], l=v["l"], c=v["c"]))
        r["h"], r["l"], r["c"] = max(r["h"], v["h"]), min(r["l"], v["l"]), v["c"]
    return list(h1.values())

def two_way(c, k):
    """Concept metrics of one window of closed 1m candles at the engine's scale (legs >= k x ATR)."""
    cl = [x["c"] for x in c]; n = len(cl); hrs = n / 60; px = cl[-1]; atr = wilder_atr(c) or 0.0
    th = k * atr / px if px and atr else 0.0
    if th <= 0 or n < 120: return None
    piv = zigzag_pivots(cl, th); sizes, bounces = [], []
    for (i0, k0), (i1, k1) in zip(piv, piv[1:]):
        size = abs(cl[i1] - cl[i0]); s = 1 if k1 == "L" else -1; fwd = cl[i1 + 1:i1 + 31]
        sizes.append(size / cl[i0] * 100)
        if fwd: bounces.append(min(max(max(s * (x - cl[i1]) for x in fwd) / (size or 1e-9), 0.0), 1.0))
    path = sum(abs(a - b) for a, b in zip(cl, cl[1:])); net = cl[-1] - cl[0]; er = abs(net) / path if path else 0.0
    er4 = max((abs(cl[min(i + 239, n - 1)] - cl[i]) / (sum(abs(a - b) for a, b in zip(cl[i:i + 240], cl[i + 1:i + 240])) or 1e-9)
               for i in range(0, n - 60, 240)), default=er)
    vols = [x["v"] for x in c]; tot = sum(vols) or 1e-9
    top_share, top_move = max((sum(vols[i:i + 60]) / tot, abs(cl[min(i + 59, n - 1)] / cl[i] - 1) * 100) for i in range(0, n, 60))
    leg = statistics.median(sizes) if sizes else 0.0; bounce = statistics.median(bounces) if bounces else 0.0
    return dict(legs_h=len(sizes) / hrs, leg=leg, bounce=bounce, er=er, er4=er4, atr_pct=atr / px * 100, net=net / cl[0] * 100,
                pump=top_share >= 0.35 and top_move >= 4, concept=len(sizes) / hrs * leg * bounce * (1 - er))

def proxy(c, c15, sp, sg, bounds):
    """The engine replayed on closed 1m candles (module docstring). One unit = 1.0 of notional at its entry (contracts = 1/price), so
    pnl comes out in fractions of a unit (x100 = % of a unit). Returns {window index: dict(pnl, cycles, adds, stops, dd, inmkt)} for
    bounds = [(start_ms, end_ms), ...] (most recent first)."""
    p = {**STRAT, **sp}; q = {**SIG, **sg}
    cap = p["cap_frac"] / p["unit_frac"] if p.get("unit_frac") and p.get("cap_frac") else 0.2       # the money cap as a fraction of one unit's notional
    pos = dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None)
    stop = struct = peak = None; cool = 0; day = halt_day = None; stops_day = fail = 0; last_lot = None; realized = 0.0
    vr_hist, bars15, htf, atr15, last15 = [], {}, [], None, None; seeded = {v["ts"]: v for v in c15}
    res, eq_peak = {}, 0.0
    def window(t):
        for j, (a, z) in enumerate(bounds):
            if a <= t < z: return j
        return None
    for i, x in enumerate(c):
        t, px = x["ts"], x["c"]; j = window(t)
        if j is not None and j not in res: res[j] = dict(pnl=0.0, cycles=0, adds=0, stops=0, dd=0.0, inmkt=0, n=0); eq_peak = realized + sum((px - l[1]) * l[0] for l in pos["lots"])
        d = t // 86_400_000
        if d != day: day, stops_day, halt_day = d, 0, None
        k15 = t // 900_000; b15 = bars15.setdefault(k15, dict(ts=k15 * 900_000, o=x["o"], h=x["h"], l=x["l"], c=x["c"]))
        b15["h"], b15["l"], b15["c"] = max(b15["h"], x["h"]), min(b15["l"], x["l"]), x["c"]
        if k15 != last15:                                   # the 15m/1H structure the stop rests on, refreshed when a 15m bar closes
            last15 = k15
            closed = sorted({**seeded, **{v["ts"]: v for kk, v in bars15.items() if kk < k15}}.values(), key=lambda v: v["ts"])[-400:]
            atr15 = wilder_atr(closed); b1h = bars_1h(closed)[:-1]
            l15, _ = pivot_levels(closed, atr15); l1h, _ = pivot_levels(b1h, wilder_atr(b1h) if len(b1h) >= 20 else None)
            htf = sorted(set(l15 + l1h))
        atr = wilder_atr(c[max(0, i - 99):i + 1])
        if i < 30 or not atr: continue
        cl = c[i - 30:i + 1]; cf = candle_features(cl); vr_hist = (vr_hist + [cf["vr"]])[-3:]
        sigs = candle_rule(cl, cf, vr_hist, q) if q["c1_on"] else []
        qty, avg = pos_stats(pos)
        if qty and stop is not None and x["l"] <= stop:      # the stop on the candle's low
            fill = stop * (1 - SLIP); pnl = apply_fill(pos, 1, False, qty, fill, fee=qty * fill * TAKER); realized += pnl
            stops_day += 1; cool = t + p["stop_cooldown_s"] * 1000; stop = struct = peak = None
            if j is not None: res[j]["pnl"] += pnl; res[j]["stops"] += 1
            if stops_day >= p["max_stops_day"]: halt_day = d
            qty, avg = pos_stats(pos)
        step = max(p["step_add_pct"], p["step_add_atr"] * atr15 / px * 100) if atr15 and p["step_add_atr"] > 0 else p["step_add_pct"]
        if qty:                                              # trims: the LIFO lot on a stall or the retrace top, everything at full_exit
            lq, lpx, lid = pos["lots"][-1]; is_core = len(pos["lots"]) <= int(p["core_units"]); ref = avg if is_core else lpx
            if lid != last_lot: fail, last_lot = 0, lid
            g_norm = p["pop_min_pct"] if is_core else p["unit_min_pct"]; floor = 0.0 if is_core else p["gate_floor_unit_pct"]
            g = floor + (g_norm - floor) * (1 - p["gate_relax"]) ** fail
            dev, dev_lot = (px / avg - 1) * 100, (px / ref - 1) * 100
            peak = x["h"] if peak is None else max(peak, x["h"])
            retrace = p["trim_retrace_atr"] > 0 and (peak / ref - 1) * 100 >= g and peak - px >= p["trim_retrace_atr"] * atr
            if "POP_STALLING" in sigs and dev_lot < g and p["gate_relax"] > 0: fail += 1
            if ("POP_STALLING" in sigs or retrace) and dev_lot >= g:
                sell = qty if dev >= p["full_exit_pct"] else lq; n0 = len(pos["lots"])
                pnl = apply_fill(pos, 1, False, sell, px, fee=sell * px * TRIM_FEE); realized += pnl; peak = None
                if j is not None: res[j]["pnl"] += pnl; res[j]["cycles"] += n0 - len(pos["lots"])
                qty, avg = pos_stats(pos)
                if not qty: stop = struct = None
        if "DIP_SLOWING" in sigs and t >= cool and halt_day is None and len(pos["lots"]) < p["max_units"]:   # add on a deceleration
            ok = (not qty or (pos["last"] == "buy" and px <= pos["last_buy_px"] * (1 - step / 100))
                  or (pos["last"] == "trim" and px <= pos["last_trim_px"] * (1 - p["gap_rebuy_pct"] / 100)))
            u = 1.0 / px
            if ok and qty and stop is not None and ((avg * qty + px * u) / (qty + u) - stop) * (qty + u) > cap: ok = False   # the cap is an add budget
            if ok:
                pnl = apply_fill(pos, 1, True, u, px, oid=f"b{i}", fee=u * px * MAKER); realized += pnl; peak = None
                if j is not None: res[j]["pnl"] += pnl; res[j]["adds"] += 1
                qty, avg = pos_stats(pos)
        if qty:                                              # the stop: structure leaving ladder room - buffer, else the money cap; never loosened; trails
            cap_px = avg - cap / qty
            lvl = structural_level(dict(htf_lows=htf, atr15=atr15), 1, pos["last_buy_px"] or px, p, len(pos["lots"])) if p["stop_structural_on"] else None
            if lvl and atr15:
                cand = lvl - p["stop_buffer_atr"] * atr15
                if struct is None and stop is None: struct = cand if cand < px else None
                elif struct is not None and p["stop_trail"] and px > cand > struct: struct = cand
            new = cap_px if struct is None else max(struct, cap_px)
            if stop is None and new >= px: new = cap_px if cap_px < px else None
            if new is None:                                  # no valid stop level: the position ends now
                pnl = apply_fill(pos, 1, False, qty, px, fee=qty * px * TAKER); realized += pnl; stops_day += 1; stop = struct = peak = None
                if j is not None: res[j]["pnl"] += pnl; res[j]["stops"] += 1
            else: stop = new if stop is None else max(stop, new)
        else: stop = struct = None
        qty, avg = pos_stats(pos)
        if j is not None:
            eq = realized + sum((px - l[1]) * l[0] for l in pos["lots"]); eq_peak = max(eq_peak, eq)
            res[j]["dd"] = max(res[j]["dd"], eq_peak - eq); res[j]["n"] += 1; res[j]["inmkt"] += 1 if qty else 0
    for r in res.values():
        r["pnl"] = round(r["pnl"] * 100, 3); r["dd"] = round(r["dd"] * 100, 3); r["inmkt"] = round(r["inmkt"] / max(r["n"], 1), 3)
    return res

def flags_of(x, wins, net_total):
    f = []
    if x["tick_pct"] > 0.05: f.append(f"tick{x['tick_pct']:.2f}%")
    if x["spread_bp"] > 10: f.append(f"spr{x['spread_bp']:.0f}bp")
    if abs(x["fund"]) >= 0.1: f.append(f"fund{x['fund']:+.2f}%")
    if sum(1 for w in wins if w and w["pump"]) >= 2: f.append("pump")
    ups = [w["net"] for w in wins if w]
    if (ups and max(ups) >= 25) or net_total >= 40: f.append(f"pump{max(ups + [net_total]):+.0f}%")   # a pump is an UP move (crashes are cycle heaven): +25% in a day or +40% over the windows, however two-way it swings on the way
    if wins and wins[0] and wins[0]["er"] >= 0.35: f.append(f"ER{wins[0]['er']:.2f}")
    return f

def rank(min_vol=5e7, days=3, syms=None, exclude=(), log=print):
    """Scan the universe; returns rows sorted: unflagged by proxy (desc) first, then flagged by volume. Public REST only."""
    b = Bitget("", "", ""); p = load_params() or {}; sp = {**STRAT, **(p.get("strat") or {})}; sg = {**SIG, **(p.get("sig") or {})}
    contracts = {c["symbol"]: c for c in b.get("/api/v2/mix/market/contracts", auth=False, productType=PRODUCT) if c.get("symbolStatus") == "normal"}
    tickers = b.get("/api/v2/mix/market/tickers", auth=False, productType=PRODUCT)
    cand = []
    for t in tickers:
        s = t["symbol"]; c = contracts.get(s)
        if not c or not s.endswith("USDT") or s in exclude or (syms and s not in syms): continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < min_vol and not syms: continue
        px = float(t["lastPr"]); bid, ask = float(t.get("bidPr") or px), float(t.get("askPr") or px)
        tick = float(c["priceEndStep"]) * 10 ** -int(c["pricePlace"])
        cand.append(dict(symbol=s, px=px, qv=qv, chg=float(t.get("change24h") or 0) * 100, fund=float(t.get("fundingRate") or 0) * 100,
                         oi=float(t.get("holdingAmount") or 0) * px, tick_pct=tick / px * 100, spread_bp=(ask - bid) / px * 1e4))
    cand.sort(key=lambda x: -x["qv"])
    log(f"{len(cand)} symbols (24h volume >= {min_vol:.0f}); pulling {days} days of 1m candles ...")
    rows = []
    for x in cand:
        try:
            c1 = fetch_1m(b, x["symbol"], days * WIN)
            c15 = b.history_candles(x["symbol"], "15m", c1[0]["ts"], 200) if c1 else []
        except Exception as e: log(f"  {x['symbol']}: candles failed {e}"); continue
        nw = len(c1) // WIN
        if nw < 1: log(f"  {x['symbol']}: only {len(c1)} candles"); continue
        n = len(c1); bounds = [(c1[n - WIN * (j + 1)]["ts"], c1[n - WIN * j]["ts"] if j else c1[-1]["ts"] + 60_000) for j in range(nw)]
        wins = [two_way(c1[n - WIN * (j + 1):n - WIN * j], sg["dip_min_atr"]) for j in range(nw)]
        pr = proxy(c1, c15, sp, sg, bounds); prs = [pr.get(j) for j in range(nw)]
        med = lambda xs: statistics.median(xs) if xs else 0.0
        x.update(wins=wins, proxies=prs, concept=med([w["concept"] for w in wins if w]), proxy=med([r["pnl"] for r in prs if r]),
                 cyc_d=med([r["cycles"] for r in prs if r]), stops_d=med([r["stops"] for r in prs if r]), dd=max([r["dd"] for r in prs if r] or [0.0]),
                 legs_h=med([w["legs_h"] for w in wins if w]), leg=med([w["leg"] for w in wins if w]), bounce=med([w["bounce"] for w in wins if w]),
                 er=wins[0]["er"] if wins and wins[0] else 0.0, atr_pct=wins[0]["atr_pct"] if wins and wins[0] else 0.0)
        bars15 = {}
        for v in c1:
            k = v["ts"] // 900_000; r = bars15.setdefault(k, dict(ts=k * 900_000, o=v["o"], h=v["h"], l=v["l"], c=v["c"]))
            r["h"], r["l"], r["c"] = max(r["h"], v["h"]), min(r["l"], v["l"]), v["c"]
        s15 = sorted({**{v["ts"]: v for v in c15}, **bars15}.values(), key=lambda v: v["ts"])[:-1]; b1h = bars_1h(s15)[:-1]
        x["side"] = (structure_side(b1h, wilder_atr(b1h)) if len(b1h) >= 20 else structure_side(s15, wilder_atr(s15))) or "-"
        x["flags"] = flags_of(x, wins, (c1[-1]["c"] / c1[0]["c"] - 1) * 100)
        rows.append(x)
    ok = sorted([r for r in rows if not r["flags"]], key=lambda r: -r["proxy"])
    bad = sorted([r for r in rows if r["flags"]], key=lambda r: -r["qv"])
    return ok + bad

def table(rows, top):
    hdr = f"{'symbol':<12}{'proxy%/d':>9}{'concept':>8}{'legs/h':>7}{'leg%':>6}{'bounce':>7}{'ER':>6}{'atr%':>6}{'cyc/d':>6}{'stop/d':>7}{'dd%':>6}{'vol24h':>8}{'fund%':>7}  side  flags"
    print(hdr)
    for r in rows[:top] + [r for r in rows if r["flags"]][:top]:
        print(f"{r['symbol']:<12}{r['proxy']:9.2f}{r['concept']:8.2f}{r['legs_h']:7.2f}{r['leg']:6.2f}{r['bounce']:7.2f}{r['er']:6.2f}{r['atr_pct']:6.2f}"
              f"{r['cyc_d']:6.1f}{r['stops_d']:7.1f}{r['dd']:6.2f}{r['qv'] / 1e6:7.0f}M{r['fund']:+7.3f}  {r['side']:<5} {' '.join(r['flags'])}")

def main():
    top, days, min_vol = arg("--top", 15), arg("--days", 3), arg("--min-vol", 50e6)
    syms = arg("--sym", "").split(",") if "--sym" in sys.argv else None
    t0 = time.time(); rows = rank(min_vol, days, syms)
    table(rows, top); print(f"--- {len(rows)} symbols, {days} windows of 24h, {time.time() - t0:.0f}s")
    if "--json" in sys.argv:
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "scan.json"), "w", encoding="utf-8") as f: json.dump(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), days=days, rows=rows), f)

if __name__ == "__main__":
    main()
