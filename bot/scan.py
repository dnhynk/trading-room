"""Symbol selector for 순환매: score liquid USDT-M perpetuals in the engine's own money — expected % of one unit's notional per hour,
net of the toll — and disqualify the ones whose toll eats the edge. Report only; bot/select.py owns the basket.
  python -m bot.scan [--top 15] [--days 3] [--min-vol 50e6] [--n-books 4] [--json] [--sym TRUMPUSDT,ENAUSDT]
Universe: contracts with symbolStatus normal and 24h quote volume >= --min-vol. Windows: --days windows of 24h of closed 1m candles
(public REST history), most recent first. The value of every column is the median over windows (a typical day, not yesterday).

THE SCORE.  edge%/h = trials/h x (mean outcome - fee% - imp%).  Every term is % of one unit's notional, so the toll and the
opportunity subtract in the same unit. What the old `concept` got wrong (measured 2026-09-01) is that it had no cost term at all and
ranked on the one quantity our P&L barely contains:
  - what we capture per cycle is set by OUR trim gates, not by the tape's leg size. Median gross per lot cycle: TRUMPUSDT 0.233%
    over 209 cycles (0.260 / 0.141 / 0.250 / 0.212 by day) vs ZECUSDT 0.295% over 15 — while their leg% differed by 35%.
  - what varies is the win rate and the loss tail. Across TRUMP's volume collapse (53M -> 25M at 09-01 09:55) mean gross win fell
    0.393 -> 0.284, mean gross loss grew 0.511 -> 0.718, cycles/h fell 4.21 -> 2.15 and realized edge went +0.100 -> -0.159 %/cycle,
    while `concept` stayed #1. ZEC ran in the same hours at +0.115 against TRUMP's -0.073, so it was the symbol and not the market
    (n = 15 vs 14 — the right kind of comparison, not yet a large one).
  trials/h, outcome  a triple-barrier count (`trials`): every bot.signal.candle_rule firing opens a trial resolved by +win_pct,
                     -loss_pct or the hold horizon. win_pct/loss_pct are the engine's own measured geometry (bot.cycles summary),
                     so the tape is asked what it offers without simulating a single fill.
  fee%               measured round-trip fee per cycle, 0.048%.
  imp%               sigma_daily x sqrt(unit notional / 24h volume) at the REAL per-book unit (unit_frac x equity / n_books / sides).
                     Break-even win rate `need` = (loss + fee + imp) / (win + loss) = 0.665 + imp / 0.929 at the measured
                     geometry: every 0.009% of imp% costs one point of win rate.
Reported, never ranked: `concept` = legs/h x leg% x bounce x (1 - ER) and its parts, kept so the two rankings stay comparable on live
results. Its factors carry little cross-section (2026-09-01, 19 symbols: 1-ER in [0.95, 1.00], bounce in [0.73, 1.00], leg% ~ 5.3 x
atr%), so it reduces to frequency x volatility — and volatility is also what raises imp%.

WHAT THE SCORE IS NOT (measured 2026-09-01, and the reason it only ranks): `trials` resolves at fixed barriers, the engine does
not. `gate_relax` walks a lot's trim gate down on every stall that misses it, so near misses are booked as small wins — which is
exactly why live cycles are 70% winners at a mean gross win of 0.360 against a mean gross loss of 0.569. The estimator cannot
reproduce that: on TRUMPUSDT's own three days (09-01 / 08-31 / 08-30) its p_up reads 0.59 / 0.62 / 0.59 while the live win rate on
those days was 0.65 / 0.72 / 0.64 — the level is low and the day-to-day spread is a third of the live one. So the score orders the
eligible set and nothing more.
Its one gate is soft: `entry` (one trial pays the toll, out% > fee% + imp%) may block an add and never forces an exit, because a
pessimistic estimator is allowed to make us careful and not to make us sell.

HARD DISQUALIFICATION (flags; never a candidate, and a flagged holding is wound down — bot/select.py): imp% >= fee% (our own
footprint costs more than the exchange), 24h volume < --min-vol (a heuristic backstop, 50M: the derived gate is imp% >= fee% and at
this equity it does not bind — our unit is ~5 ppm of ADV where that gate needs ~50), tick% > 0.05, spread > 10 bp, |funding| >= 0.1%
per 8h, pump shape on >= 2 windows (the hour carrying the most volume >= 35% of the window while moving the price >= 4%),
a pump (an UP move of >= +25% in a window or
>= +40% over the windows, however two-way the swings on the way — 작전 코인 is 순환매 지옥, user 2026-08-30; PROMUSDT +54%/day
slipped through a bounce-based rule on 2026-08-30), ER >= 0.35 in the latest window (one-way right now; re-judged next scan). A
crash day is NOT a flag: the coin that fell 10% in an hour is often the best two-way tape afterwards (user 2026-08-29); its losses
are the stop's business. `rank(always=...)` measures a symbol below the volume gate but never exempts it — measurement is not
eligibility (2026-09-01: the incumbent was exempted from the gate and traded 13 more hours at -0.159%/cycle).
Also per symbol: the 1H structure side (`side`, the side a book starts on), funding, OI. --json writes logs/scan.json for select."""
import json, os, statistics, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import Bitget, PRODUCT
from bot.signal import STRAT, SIG, zigzag_pivots, wilder_atr, structure_side, candle_features, candle_rule
from bot.ws import load_params, load_states, portfolio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN = 1440
# The engine's own geometry, measured from the live ledger (the bot.cycles summary prints all three; re-read them after any change
# to the trim gates or the stop, and whenever a month of cycles has accumulated). They are the barriers of `trials` and its payoffs.
# 2026-09-01, 224 completed lot cycles (TRUMPUSDT 209 + ZECUSDT 15): mean GROSS win 0.360%, mean gross loss 0.569%, round-trip fee
# 0.048% of notional -> break-even win rate (loss + fee) / (win + loss) = 0.665, live 0.701. Gross, not net: the fee is subtracted
# once, in the score. Pooling across symbols assumes this geometry is symbol-invariant — that is the hypothesis these constants
# encode, and TRUMPUSDT is 93% of the sample, so it is barely tested (NEXT 6).
EDGE = dict(win_pct=0.360, loss_pct=0.569, fee_pct=0.048, hold_min=120)   # hold_min: live median hold is 13 min, 120 covers the tail (measured timeout share 0-8%)
MIN_TRIALS_H = 1.3   # fewer pauses per hour than this cannot accumulate 300 campaigns in 30 days: unjudgeable on live results = ineligible (select.min_trials_h)

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

def trials(c, sg, win, loss, hold_min, bounds):
    """Triple-barrier count of what the tape offers this engine, per window of closed 1m candles. Every bot.signal.candle_rule firing
    opens a trial at that close — DIP_SLOWING a long, POP_STALLING a short (쌍검 runs both) — resolved by whichever comes first: the
    win barrier (+win% from entry, on the candle high for a long), the loss barrier (-loss%, on the low), or the hold horizon, where
    the outcome is the return at expiry clipped into [-loss, +win]. Both barriers inside one candle counts adverse: the order inside a
    bar is not observable and the conservative reading is the one that does not invent edge.
    win/loss are the engine's own measured per-cycle geometry, so this asks the tape a question about OUR round trip without
    simulating a fill — the fill model is what made the old candle proxy invert the ranking 4 times out of 4 (RULES 도구 절), and no
    coarser statistic can be built out of it: `bounce` saturates (0.73..1.00 over 19 symbols) because it asks whether the tape
    retraced at all, not whether it retraced +win before it went -loss.
    A trial needs a full `hold_min` of candles ahead of it, so `bars` counts only the bars that could have opened one — the rate is
    n / (bars / 60) and stays honest at the edge of the tape.
    Returns {window index: dict(n, up, dn, to, out, bars)} for bounds = [(start_ms, end_ms), ...] (most recent first)."""
    q = {**SIG, **sg}; n = len(c); vr_hist = []; res = {}
    def window(t):
        for j, (a, z) in enumerate(bounds):
            if a <= t < z: return j
        return None
    for i, x in enumerate(c):
        if i < 30: continue
        cl = c[i - 30:i + 1]; cf = candle_features(cl); vr_hist = (vr_hist + [cf["vr"]])[-3:]
        j = window(x["ts"])
        if j is None or i + hold_min >= n: continue
        r = res.setdefault(j, dict(n=0, up=0, dn=0, to=0, out=0.0, bars=0, n_h=[0] * 24, bars_h=[0] * 24)); r["bars"] += 1
        hod = (x["ts"] // 3_600_000) % 24; r["bars_h"][hod] += 1                       # by UTC hour of day: tokenised stocks / metals trade in sessions (NEXT 8)
        sigs = candle_rule(cl, cf, vr_hist, q) if q["c1_on"] else []
        for s in ([1] if "DIP_SLOWING" in sigs else []) + ([-1] if "POP_STALLING" in sigs else []):
            p0 = x["c"]; up = p0 * (1 + s * win / 100); dn = p0 * (1 - s * loss / 100); out = None
            for y in c[i + 1:i + hold_min + 1]:
                if (y["l"] <= dn) if s > 0 else (y["h"] >= dn): out = -loss; break        # adverse first when a bar holds both
                if (y["h"] >= up) if s > 0 else (y["l"] <= up): out = win; break
            if out is None: out = max(-loss, min(win, s * (c[i + hold_min]["c"] / p0 - 1) * 100)); r["to"] += 1
            else: r["up" if out > 0 else "dn"] += 1
            r["n"] += 1; r["out"] += out; r["n_h"][hod] += 1
    return res

def edge_of(t, fee, imp):
    """One window's score: % of one unit's notional per hour, net of the toll. No trial is no edge, not a missing number."""
    if not t or not t["n"] or not t["bars"]: return 0.0
    return t["n"] / (t["bars"] / 60) * (t["out"] / t["n"] - fee - imp)

def corr(a, b):
    """Pearson correlation of two equal-length return series (0.0 when either is flat)."""
    n = len(a)
    if n < 10 or n != len(b): return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a); vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0: return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5

def clusters(rows, th=0.5):
    """Driver clusters from the latest window's 15-minute returns (`_r15`: {bar ts: return}): rows in volume order, each joins the first
    cluster whose SEED it correlates with at >= th, else seeds a new one; the cluster id is its seed's symbol. Crypto majors correlate
    0.6-0.8 with each other and ~0 with tokenised stocks and metals, so this splits the universe by what drives it without any fitted
    model — a basket of four crypto books is one bet on the crash day (NEXT 8: 상관). Sets r["cluster"] and drops the series."""
    seeds = []
    for r in sorted(rows, key=lambda r: -r.get("qv", 0.0)):
        mine = r.pop("_r15", None) or {}
        for sym, ser in seeds:
            keys = sorted(set(mine) & set(ser))
            if keys and corr([mine[k] for k in keys], [ser[k] for k in keys]) >= th: r["cluster"] = sym; break
        else:
            r["cluster"] = r["symbol"]; seeds.append((r["symbol"], mine))
    return rows

def flags_of(x, wins, net_total, min_vol=0.0, fee=0.0, min_trials_h=0.0):
    """Hard disqualification, never ranking: a flagged symbol is not a candidate and a flagged holding is wound down (bot/select.py).
    Only facts that hold whatever the score says go here — our footprint, the contract, the tape's shape. The score's own verdict
    (`entry`) is a soft gate that blocks an add and never forces an exit: the estimator is measured to be pessimistic against this
    engine, so it is allowed to make us more careful and never to make us sell."""
    f = []
    if fee and x.get("impact", 0.0) >= fee: f.append(f"imp{x['impact']:.4f}%")                   # our own footprint costs more than the exchange
    if min_vol and x.get("qv", 0.0) < min_vol: f.append(f"vol{x['qv'] / 1e6:.0f}M")              # backstop under the uncalibrated square-root law
    if x["tick_pct"] > 0.05: f.append(f"tick{x['tick_pct']:.2f}%")
    if x["spread_bp"] > 10: f.append(f"spr{x['spread_bp']:.0f}bp")
    if abs(x["fund"]) >= 0.1: f.append(f"fund{x['fund']:+.2f}%")
    if sum(1 for w in wins if w and w["pump"]) >= 2: f.append("pump")
    ups = [w["net"] for w in wins if w]
    if (ups and max(ups) >= 25) or net_total >= 40: f.append(f"pump{max(ups + [net_total]):+.0f}%")   # a pump is an UP move (crashes are cycle heaven): +25% in a day or +40% over the windows, however two-way it swings on the way
    if wins and wins[0] and wins[0]["er"] >= 0.35: f.append(f"ER{wins[0]['er']:.2f}")
    if min_trials_h and x.get("trials_h") is not None and x["trials_h"] < min_trials_h: f.append(f"slow{x['trials_h']:.1f}/h")   # too few pauses to ever be judged on live results (300 campaigns in 30 days needs ~1.3/h): unjudgeable is ineligible
    return f

def rank(min_vol=5e7, days=3, syms=None, exclude=(), log=print, always=(), equity=None, n_books=1, edge=None, min_trials_h=0.0, corr_th=0.5):
    """Scan the universe; returns rows sorted: unflagged by edge (desc) first, then flagged by volume. Public REST only.
    `always` = symbols measured even when they fail the volume gate (bot/select.py passes the held books: a basket verdict needs
    numbers for the symbols being traded, and absence is not evidence). They are measured, never exempted — the volume flag is
    raised all the same, so an `always` symbol under the gate is disqualified like any other. The old code exempted the incumbent
    from the gate instead, and it stayed in the book for 12 more hours at -0.140%/cycle (2026-09-01).
    `n_books` = the target basket size: imp% is charged against the real per-book unit, unit_frac x equity / n_books / sides.
    `edge` overrides the measured engine geometry in EDGE (win_pct, loss_pct, fee_pct, hold_min)."""
    e = {**EDGE, **(edge or {})}
    b = Bitget("", "", ""); p = load_params() or {}; sp = {**STRAT, **(p.get("strat") or {})}; sg = {**SIG, **(p.get("sig") or {})}
    n_sides = max(len(sp.get("sides") or [sp.get("side")]), 1)                 # budgets are split per book (signal.SPLIT_KEYS)
    n_books = max(int(n_books or 1), 1)
    unit_usdt = sp["unit_frac"] * equity / n_books / n_sides if sp.get("unit_frac") and equity else 0.0
    contracts = {c["symbol"]: c for c in b.get("/api/v2/mix/market/contracts", auth=False, productType=PRODUCT) if c.get("symbolStatus") == "normal"}
    tickers = b.get("/api/v2/mix/market/tickers", auth=False, productType=PRODUCT)
    cand = []
    for t in tickers:
        s = t["symbol"]; c = contracts.get(s)
        if not c or not s.endswith("USDT") or s in exclude or (syms and s not in syms): continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < min_vol and not syms and s not in always: continue
        px = float(t["lastPr"]); bid, ask = float(t.get("bidPr") or px), float(t.get("askPr") or px)
        tick = float(c["priceEndStep"]) * 10 ** -int(c["pricePlace"])
        cand.append(dict(symbol=s, px=px, qv=qv, chg=float(t.get("change24h") or 0) * 100, fund=float(t.get("fundingRate") or 0) * 100,
                         oi=float(t.get("holdingAmount") or 0) * px, tick_pct=tick / px * 100, spread_bp=(ask - bid) / px * 1e4))
    cand.sort(key=lambda x: -x["qv"])
    below = [x["symbol"] for x in cand if x["qv"] < min_vol]
    log(f"{len(cand)} symbols (24h volume >= {min_vol:.0f}{'; under the gate, measured but disqualified: ' + ','.join(below) if below else ''}); "
        f"pulling {days} days of 1m candles ...")
    rows = []
    for x in cand:
        try:
            c1 = fetch_1m(b, x["symbol"], days * WIN)
            c15 = b.history_candles(x["symbol"], "15m", c1[0]["ts"], 200) if c1 else []
        except Exception as ex: log(f"  {x['symbol']}: candles failed {ex}"); continue
        nw = len(c1) // WIN
        if nw < 1: log(f"  {x['symbol']}: only {len(c1)} candles"); continue
        n = len(c1); bounds = [(c1[n - WIN * (j + 1)]["ts"], c1[n - WIN * j]["ts"] if j else c1[-1]["ts"] + 60_000) for j in range(nw)]
        wins = [two_way(c1[n - WIN * (j + 1):n - WIN * j], sg["dip_min_atr"]) for j in range(nw)]
        med = lambda xs: statistics.median(xs) if xs else 0.0
        x.update(wins=wins, concept=med([w["concept"] for w in wins if w]),
                 legs_h=med([w["legs_h"] for w in wins if w]), leg=med([w["leg"] for w in wins if w]), bounce=med([w["bounce"] for w in wins if w]),
                 er=wins[0]["er"] if wins and wins[0] else 0.0, atr_pct=wins[0]["atr_pct"] if wins and wins[0] else 0.0)
        # Our own footprint: square-root impact sigma_daily x sqrt(unit notional / 24h volume), no fitted constant. It is subtracted
        # from the score in the same unit as the fee, and >= the fee disqualifies. The law is written for institutional participation
        # and our unit is ~20 ppm of ADV, so this reads as an upper bound; the tick backtest is what can calibrate it (NEXT 6).
        rr = [a["c"] / q["c"] - 1 for q, a in zip(c1[-WIN:], c1[-WIN + 1:]) if q["c"]]
        sig_d = statistics.pstdev(rr) * (1440 ** 0.5) * 100 if len(rr) > 60 else 0.0
        x["sigma_d"] = sig_d                                                                  # daily sigma (%): the impact term, the optional sigma-normalised share (select), the pair table
        last = c1[-WIN:]; x["_r15"] = {last[i]["ts"] // 900_000: last[i + 14]["c"] / last[i]["c"] - 1 for i in range(0, len(last) - 14, 15) if last[i]["c"]}   # 15-min returns for the driver clusters
        x["unit_usdt"] = un = unit_usdt or sp["unit_qty"] * x["px"] / n_books / n_sides
        x["impact"] = sig_d * (un / x["qv"]) ** 0.5 if x["qv"] else 0.0
        tr = trials(c1, sg, e["win_pct"], e["loss_pct"], int(e["hold_min"]), bounds); got = [t for t in (tr.get(j) for j in range(nw)) if t]
        eds = [edge_of(t, e["fee_pct"], x["impact"]) for t in got]
        tot_n = sum(t["n"] for t in got); res = sum(t["up"] + t["dn"] for t in got)
        n_h = [sum(t["n_h"][h] for t in got) for h in range(24)]; b_h = [sum(t["bars_h"][h] for t in got) for h in range(24)]
        x["tr_by_h"] = [round(n / (b / 60), 2) if b else 0.0 for n, b in zip(n_h, b_h)]      # trials per hour by UTC hour of day (session shape, NEXT 8)
        x.update(edge=med(eds), edge_sd=statistics.pstdev(eds) if len(eds) > 1 else 0.0, edges=[round(v, 4) for v in eds],
                 trials_h=med([t["n"] / (t["bars"] / 60) for t in got if t["bars"]]), n_trials=tot_n,
                 p_up=sum(t["up"] for t in got) / res if res else 0.0, timeout=sum(t["to"] for t in got) / tot_n if tot_n else 0.0,
                 out=sum(t["out"] for t in got) / tot_n if tot_n else 0.0,
                 need=(e["loss_pct"] + e["fee_pct"] + x["impact"]) / (e["win_pct"] + e["loss_pct"]))     # break-even win rate
        x["entry"] = x["out"] - e["fee_pct"] - x["impact"] > 0     # soft: one trial has to pay the toll before we open a book on it
        bars15 = {}
        for v in c1:
            k = v["ts"] // 900_000; r = bars15.setdefault(k, dict(ts=k * 900_000, o=v["o"], h=v["h"], l=v["l"], c=v["c"]))
            r["h"], r["l"], r["c"] = max(r["h"], v["h"]), min(r["l"], v["l"]), v["c"]
        s15 = sorted({**{v["ts"]: v for v in c15}, **bars15}.values(), key=lambda v: v["ts"])[:-1]; b1h = bars_1h(s15)[:-1]
        x["side"] = (structure_side(b1h, wilder_atr(b1h)) if len(b1h) >= 20 else structure_side(s15, wilder_atr(s15))) or "-"
        x["flags"] = flags_of(x, wins, (c1[-1]["c"] / c1[0]["c"] - 1) * 100, min_vol, e["fee_pct"], min_trials_h)
        rows.append(x)
    clusters(rows, corr_th)
    ok = sorted([r for r in rows if not r["flags"]], key=lambda r: (not r.get("entry"), -r["edge"]))   # entry-eligible first, then by score
    bad = sorted([r for r in rows if r["flags"]], key=lambda r: -r["qv"])
    return ok + bad

def table(rows, top, e=None):
    e = {**EDGE, **(e or {})}
    print(f"{'symbol':<12}{'edge%/h':>9}{'+-sd':>7}{'p_up':>6}{'need':>6}{'tr/h':>6}{'out%':>7}{'imp%':>8}{'concept':>8}"
          f"{'legs/h':>7}{'atr%':>6}{'sig_d':>6}{'ER':>6}{'vol24h':>8}{'fund%':>7}  side   in  cluster     flags")
    for r in rows[:top] + [r for r in rows if r["flags"]][:top]:
        print(f"{r['symbol']:<12}{r.get('edge', 0.0):9.3f}{r.get('edge_sd', 0.0):7.3f}{r.get('p_up', 0.0):6.2f}{r.get('need', 0.0):6.2f}"
              f"{r.get('trials_h', 0.0):6.2f}{r.get('out', 0.0):+7.3f}{r.get('impact', 0.0):8.4f}{r['concept']:8.2f}"
              f"{r['legs_h']:7.2f}{r['atr_pct']:6.2f}{r.get('sigma_d', 0.0):6.2f}{r['er']:6.2f}{r['qv'] / 1e6:7.0f}M{r['fund']:+7.3f}  {r['side']:<5} "
              f"{'ok' if r.get('entry') else '- ':>3}  {r.get('cluster', '-'):<11} {' '.join(r['flags'])}")
    if rows: print(f"  edge%/h = tr/h x (out% - fee {e['fee_pct']:.3f} - imp%), barriers win {e['win_pct']:.3f} / loss {e['loss_pct']:.3f} / hold {int(e['hold_min'])}m, "
                   f"unit {rows[0].get('unit_usdt', 0):.0f} USDT.  `in` = may be added (out% pays the toll); `need` = break-even p_up")

def main():
    # equity and basket size come from the live state and params, or the table's imp% (and the flags that read it) describes a
    # position size nobody holds — the manual run and the nightly report have to charge the same footprint select charges.
    p = load_params() or {}; states = load_states()
    eq = next((((s.get("acct") or {}).get("equity")) for s in states.values() if (s.get("acct") or {}).get("equity")), None)
    top, days, min_vol = arg("--top", 15), arg("--days", 3), arg("--min-vol", 5e7)
    nb = arg("--n-books", int((p.get("select") or {}).get("n") or len(p.get("books") or {}) or 1))   # the TARGET basket, so the table's flags match select's
    syms = arg("--sym", "").split(",") if "--sym" in sys.argv else None
    from bot.cycles import geometry
    edge = (p.get("select") or {}).get("edge") or geometry()          # as select charges it: the live ledger's current geometry, else EDGE
    sel = p.get("select") or {}
    t0 = time.time(); rows = rank(min_vol, days, syms, equity=eq, n_books=nb, always=tuple(portfolio(p)) if p else (), edge=edge,
                                  min_trials_h=float(sel.get("min_trials_h", MIN_TRIALS_H)), corr_th=float(sel.get("corr_th", 0.5)))
    table(rows, top, edge); print(f"--- {len(rows)} symbols, {days} windows of 24h, {time.time() - t0:.0f}s" + (f", geometry from {edge['n']} live cycles" if edge and edge.get('n') else ", geometry = EDGE constants"))
    if "--json" in sys.argv:
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "scan.json"), "w", encoding="utf-8") as f: json.dump(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), days=days, rows=rows), f)

if __name__ == "__main__":
    main()
