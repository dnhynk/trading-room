"""Feature engine + 순환매 state machine. Pure: fed exchange messages of one symbol, yields per-second features, signals and the
desired order set. The same code runs live (bot/cycle.py) and offline (bot/replay.py). Thresholds: params.json["sig"] / ["strat"].

Signals (side-agnostic; Strategy maps them to add/trim by side):
  DIP_SLOWING  price fell >= dip_min_atr from the swing high (swing_s window), was falling fast (min v <= -v_fast) and has now
               nearly stopped (v >= -v_slow, accelerating up) for hold_s consecutive seconds.
  POP_STALLING mirror image around the swing low. (BREAKDOWN/BREAKOUT veto nothing: Strategy reads them only as de-risk evidence
               against a position it already held when the break fired.)
  BREAKDOWN / BREAKOUT  price beyond the older extreme (brk_lookback, excluding the last 60s) by brk_atr with volume in the last
               60s > brk_vol x the 10-minute average; sets a cooldown.
v = EMA(1s log return, v_hl) / sigma, sigma = sqrt(EMA(r^2, vol_hl)); a = v - v[a_lag seconds ago]; ATR = Wilder-14 of closed 1m candles.
Confirmation features are computed and attached to every signal but do not gate it (tune first, then gate via params)."""
import math
from collections import deque

SIG = dict(vol_hl=300, v_hl=8, a_lag=5, swing_s=600, dip_min_atr=3.0, v_fast=1.0, v_slow=0.3, hold_s=3,
           brk_lookback=1800, brk_atr=0.3, brk_vol=2.0, brk_cooldown=300, cooldown=60, refire_atr=1.0, depth_levels=5,
           # regime block (per closed 1m candle, rg_window candles): efficiency ratio, drift in ATR, zigzag swings >= rg_theta %
           rg_window=90, rg_theta=0.7, rg_drift=6.0, rg_counter_max=1, rg_confirm=3, rg_dead_sw=2,
           rg_drift_min_pct=0.0,   # > 0: AGAINST/FAVOR also need the window's net move to be at least this % of price (a -1% grind in a 0.1%-ATR tape is 7 ATR but no catastrophe)
           # volume profile from the last vp_window 1m candles (volume spread over each bar's range), buckets of vp_bucket_ticks
           vp_window=360, vp_bucket_ticks=5, vp_hvn=1.5,
           # 1m-candle rule = the manual watcher's speed_line (bot/watch.py v13, 2026-08-29); emitted with src="1m"
           c1_on=1, c1_dev=0.4, c1_vr=1.5, c1_wick=0.33, c1_roc=0.3, c1_decel=0.35,
           # structure: last confirmed 1m pivot low/high (zigzag rg_theta over stop_lookback candles) for the structural stop
           stop_lookback=180,
           # volume-decay component of a deceleration (10s windows of aggressor volume vs the 10-min average): a spike of >= vd_spike x that
           # has since fallen to <= vd_decay x its peak and is still falling. vd_gate=1 makes it a requirement (DIP needs selling to
           # fade, POP needs buying to fade); 0 = logged only
           vd_spike=2.0, vd_decay=0.5, vd_gate=0)
STRAT = dict(side="long", unit_qty=70, max_units=4, max_notional=1000, step_add_pct=0.5, step_add_atr=0.7, gap_rebuy_pct=0.3,
             pop_min_pct=0.4, unit_min_pct=0.15, full_exit_pct=3.0, trim_taker_after_s=10, trim_taker_slip_pct=0.1, trim_rest_pct=0,
             trim_retrace_atr=0.5,   # a top confirmed by retrace: peak above the trim gate, then >= this x ATR back -> pull at once (wick protection, 0 disables)
             unit_frac=0.0, cap_frac=0.0, daily_loss_frac=0.0,   # >0: unit notional / cap / daily limit as fractions of wallet equity (cycle.py resizes when flat)
             gate_relax=0.5, gate_floor_unit_pct=0.05,   # each stall that fails to reach a lot's gate lowers the gate by gate_relax of the way to its floor (core: breakeven)
             add_confirm=None, confirm_within_s=90,      # opening risk needs a higher bar: None = auto (on when two books run), 1 = both signal rules / volume decay / retrace from the trough
             against_daily_mult=0.5,                     # unit multiplier when the book's side runs against the daily trend
             against_regime_mult=0.0,                    # > 0: an AGAINST regime scales the unit by this instead of vetoing adds (a size scale, never a veto)
             core_units=1, favor_pop_mult=2.0, derisk_pct=3.0, derisk_on_breakdown=True, derisk_core_frac=0.5,
             derisk_on_against=True,                     # False: the AGAINST label (a trailing 90-min statistic, late by construction) only scales adds; de-risk keeps its timely triggers (latch, fresh break)
             cap_usdt=20, stop_structural=None, stop_structural_on=1, stop_buffer_atr=0.3, stop_trail=1, stop_cooldown_s=300, max_stops_day=3,
             buy_ttl_s=90,                               # a real filter, not a backstop: rests beyond it pre-empt the next signal's lower fill (2026-08-30 tapes: 300/900/1800 s all worse even with the "left" cancel)
             cancel_v=1.0, tick=0.001, qstep=0.1)


class EMA:
    __slots__ = ("k", "v", "n")
    def __init__(self, hl): self.k, self.v, self.n = 1 - 0.5 ** (1.0 / hl), None, 0
    def add(self, x):
        self.n += 1; k = max(self.k, 1.0 / self.n)                    # expanding-window mean until the window is reached: no single-sample start
        self.v = x if self.v is None else self.v + k * (x - self.v)
        return self.v

def wilder_atr(cl, n=14):
    if len(cl) < n + 1: return None
    trs = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(cl, cl[1:])]
    a = sum(trs[:n]) / n
    for t in trs[n:]: a = (a * (n - 1) + t) / n
    return a

def round_tick(px, tick): return round(round(px / tick) * tick, 10)

SPLIT_KEYS = ("unit_qty", "cap_usdt", "daily_loss_limit", "max_notional", "unit_frac", "cap_frac", "daily_loss_frac")

def book_params(sp, side, tick, n_sides, qstep=None):
    """Per-side strategy params: side, tick, quantity step, and budgets split across the running sides (live and backtest use the same rule)."""
    p = {**sp, "side": side, "tick": tick}
    if qstep: p["qstep"] = qstep
    if n_sides > 1:
        for k in SPLIT_KEYS:
            if p.get(k): p[k] = p[k] / n_sides
    return p

def zigzag(closes, th):
    """Completed swings (signed %) between reversals of at least th (fraction); a straight move has none."""
    piv = hi = lo = closes[0]; dirn = 0; sw = []
    for p in closes[1:]:
        if dirn >= 0 and p > hi: hi = p
        if dirn <= 0 and p < lo: lo = p
        if dirn >= 0 and p <= hi * (1 - th):
            if dirn == 1: sw.append((hi / piv - 1) * 100)
            piv, lo, dirn = hi, p, -1
        elif dirn <= 0 and p >= lo * (1 + th):
            if dirn == -1: sw.append((lo / piv - 1) * 100)
            piv, hi, dirn = lo, p, 1
    return sw

def zigzag_pivots(closes, th):
    """Confirmed pivots [(index, 'H'|'L')] under the same reversal rule as zigzag()."""
    hi = lo = closes[0]; hi_i = lo_i = 0; dirn = 0; piv = []
    for i, p in enumerate(closes[1:], 1):
        if dirn >= 0 and p > hi: hi, hi_i = p, i
        if dirn <= 0 and p < lo: lo, lo_i = p, i
        if dirn >= 0 and p <= hi * (1 - th): piv.append((hi_i, "H")); lo, lo_i, dirn = p, i, -1
        elif dirn <= 0 and p >= lo * (1 + th): piv.append((lo_i, "L")); hi, hi_i, dirn = p, i, 1
    return piv


def pivot_levels(bars, atr, k=2.0, min_pct=1.0):
    """Confirmed pivot lows/highs of a bar series (zigzag on closes, threshold max(min_pct, k x ATR%)) as the bar extremes around each
    pivot, plus the extreme of the leg in progress — the levels a campaign's premise rests on. ([], []) below 20 bars."""
    if len(bars) < 20 or not atr: return [], []
    closes = [b["c"] for b in bars]; th = max(min_pct, k * atr / closes[-1] * 100) / 100
    piv = zigzag_pivots(closes, th)
    lows = [min(b["l"] for b in bars[max(0, i - 1):i + 2]) for i, kd in piv if kd == "L"][-6:]
    highs = [max(b["h"] for b in bars[max(0, i - 1):i + 2]) for i, kd in piv if kd == "H"][-6:]
    if piv and piv[-1][1] == "H": lows.append(min(b["l"] for b in bars[piv[-1][0] + 1:] or bars[-1:]))      # down-leg in progress: its low so far
    if piv and piv[-1][1] == "L": highs.append(max(b["h"] for b in bars[piv[-1][0] + 1:] or bars[-1:]))
    return lows, highs

def structural_level(f, s, ref, p, n_lots=1):
    """The campaign's premise level for the stop: the nearest confirmed 15m/1H pivot low (high for a short) that still leaves room for
    the remaining add ladder — (max_units - n_lots) adds at least one step apart below (above) ref, the last buy. None when no level
    qualifies: then the money cap alone is the stop. A level inside the ladder would stop the campaign before it could defend itself."""
    lvls = f.get("htf_lows" if s > 0 else "htf_highs") or []
    step = max(p["step_add_pct"], p["step_add_atr"] * f["atr15"] / ref * 100) if f.get("atr15") and p["step_add_atr"] > 0 else p["step_add_pct"]
    room = max(p["max_units"] - n_lots, 0) * step / 100
    ok = [x for x in lvls if s * (ref - x) > 0 and s * (ref - x) / ref >= room]
    return (max(ok) if s > 0 else min(ok)) if ok else None

def structure_side(bars, atr15, k=2.0, min_pct=1.0):
    """Which way the higher-timeframe structure points, from 15m closes: zigzag with threshold max(min_pct, k x ATR15 %); the last two
    pivot highs and lows must both step up ("long") or both step down ("short"); anything else is None (keep the current side)."""
    if len(bars) < 20 or not atr15: return None
    closes = [b["c"] for b in bars]; th = max(min_pct, k * atr15 / closes[-1] * 100) / 100
    piv = zigzag_pivots(closes, th)
    hs = [closes[i] for i, kd in piv if kd == "H"][-2:]; ls = [closes[i] for i, kd in piv if kd == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2: return None
    if hs[1] > hs[0] and ls[1] > ls[0]: return "long"
    if hs[1] < hs[0] and ls[1] < ls[0]: return "short"
    return None


def candle_features(cl):
    """Shape of the last closed 1m candle: volume vs the previous 20, lower/upper wick fractions, the 3-bar rate and the prior 3-bar rate (%)."""
    last = cl[-1]; vavg = sum(k["v"] for k in cl[-21:-1]) / len(cl[-21:-1]); rng = last["h"] - last["l"]
    return dict(vr=last["v"] / vavg if vavg else 0.0,
                lw=(min(last["o"], last["c"]) - last["l"]) / rng if rng else 0.0,
                uw=(last["h"] - max(last["o"], last["c"])) / rng if rng else 0.0,
                roc3=(last["c"] - cl[-4]["c"]) / cl[-4]["c"] * 100, roc3p=(cl[-4]["c"] - cl[-7]["c"]) / cl[-7]["c"] * 100)

def candle_rule(cl, cf, vr_hist, p):
    """The 1m rule (src="1m"), pure: the signals the last closed candle fires. A dip/pop of >= c1_dev % from the 30-bar extreme with
    (a) climax fading: a >= c1_vr volume bar within the last 3 with volume now declining from it (17x -> 8.8x -> 1.7x; never the climax
    bar itself), a wick >= c1_wick on the rejected side and the 3-bar extreme held, or (b) rate decay: the 3-bar rate <= c1_decel x the
    prior 3-bar rate (prior >= c1_roc). cl = closed candles (>= 31), cf = candle_features(cl), vr_hist = the last 3 candles' vr."""
    last = cl[-1]
    decel = abs(cf["roc3p"]) >= p["c1_roc"] and abs(cf["roc3"]) <= p["c1_decel"] * abs(cf["roc3p"])
    climax = len(vr_hist) == 3 and max(vr_hist[:2]) >= p["c1_vr"] and vr_hist[2] < vr_hist[1]
    hi30, lo30 = max(c["h"] for c in cl[-30:]), min(c["l"] for c in cl[-30:])
    held_lo = last["c"] > min(c["l"] for c in cl[-3:]); held_hi = last["c"] < max(c["h"] for c in cl[-3:])
    out = []
    if (last["c"] / hi30 - 1) * 100 <= -p["c1_dev"] and ((climax and cf["lw"] >= p["c1_wick"] and held_lo) or (decel and cf["roc3p"] < 0)): out.append("DIP_SLOWING")
    if (last["c"] / lo30 - 1) * 100 >= p["c1_dev"] and ((climax and cf["uw"] >= p["c1_wick"] and held_hi) or (decel and cf["roc3p"] > 0)): out.append("POP_STALLING")
    return out


class Features:
    """feed(msg) with parsed trade/books15/candle1m/ticker messages; returns the signals emitted (list of dicts, usually empty).
    .f holds the latest per-second feature dict (key "t" = exchange second)."""
    def __init__(self, sig=None):
        self.p = p = {**SIG, **(sig or {})}
        self.bid = self.ask = self.mid = self.mark = None
        self.bids, self.asks = [], []
        self.sec = None
        self.mids = deque(maxlen=p["brk_lookback"])
        self.var, self.vraw = EMA(p["vol_hl"]), EMA(p["v_hl"])
        self.vh = deque(maxlen=p["a_lag"] + 1)
        self.b = self.s = 0.0                                   # buy / sell volume of the open second
        self.flow = deque(maxlen=600)
        self.cvd = 0.0
        self.dbid, self.dask = deque(maxlen=60), deque(maxlen=60)
        self.candles, self.cur, self.atr, self.atr15, self.cf = [], None, None, None, {}
        self.tick, self.rg, self.vp = None, {}, None                      # tick inferred from the first book
        self.pending, self.struct_lo, self.struct_hi = [], None, None     # 1m-rule signals waiting for the next second close
        self.c15, self.side_hint = [], None                                # 15m bars (seeded + built from 1m) and the structure's side
        self.side_hint_15m = self.side_hint_1h = None; self.daily, self.daily_trend = [], None   # 1H structure is the primary side read; daily trend scales size
        self.htf_lows, self.htf_highs = [], []                             # 15m + 1H pivot levels: where a campaign's stop may sit (structural_level)
        self.dip = dict(minv=0.0, lows=[], hold=0, last=None, div=False)   # tracked since the swing high
        self.pop = dict(maxv=0.0, highs=[], hold=0, last=None, div=False)  # tracked since the swing low
        self.brk_until = self.bko_until = 0
        self.f = {}

    # ---- input ------------------------------------------------------------
    def feed(self, m):
        """One exchange message. The second(s) before this message's second close first, on the state they ended with — this
        message's quotes and prints belong to its own second (the backtest closes the same way)."""
        arg = m.get("arg") or {}; ch = arg.get("channel"); data = m.get("data")
        if not ch or not data: return []
        if ch == "trade" and m.get("action") == "snapshot": return []          # history, newest first; live updates follow
        ts = int(m.get("ts") or 0)
        if ch == "books15": ts = int(data[0].get("ts") or ts)
        out = self._clock(ts // 1000) if ts and self.mid is not None else []
        if ch == "books15":
            d = data[0]
            self.bids = [(float(x), float(q)) for x, q in d["bids"]]; self.asks = [(float(x), float(q)) for x, q in d["asks"]]
            if self.bids and self.asks:
                self.bid, self.ask = self.bids[0][0], self.asks[0][0]; self.mid = (self.bid + self.ask) / 2
                if self.tick is None and len(self.bids) > 3:
                    self.tick = min(x for x in (round(a - b, 10) for (a, _), (b, _) in zip(self.bids, self.bids[1:])) if x > 0)
        elif ch == "trade":
            for t in data:
                if t["side"] == "buy": self.b += float(t["size"])
                else: self.s += float(t["size"])
            if self.mid is None: self.mid = float(data[-1]["price"])
        elif ch == "candle1m":
            for row in data: self._candle(row)
        elif ch == "ticker":
            self.mark = float(data[0].get("markPrice") or 0) or self.mark
        return out

    def _candle(self, row):
        ts = int(row[0]); c = dict(ts=ts, o=float(row[1]), h=float(row[2]), l=float(row[3]), c=float(row[4]), v=float(row[5]))
        if self.cur is None or ts > self.cur["ts"]:
            if self.cur is not None and (not self.candles or self.cur["ts"] > self.candles[-1]["ts"]):
                self.candles.append(self.cur); del self.candles[:-600]; self._candle_closed()
            self.cur = c
        elif ts == self.cur["ts"]:
            self.cur = c

    def seed_candles(self, rows, rows15=None):
        """Closed 1m candles (dicts ts/o/h/l/c/v, oldest first) so ATR, regime and VP exist before the stream builds history;
        rows15 = older closed 15m candles for the structure-side read (the 1m history only covers ~10h)."""
        if rows15: self.c15 = [dict(r) for r in rows15][-400:]
        if self.candles or not rows: return
        self.candles = [dict(r) for r in rows][-600:]; self._candle_closed()

    def seed_daily(self, rows):
        """Daily candles (oldest first, the open day last) -> daily_trend: 'up' when close > EMA20 and EMA20 rising over 5 days, 'down' the
        mirror, else None. A book running against it trades smaller (against_daily_mult); it never blocks a trade."""
        self.daily = [dict(r) for r in rows][-80:]
        cl = [c["c"] for c in self.daily]
        if len(cl) < 26: self.daily_trend = None; return
        k = 2 / 21; e = cl[0]; ema = []
        for v in cl: e = v * k + e * (1 - k); ema.append(e)
        up, dn = cl[-1] > ema[-1] and ema[-1] > ema[-6], cl[-1] < ema[-1] and ema[-1] < ema[-6]
        self.daily_trend = "up" if up else "down" if dn else None

    def _candle_closed(self):
        cl = self.candles
        self.atr = wilder_atr(cl[-100:])
        bars = {}
        for c in cl[-450:]:                                  # 15m bars from closed 1m candles (last, possibly partial, bar dropped)
            b = bars.setdefault(c["ts"] // 900000, dict(ts=c["ts"] // 900000 * 900000, o=c["o"], h=c["h"], l=c["l"], c=c["c"]))
            b["h"], b["l"], b["c"] = max(b["h"], c["h"]), min(b["l"], c["l"]), c["c"]
        self.atr15 = wilder_atr(list(bars.values())[:-1])
        closed15 = {b["ts"]: b for b in list(bars.values())[:-1]}
        self.c15 = sorted({**{b["ts"]: b for b in self.c15}, **closed15}.values(), key=lambda b: b["ts"])[-400:]   # seeded history + bars built from 1m
        self.side_hint_15m = structure_side(self.c15, self.atr15)
        h1 = {}
        for b in self.c15:                                    # 1H bars from the 15m series: the cycle-horizon structure (primary side read)
            r = h1.setdefault(b["ts"] // 3600000, dict(ts=b["ts"] // 3600000 * 3600000, o=b["o"], h=b["h"], l=b["l"], c=b["c"]))
            r["h"], r["l"], r["c"] = max(r["h"], b["h"]), min(r["l"], b["l"]), b["c"]
        b1h = list(h1.values())[:-1]
        self.side_hint_1h = structure_side(b1h, wilder_atr(b1h)) if len(b1h) >= 20 else None
        self.side_hint = self.side_hint_1h if len(b1h) >= 20 else self.side_hint_15m
        l15, h15 = pivot_levels(self.c15, self.atr15); l1h, h1h = pivot_levels(b1h, wilder_atr(b1h) if len(b1h) >= 20 else None)
        self.htf_lows, self.htf_highs = sorted(set(l15 + l1h)), sorted(set(h15 + h1h))
        self._regime(); self._vp(); self._structure()
        if len(cl) < 31: return
        self.cf = cf = candle_features(cl)
        self.vr_hist = (getattr(self, "vr_hist", []) + [cf["vr"]])[-3:]
        if self.p["c1_on"]: self.pending += candle_rule(cl, cf, self.vr_hist, self.p)   # the manual watcher's rule, shared with bot/scan.py's engine proxy

    def _structure(self):
        """struct_lo/hi = the structural extreme a stop goes beyond: the lowest low around the last confirmed pivot low, or lower still,
        the low of the current down-leg when price is already under that pivot (a crash entry must be stopped under the new low, not
        under a pivot that is above the price). Fallback: last 60 candles."""
        cl = self.candles[-int(self.p["stop_lookback"]):]
        if len(cl) < 20: return
        piv = zigzag_pivots([c["c"] for c in cl], self.p["rg_theta"] / 100)
        lows = [i for i, k in piv if k == "L"]; highs = [i for i, k in piv if k == "H"]
        lo = min(c["l"] for c in (cl[max(0, lows[-1] - 2):lows[-1] + 3] if lows else cl[-60:]))
        hi = max(c["h"] for c in (cl[max(0, highs[-1] - 2):highs[-1] + 3] if highs else cl[-60:]))
        if piv and piv[-1][1] == "H": lo = min(lo, min(c["l"] for c in cl[piv[-1][0] + 1:] or cl[-1:]))   # down-leg in progress
        if piv and piv[-1][1] == "L": hi = max(hi, max(c["h"] for c in cl[piv[-1][0] + 1:] or cl[-1:]))   # up-leg in progress
        self.struct_lo, self.struct_hi = lo, hi

    def _regime(self):
        p, cl = self.p, self.candles; W = int(p["rg_window"])
        if len(cl) < W or not self.atr: self.rg = {}; return
        closes = [c["c"] for c in cl[-W:]]
        path = sum(abs(a - b) for a, b in zip(closes, closes[1:])); net = closes[-1] - closes[0]
        sw = zigzag(closes, p["rg_theta"] / 100)
        ups = sorted(s for s in sw if s > 0); dns = sorted(-s for s in sw if s < 0)
        self.rg = dict(rg_t=cl[-1]["ts"], rg_er=abs(net) / path if path else 0.0, rg_drift=net / self.atr, rg_up=len(ups), rg_dn=len(dns),
                       rg_med_up=ups[len(ups) // 2] if ups else 0.0, rg_med_dn=dns[len(dns) // 2] if dns else 0.0)

    def _vp(self):
        p, cl = self.p, self.candles
        if not self.tick: self.vp = None; return
        bw = self.tick * p["vp_bucket_ticks"]; prof = {}
        for c in cl[-int(p["vp_window"]):]:
            lo, hi = int(round(c["l"] / bw)), int(round(c["h"] / bw)); n = hi - lo + 1
            for b in range(lo, hi + 1): prof[b] = prof.get(b, 0.0) + c["v"] / n
        if not prof: self.vp = None; return
        total = sum(prof.values()); lo, hi = min(prof), max(prof); poc = max(prof, key=prof.get)
        acc, a, b = prof[poc], poc, poc
        while acc < 0.7 * total and (a > lo or b < hi):        # value area: grow from the POC toward the heavier neighbour
            up = prof.get(b + 1, 0.0) if b < hi else -1.0; dn = prof.get(a - 1, 0.0) if a > lo else -1.0
            if up >= dn: b += 1; acc += max(up, 0.0)
            else: a -= 1; acc += max(dn, 0.0)
        self.vp = dict(prof=prof, bw=bw, poc=poc, vah=b, val=a, mean=total / (hi - lo + 1), lo=lo, hi=hi)

    def _vp_feats(self, mid, atr):
        v = self.vp
        if not v or not atr: return {}
        b = int(round(mid / v["bw"])); hv = self.p["vp_hvn"] * v["mean"]
        sup = next((b - k for k in range(b - 1, v["lo"] - 1, -1) if v["prof"].get(k, 0.0) >= hv), None)
        res = next((k - b for k in range(b + 1, v["hi"] + 1) if v["prof"].get(k, 0.0) >= hv), None)
        return dict(vp_dens=v["prof"].get(b, 0.0) / v["mean"] if v["mean"] else 0.0, vp_poc=(mid - v["poc"] * v["bw"]) / atr,
                    vp_va=-1 if b < v["val"] else 1 if b > v["vah"] else 0,
                    vp_sup=sup * v["bw"] / atr if sup is not None else None, vp_res=res * v["bw"] / atr if res is not None else None)

    # ---- per-second clock ---------------------------------------------------
    def _clock(self, sec):
        if self.sec is None:
            self.sec = sec
            if self.candles and not self.mids:      # warm the 30-min window from candle closes so BREAKDOWN/BREAKOUT and the swing
                for c in self.candles[-(self.mids.maxlen // 60):]: self.mids.extend([c["c"]] * 60)   # references are not 8-minute artifacts after a restart
                for c in self.candles[-(self.flow.maxlen // 60):]: self.flow.extend([(c["v"] / 120, c["v"] / 120)] * 60)   # and the volume baseline is not blind for a minute
            return []
        out = []
        if sec - self.sec > 120:            # outage: every second-level state is stale; rebuild the windows from candles like at start
            self.vraw = EMA(p["v_hl"]); self.vh.clear(); self.dip.update(minv=0.0, lows=[], hold=0, div=False); self.pop.update(maxv=0.0, highs=[], hold=0, div=False)
            self.mids.clear(); self.flow.clear(); self.dbid.clear(); self.dask.clear(); self.b = self.s = 0.0; self.pending = []
            for c in self.candles[-(self.mids.maxlen // 60):]: self.mids.extend([c["c"]] * 60)
            for c in self.candles[-(self.flow.maxlen // 60):]: self.flow.extend([(c["v"] / 120, c["v"] / 120)] * 60)
            self.sec = sec - 1
        while self.sec < sec:
            out += self._close(); self.sec += 1
        return out

    def _close(self):
        p, mid, sec = self.p, self.mid, self.sec
        prev = self.mids[-1] if self.mids else mid
        r = math.log(mid / prev) if prev else 0.0
        self.var.add(r * r); sigma = max(math.sqrt(self.var.v), 1e-7)
        self.vraw.add(r); v = self.vraw.v / sigma
        ve = v if self.var.n >= p["vol_hl"] else 0.0        # the normaliser needs a half-life of data before v means anything: at a restart v was +-1 from its
                                                            # first sample (vraw and sigma from the same return) and passed the fast-phase test (8 of 11 live v signals on 2026-08-29 came within 3 min of a START)
        self.vh.append(v); a = v - self.vh[0]
        self.mids.append(mid)
        b, s = self.b, self.s; self.b = self.s = 0.0
        self.flow.append((b, s)); self.cvd += b - s
        L = p["depth_levels"]
        self.dbid.append(sum(q for _, q in self.bids[:L])); self.dask.append(sum(q for _, q in self.asks[:L]))
        allm = list(self.mids); n = len(allm); win = allm[-p["swing_s"]:]
        H, Lo = max(win), min(win)
        atr = self.atr
        d, u = self.dip, self.pop
        if mid >= H: d.update(minv=ve, lows=[], hold=0, div=False)         # making the swing high: dip tracking restarts
        else:
            d["minv"] = min(d["minv"], ve)
            if not d["lows"] or mid < d["lows"][-1][0]:
                d["div"] = bool(d["lows"]) and self.cvd > d["lows"][-1][1]   # lower price low, higher CVD low
                d["lows"].append((mid, self.cvd))
        if mid <= Lo: u.update(maxv=ve, highs=[], hold=0, div=False)
        else:
            u["maxv"] = max(u["maxv"], ve)
            if not u["highs"] or mid > u["highs"][-1][0]:
                u["div"] = bool(u["highs"]) and self.cvd < u["highs"][-1][1]
                u["highs"].append((mid, self.cvd))
        fl = list(self.flow); f10 = fl[-10:]; f30 = fl[-30:]; f60 = fl[-60:]
        vol60 = sum(x + y for x, y in f60); avg60 = sum(x + y for x, y in fl) / len(fl) * 60
        bs10 = sum(x for x, _ in f10) / max(sum(x + y for x, y in f10), 1e-9)
        buy30, sell30 = sum(x for x, _ in f30) / max(avg60 / 2, 1e-9), sum(y for _, y in f30) / max(avg60 / 2, 1e-9)   # vs the 10-min average per 30s
        m30 = allm[-31] if n > 30 else allm[0]
        # volume decay: three 10s windows (w0 latest) of sell and of buy aggressor volume vs the 10-min average per 10s
        avg10 = avg60 / 6
        def decay(idx):
            w = [sum(r[idx] for r in fl[-10:]), sum(r[idx] for r in fl[-20:-10]), sum(r[idx] for r in fl[-30:-20])]
            peak = max(w[1], w[2]); spike = peak / max(avg10, 1e-9)
            fading = peak > 0 and spike >= p["vd_spike"] and w[0] < w[1] and w[0] <= p["vd_decay"] * peak   # decay of a real spike, not of any trickle
            return spike, fading, w[0] / max(avg10, 1e-9)
        sell_spike, sell_decay, sell_now = decay(1); buy_spike, buy_decay, buy_now = decay(0)
        db, da = self.dbid[-1], self.dask[-1]
        m10 = allm[-11] if n > 10 else mid; m40 = allm[-41] if n > 40 else m10
        dec = abs(mid - m10) / abs(m10 - m40) if m10 != m40 else 0.0
        out = []
        if atr:
            D, U = (H - mid) / atr, (mid - Lo) / atr
            if n > 120:
                older = allm[:-60]; lo_old, hi_old = min(older), max(older)
                if sec >= self.brk_until and mid < lo_old - p["brk_atr"] * atr and vol60 > p["brk_vol"] * avg60:
                    self.brk_until = sec + p["brk_cooldown"]; out.append(dict(sig="BREAKDOWN"))
                if sec >= self.bko_until and mid > hi_old + p["brk_atr"] * atr and vol60 > p["brk_vol"] * avg60:
                    self.bko_until = sec + p["brk_cooldown"]; out.append(dict(sig="BREAKOUT"))
            brk, bko = sec < self.brk_until, sec < self.bko_until
            self.f = dict(t=sec, mid=mid, bid=self.bid, ask=self.ask, mark=self.mark, sigma=sigma, atr=atr, atr15=self.atr15, v=v, a=a, D=D, U=U,
                          minv=d["minv"], maxv=u["maxv"], cvd_div=d["div"], cvd_div_bear=u["div"],
                          bid_refill=db / max(min(self.dbid), 1e-9), ask_refill=da / max(min(self.dask), 1e-9),
                          imb=(db - da) / max(db + da, 1e-9), bs10=bs10, dec=dec, vol60=vol60 / max(avg60, 1e-9), brk=brk, bko=bko,
                          buy30=buy30, sell30=sell30, dmid30=(mid - m30) / atr, struct_lo=self.struct_lo, struct_hi=self.struct_hi, side_hint=self.side_hint,
                          side_hint_15m=self.side_hint_15m, side_hint_1h=self.side_hint_1h, daily_trend=self.daily_trend, htf_lows=self.htf_lows, htf_highs=self.htf_highs,
                          dip_low=d["lows"][-1][0] if d["lows"] else None, pop_high=u["highs"][-1][0] if u["highs"] else None,
                          sell_spike=sell_spike, sell_decay=sell_decay, sell_now=sell_now, buy_spike=buy_spike, buy_decay=buy_decay, buy_now=buy_now,
                          **self.cf, **self.rg, **self._vp_feats(mid, atr))
            # no veto anywhere: BREAKDOWN/BREAKOUT sit in f (brk/bko) as the 5-minute flag; Strategy reads them only as de-risk evidence
            # against a position it already held when the break fired (the deceleration after a crash is the best add, never blocked)
            vd_dip = not p["vd_gate"] or (sell_spike >= p["vd_spike"] and sell_decay)   # optional: the selling that made the dip must be fading
            vd_pop = not p["vd_gate"] or (buy_spike >= p["vd_spike"] and buy_decay)
            dip_ok = lambda: vd_dip and (d["last"] is None or sec - d["last"][0] >= p["cooldown"] or mid <= d["last"][1] - p["refire_atr"] * atr)
            pop_ok = lambda: vd_pop and (u["last"] is None or sec - u["last"][0] >= p["cooldown"] or mid >= u["last"][1] + p["refire_atr"] * atr)
            cond = D >= p["dip_min_atr"] and d["minv"] <= -p["v_fast"] and v >= -p["v_slow"] and a > 0
            d["hold"] = d["hold"] + 1 if cond else 0
            if d["hold"] >= p["hold_s"] and dip_ok(): d["last"] = (sec, mid); d["minv"] = ve; out.append(dict(sig="DIP_SLOWING", src="v"))
            cond = U >= p["dip_min_atr"] and u["maxv"] >= p["v_fast"] and v <= p["v_slow"] and a < 0
            u["hold"] = u["hold"] + 1 if cond else 0
            if u["hold"] >= p["hold_s"] and pop_ok(): u["last"] = (sec, mid); u["maxv"] = ve; out.append(dict(sig="POP_STALLING", src="v"))
            for name in self.pending:   # 1m-candle rule, same cooldown and veto as the velocity rule
                if name == "DIP_SLOWING" and dip_ok(): d["last"] = (sec, mid); out.append(dict(sig=name, src="1m"))
                if name == "POP_STALLING" and pop_ok(): u["last"] = (sec, mid); out.append(dict(sig=name, src="1m"))
        else:
            self.f = dict(t=sec, mid=mid, bid=self.bid, ask=self.ask, mark=self.mark, sigma=sigma, atr=None, v=v, a=a, brk=False, bko=False)
        self.pending = []
        dbl = {"DIP_SLOWING", "POP_STALLING"} <= {o["sig"] for o in out}   # one bar claiming both extremes (climax wick vs rate decay): recorded, not gated
        for o in out: o.update(self.f); o["dbl"] = dbl
        return out


# ---- position accounting (pure) -------------------------------------------------
def sim_match(orders, px, size, side, qstep):
    """Fill model shared by cycle.py (dry) and backtest.py, one print at a time. orders = [(key, w, bid)] with w = dict(px, qty, filled,
    queue, t) and bid = the order rests on the bid. A print through an order's price fills it whole. At its price only aggressors hitting
    our side count, and the one print is shared: orders (any book) fill in placement order from what is left after their own queue,
    the queue ahead of a later order shrinking by what the print already consumed. Returns [(key, w, fill)]."""
    out, res, ahead = [], size, 0.0
    for key, w, bid in sorted(orders, key=lambda o: o[1].get("t", 0)):
        rem = w["qty"] - w["filled"]
        if rem <= 0: continue
        if (px < w["px"] if bid else px > w["px"]): fill = rem
        elif px == w["px"]:
            if side and side != ("sell" if bid else "buy"): continue
            w["queue"] = max(w["queue"] - ahead, 0.0)
            take = min(res, w["queue"]); w["queue"] -= take; res -= take; ahead += take
            if w["queue"] > 0 or res <= 0: continue
            fill = min(rem, res); res -= fill
        else: continue
        fill = round(fill / qstep) * qstep
        if fill > 0: out.append((key, w, fill))
    return out

def pos_stats(pos):
    qty = sum(l[0] for l in pos["lots"])
    return qty, (pos.get("avg") if qty else None)

def apply_fill(pos, side_s, is_add, qty, px, oid=None, fee=0.0):
    """Mutates pos (lots=[[qty, px, oid], ...], avg). Exchange-style accounting (Bitget openPriceAvg): the average moves only on
    adds; a reduce realizes side*(px - avg)*qty and leaves the average unchanged, so a cycle (add low, sell that unit on a weak
    bounce) shows as a lower average on the same size. Lots keep the buy prices for LIFO gating; trims reduce LIFO.
    Returns realized pnl of this fill net of fee."""
    lots = pos["lots"]; q0 = sum(l[0] for l in lots)
    if is_add:
        pos["avg"] = px if not q0 else (pos["avg"] * q0 + px * qty) / (q0 + qty)
        if lots and oid is not None and lots[-1][2] == oid:
            lq, lpx, _ = lots[-1]; lots[-1] = [lq + qty, (lq * lpx + qty * px) / (lq + qty), oid]
        else: lots.append([qty, px, oid])
        pos["last"], pos["last_buy_px"] = "buy", px
        return -fee
    pnl, rem = side_s * (px - pos["avg"]) * min(qty, q0), qty
    while rem > 1e-12 and lots:
        lq, lpx, loid = lots[-1]; take = min(lq, rem); rem -= take
        if take >= lq - 1e-12: lots.pop()
        else: lots[-1] = [lq - take, lpx, loid]
    if not lots: pos["avg"] = None
    pos["last"], pos["last_trim_px"] = "trim", px
    return pnl - fee


class Strategy:
    """Desired order set from features + own position: at most one entry order, one trim order, one position stop.
    Entry: buy-side signal arms a maker order at the touch (TTL, no chase, cancelled if the move re-accelerates). Gated by halt /
    pause / post-stop cooldown / AGAINST regime, unit and notional caps, step_add below the last buy, gap_rebuy below the last trim
    while inventory is held (a flat book takes the next deceleration as a new campaign), free margin (live).
    Trim (primary, speed-based): trim-side signal with dev >= pop_min_pct sells the LIFO unit (everything at dev >= full_exit_pct)
    as a maker at the touch, then as a taker after trim_taker_after_s; dropped if dev falls back under the gate.
      FAVOR regime: the first core_units lots are never sold by pulls or the backstop; pop_min is multiplied by favor_pop_mult.
      De-risk (AGAINST regime, or -- with derisk_on_breakdown -- a BREAKDOWN/BREAKOUT that fired against a position the book already
      held; a unit bought at the deceleration after a break is not that break's victim): the gate drops to -derisk_pct so a weak
      bounce that stalls near breakeven reduces the position (half the core, then the rest); a stall at or above the normal gate
      is a normal trim even in de-risk. Break evidence clears like the latch: a fill (the campaign is cycling) or a full recovery.
    Trim (backstop): while no pull is active, the LIFO unit rests at avg +/- trim_rest_pct (0 disables).
    Stop: structural level frozen when the position opens = last 1m pivot low/high (Features.struct_lo/hi) -/+ stop_buffer_atr x ATR,
    or strat.stop_structural when set; in FAVOR it ratchets with new pivots and never loosens. The loss cap avg -/+ cap_usdt/qty
    applies whenever it is tighter.
    pos = dict(lots=[[qty, px, oid], ...], last=None|"buy"|"trim", last_buy_px, last_trim_px, halt=None|str, pause=bool,
               cooldown_until=sec, avail=USDT|None, lever=float|None).
    working = dict(buy=(px, qty)|None, trim=(px, qty)|None) currently resting, so the entry never chases a touch that moved away.
    Returned trim = (px, qty, "maker"|"taker")."""
    def __init__(self, strat=None, sig=None):
        self.p = {**STRAT, **(strat or {})}; self.sig = sig if sig is not None else dict(SIG)
        self.arm = None          # (until_sec, ref_mid) after a buy-side signal
        self.pull = None         # dict(t, qty, target) after a trim-side signal: sell qty until position <= target
        self.struct_stop = None  # structural stop of the open position (None while flat)
        self.stop_px = None      # last stop returned; once set for a position it is never moved against the position
        self.peak = None         # best favourable mid since the last fill (retrace-based top detection)
        self.fail_n = 0          # trim-side stalls that failed to reach the LIFO lot's gate (gate relaxation); the lot's own history
        self.last_lot = None     # LIFO lot id at the last step: a new lot (buy, or the next lot after a sell-out) starts a fresh count
        self.gate_eff = None     # the relaxed gate in force (state/report)
        self.sig_seen = {}       # buy-side signal source -> exchange second (agreement check for confirmed adds)
        self.arm_filled = 0.0    # quantity filled against the current arm (progress is per order, not net position)
        self.derisk_armed, self.last_qty = False, 0.0
        self.brk_seen = False    # a break against the side fired while the book held a position: de-risk evidence must postdate the campaign
        self.regime, self.rg_cand, self.rg_pend, self.rg_t = "TWO_WAY", "TWO_WAY", 0, None

    def on_fill(self, role, qty):
        """The OMS reports every fill; entry fills advance the arm."""
        if role == "buy" and self.arm: self.arm_filled += qty

    def adopt_stop(self, px):
        """An existing exchange stop becomes the position's baseline: it can only tighten from here."""
        self.struct_stop = self.stop_px = px

    def _regime(self, f, s, ev):
        """Side-relative regime from the per-minute block in f. A circuit breaker, not a filter: strong drift against us with the
        favourable swings collapsing, confirmed rg_confirm minutes, released with hysteresis. FAVOR/DEAD are labels only."""
        if not f.get("rg_t") or f["rg_t"] == self.rg_t: return
        self.rg_t = f["rg_t"]; g = self.sig
        fav_n, adv_n = (f["rg_up"], f["rg_dn"]) if s > 0 else (f["rg_dn"], f["rg_up"])
        fav_m, adv_m = (f["rg_med_up"], f["rg_med_dn"]) if s > 0 else (f["rg_med_dn"], f["rg_med_up"])
        drift = s * f["rg_drift"]; asym = fav_m / adv_m if adv_m else (9.9 if fav_m else 1.0)
        big = abs(f["rg_drift"]) * (f.get("atr") or 0.0) / f["mid"] * 100 >= g.get("rg_drift_min_pct", 0.0)   # the move is large in price, not only in ATR
        # one-way = strong drift AND (almost) no swings against it; a trend with 1%+ counter-swings every 20 minutes is cycle territory
        if drift <= -g["rg_drift"] and fav_n <= g["rg_counter_max"] and big: cand = "AGAINST"
        elif drift >= g["rg_drift"] and adv_n <= g["rg_counter_max"] and big: cand = "FAVOR"
        elif fav_n + adv_n < g["rg_dead_sw"]: cand = "DEAD"
        else: cand = "TWO_WAY"
        if self.regime == "AGAINST" and cand != "AGAINST" and not (fav_n > g["rg_counter_max"] or drift > -g["rg_drift"] / 2): cand = "AGAINST"
        if cand == self.regime: self.rg_pend = 0; return
        self.rg_pend = self.rg_pend + 1 if cand == self.rg_cand else 1; self.rg_cand = cand
        if self.rg_pend >= g["rg_confirm"]:
            ev.append(("REGIME_CHANGE", dict(regime=cand, was=self.regime, er=round(f["rg_er"], 2), drift=round(drift, 1), asym=round(asym, 2), fav=fav_n, adv=adv_n)))
            self.regime, self.rg_pend = cand, 0

    def step(self, f, sigs, pos, working=None):
        p, ev = self.p, []
        s = 1 if p["side"] == "long" else -1; tick = p["tick"]
        mid, bid, ask, t, atr = f["mid"], f["bid"], f["ask"], f["t"], f.get("atr")
        qty, avg = pos_stats(pos)
        self._regime(f, s, ev)
        favor = self.regime == "FAVOR"
        # minimum distance of an add below the last buy: ATR-relative (step_add_atr x ATR15), floored by step_add_pct
        step = max(p["step_add_pct"], p["step_add_atr"] * f["atr15"] / mid * 100) if f.get("atr15") and p["step_add_atr"] > 0 else p["step_add_pct"]
        buy_sig, trim_sig = ("DIP_SLOWING", "POP_STALLING") if s > 0 else ("POP_STALLING", "DIP_SLOWING")
        touch_in, touch_out = (bid, ask) if s > 0 else (ask, bid)
        names = {x["sig"] for x in sigs}
        if ("BREAKDOWN" if s > 0 else "BREAKOUT") in names and qty: self.brk_seen = True   # the break hit a campaign that already existed
        if not qty or not (f["brk"] if s > 0 else f["bko"]): self.brk_seen = False         # flat, or the 5-minute flag has expired
        blocked = (pos.get("halt") or ("pause" if pos.get("pause") else None) or ("cooldown" if t < pos.get("cooldown_until", 0) else None)
                   or ("regime" if self.regime == "AGAINST" and not p["against_regime_mult"] else None))
        # entry / add / rebuy: arm on the buy-side signal, rest at the touch until filled, TTL, or the move re-accelerates
        qs = p.get("qstep") or 0.1                                  # the unit is a whole number of exchange quantity steps before it is armed
        mult = pos.get("unit_mult", 1.0) * (p["against_regime_mult"] if self.regime == "AGAINST" and p["against_regime_mult"] else 1.0)   # AGAINST: smaller adds, not none
        unit = round(max(round(p["unit_qty"] * mult / qs) * qs, qs), 9)
        confirm_on = p["add_confirm"] if p["add_confirm"] is not None else 0
        for x in sigs:                                              # remember when each rule last fired the buy-side signal
            if x["sig"] == buy_sig: self.sig_seen[x.get("src", "?")] = t
        def confirmed():
            """Opening risk needs a higher bar than the trim-grade stall: both rules within confirm_within_s, or the aggressor volume
            that made the move is fading, or price has already come back from the extreme by trim_retrace_atr x ATR."""
            if not confirm_on: return True, ""
            srcs = [k for k, tt in self.sig_seen.items() if t - tt <= p["confirm_within_s"]]
            if len(set(srcs)) >= 2: return True, "both"
            if (f.get("sell_decay") if s > 0 else f.get("buy_decay")): return True, "decay"
            ext = f.get("dip_low") if s > 0 else f.get("pop_high")
            if ext and atr and s * (mid - ext) >= p["trim_retrace_atr"] * atr: return True, "retrace"
            return False, ""
        def caps_why(extra):   # caps are quantity-based (a partially filled unit does not count as a whole one)
            if qty + extra > p["max_units"] * unit + 1e-9: return "max_units"
            if (qty + extra) * mid > p["max_notional"]: return "max_notional"
            if self.stop_px is not None and qty and extra > 0:      # the loss cap is an add budget: never loosen the stop to fit more units
                avg2 = (avg * qty + mid * extra) / (qty + extra)
                if s * (avg2 - self.stop_px) * (qty + extra) > p["cap_usdt"]: return "cap"
            return None
        if self.arm and self.arm_filled >= self.arm[2] - 1e-9: ev.append(("DISARM", dict(why="filled"))); self.arm = None   # a completed unit frees the next signal
        if buy_sig in names:
            why = None
            if blocked: why = blocked
            elif caps_why(unit): why = caps_why(unit)
            elif qty and pos.get("last") == "trim" and s * (pos["last_trim_px"] - mid) / mid * 100 < p["gap_rebuy_pct"]: why = "gap_rebuy"   # anti-churn of the inventory cycle only: a flat book has nothing to cycle
            elif pos.get("last") == "buy" and s * (pos["last_buy_px"] - mid) / mid * 100 < step: why = f"step_add({step:.2f}%)"
            elif pos.get("avail") is not None and pos.get("lever") and pos["avail"] < unit * mid / pos["lever"] * 1.2: why = "margin"
            ok, how = confirmed()
            if not why and not ok: why = "unconfirmed"
            if why: ev.append(("SKIP", dict(sig=buy_sig, why=why, mid=mid, avail=pos.get("avail"))))
            elif self.arm: ev.append(("SKIP", dict(sig=buy_sig, why="armed", mid=mid)))
            else: self.arm = (t + p["buy_ttl_s"], mid, unit); self.arm_filled = 0.0; ev.append(("ARM", dict(mid=mid, until=self.arm[0], unit=unit, confirm=how or None)))
        buy = None
        if self.arm:
            rem = round(self.arm[2] - self.arm_filled, 9)
            if blocked: ev.append(("DISARM", dict(why=blocked))); self.arm = None
            elif caps_why(rem): ev.append(("DISARM", dict(why=caps_why(rem)))); self.arm = None
            elif t >= self.arm[0]: ev.append(("DISARM", dict(why="ttl"))); self.arm = None
            elif s * (mid / self.arm[1] - 1) * 100 >= p["pop_min_pct"]: ev.append(("DISARM", dict(why="left", mid=mid))); self.arm = None   # a pop-sized bounce from the signal price: the move the deceleration predicted happened without us; a later fill here would be a new down-move bought without a reading
            elif s * f["v"] <= -p["cancel_v"]: ev.append(("DISARM", dict(why="reaccel", v=round(f["v"], 2)))); self.arm = None
            else:
                px = touch_in; w = (working or {}).get("buy")
                if w and s * (w[0] - px) < 0: px = w[0]        # touch moved away from us: keep our price, no chase
                buy = (round_tick(px, tick), rem)                # the part of the unit still unfilled
        # trim: speed-based pull (maker at the touch, taker after trim_taker_after_s) else the resting backstop
        trim = None
        if not qty: self.pull = None
        else:
            lot_qty, lot_px = pos["lots"][-1][0], pos["lots"][-1][1]
            is_core = len(pos["lots"]) <= int(p["core_units"])       # the LIFO lot is (part of) the core
            ref = avg if is_core else lot_px                          # what this lot must beat: the average for the core, its own buy price for an added unit
            dev, dev_lot = s * (mid / avg - 1) * 100, s * (mid / ref - 1) * 100
            # de-risk when the market moved against us without offering an add: AGAINST regime, a volume break that hit the position we
            # already held (never one that preceded the entry), or the position having been a full add-step under the average with no
            # deceleration (sticky until a fill or a full recovery)
            g_norm = (p["pop_min_pct"] if is_core else p["unit_min_pct"]) * (p["favor_pop_mult"] if favor else 1.0)
            lot_id = pos["lots"][-1][2]
            if qty > self.last_qty or lot_id != self.last_lot: self.fail_n = 0               # a new lot gets a fresh expectation; a partial trim of the same lot keeps its refusals
            self.last_lot = lot_id
            floor = 0.0 if is_core else p["gate_floor_unit_pct"]                              # core: breakeven; added unit: fees covered
            g_rel = floor + (g_norm - floor) * (1 - p["gate_relax"]) ** self.fail_n            # the market refusing bounces lowers the bar
            if qty != self.last_qty or dev_lot >= g_rel: self.derisk_armed = self.brk_seen = False   # a fill or a full recovery clears the damage evidence (latch and break alike) ...
            elif dev <= -step: self.derisk_armed = True                                    # ... and it can only re-arm on a later tick
            derisk = p["derisk_pct"] > 0 and ((self.regime == "AGAINST" and p["derisk_on_against"]) or self.derisk_armed
                                              or (p["derisk_on_breakdown"] and self.brk_seen))
            gate = -p["derisk_pct"] if (derisk and is_core) else g_rel      # an added unit is only ever sold above its own buy price; the loss is taken on the core vs the average
            self.gate_eff = gate
            core = sum(l[0] for l in pos["lots"][:int(p["core_units"])]) if (favor or (derisk and dev_lot < g_rel)) and dev < p["full_exit_pct"] else 0.0   # no sacred core: at full_exit everything sells, FAVOR or not; de-risk only shapes the weak bounce
            sellable = max(qty - core, 0.0)
            if derisk and sellable <= 0:                                                # only the core left: cut part of it near breakeven ...
                sellable = core * p["derisk_core_frac"]
                if core - sellable <= unit * (1 - p["derisk_core_frac"]) ** 2 + 1e-9: sellable = core   # ... half, then the rest: a remainder no bigger than what two cuts leave (a quarter unit) goes whole, never a dust tail
            if self.pull and qty <= self.pull["target"] + qs / 2: self.pull = None          # sold what the pull asked for (within half an exchange step)
            elif self.pull and qty > self.last_qty + 1e-9: ev.append(("PULL_DROP", dict(why="add", dev_lot=round(dev_lot, 2)))); self.pull = None   # a lot bought meanwhile is not the pull's to sell (its target is absolute): the next stall judges the new LIFO lot
            if trim_sig in names and not self.pull and dev_lot < gate and p["gate_relax"] > 0:   # a stall the lot could not use: relax its gate
                self.fail_n += 1; ev.append(("GATE_RELAX", dict(fails=self.fail_n, gate=round(floor + (g_norm - floor) * (1 - p["gate_relax"]) ** self.fail_n, 3), dev_lot=round(dev_lot, 2))))
            # top confirmed by retrace: the best price since the last fill cleared the gate and price has come back >= trim_retrace_atr x ATR
            if qty != self.last_qty or self.peak is None: self.peak = mid
            elif s * (mid - self.peak) > 0: self.peak = mid
            retrace_top = (p["trim_retrace_atr"] > 0 and atr and s * (self.peak / ref - 1) * 100 >= g_rel
                           and s * (self.peak - mid) >= p["trim_retrace_atr"] * atr and dev_lot >= gate)
            if (trim_sig in names or retrace_top) and dev_lot >= gate and not self.pull and sellable >= qs - 1e-9:
                sell = sellable if dev >= p["full_exit_pct"] else min(lot_qty, sellable)
                sell = min(qty, round(round(sell / qs) * qs, 9))                                  # whole exchange steps: a half of 76.1 is 38.0, never 38.05 (live 21:37: the 0.05 remainder was rejected every 5s and the zombie pull blocked every later trim)
                if sell < qs - 1e-9: sell = 0.0
                self.pull = dict(t=t, qty=sell, target=qty - sell, gate=gate, ref=ref, px0=touch_out)
                ev.append(("PULL_TRIM", dict(dev=round(dev, 2), dev_lot=round(dev_lot, 2), ref=ref, qty=sell, all=sell >= qty - 1e-9, px=touch_out,
                                             mode="derisk" if derisk else "favor" if favor else ("retrace" if trim_sig not in names else "normal"),
                                             peak=self.peak)))
            if self.pull:
                if s * (mid / self.pull["ref"] - 1) * 100 < self.pull["gate"] - tick / self.pull["ref"] * 100:   # one tick of hysteresis: a wiggle at the gate must not cancel and re-queue the maker (2026-08-30 03:17: six pull/drop flips in 41 s lost the queue)
                    ev.append(("PULL_DROP", dict(dev_lot=round(dev_lot, 2)))); self.pull = None
                elif t - self.pull["t"] >= p["trim_taker_after_s"] or s * (self.pull["px0"] - mid) / mid * 100 >= p["trim_taker_slip_pct"]:
                    trim = (round_tick(touch_in, tick), round(qty - self.pull["target"], 9), "taker")   # waited long enough, or the stall is already turning: take it
                else: trim = (round_tick(touch_out, tick), round(qty - self.pull["target"], 9), "maker")
            if trim is None and p["trim_rest_pct"] > 0 and (min(lot_qty, sellable) if favor else lot_qty) > 0:
                px = avg * (1 + s * p["trim_rest_pct"] / 100)
                px = max(px, touch_out) if s > 0 else min(px, touch_out)
                trim = (round_tick(px, tick), min(lot_qty, sellable) if favor else lot_qty, "maker")
        # stop: structural level (frozen at open; ratchets in FAVOR), tightened by the USDT loss cap when that is closer
        stop = None
        if not qty: self.struct_stop = self.stop_px = None; self.fail_n = 0; self.gate_eff = None; self.arm_filled = self.arm_filled if self.arm else 0.0
        else:
            cap_px = avg - s * p["cap_usdt"] / qty
            # the structural stop is the campaign's premise (15m/1H pivot) and must leave room for the remaining add ladder below the last buy;
            # a level inside the ladder is ignored (money cap only). Structure only when switched on, or riding a FAVOR one-way.
            lvl = structural_level(f, s, pos.get("last_buy_px") or mid, p, len(pos["lots"])) if (p["stop_structural_on"] or favor) else None
            if lvl and (f.get("atr15") or atr):
                cand = lvl - s * p["stop_buffer_atr"] * (f.get("atr15") or atr)
                if self.struct_stop is None and self.stop_px is None:
                    if s * (mid - cand) > 0: self.struct_stop = cand; ev.append(("STRUCT_STOP", dict(level=lvl, stop=round(cand, 6))))
                    else: ev.append(("STRUCT_SKIP", dict(level=lvl, mid=mid)))          # structure is above the price: money cap only
                elif (self.struct_stop is not None and (favor or p["stop_trail"]) and s * (mid - cand) > 0
                      and s * (round_tick(cand, tick) - round_tick(self.struct_stop, tick)) >= tick - 1e-12):    # a newly defended low, at least one tick tighter
                    self.struct_stop = cand; ev.append(("TRAIL", dict(level=lvl, stop=round_tick(cand, tick))))
            st = p.get("stop_structural") or self.struct_stop
            stop = cap_px if st is None else (max(st, cap_px) if s > 0 else min(st, cap_px))
            if self.stop_px is None and s * (mid - stop) <= 0:      # only a brand-new stop can be "wrong"; an existing one that price reaches is a stop hit, never moved
                ev.append(("STOP_INVALID", dict(stop=stop, mid=mid)))
                stop = cap_px if s * (mid - cap_px) > 0 else None
            if stop is not None:
                if self.stop_px is not None and s * (stop - self.stop_px) < 0: stop = self.stop_px   # never looser than the stop already set
                stop = round_tick(stop, tick)
            elif self.stop_px is not None: stop = self.stop_px
            self.stop_px = stop
        self.last_qty = qty
        # no_stop: a position for which no valid stop exists (price already beyond the money cap) — the OMS must close it now
        return dict(buy=buy, trim=trim, stop=stop, no_stop=bool(qty) and stop is None, events=ev)
