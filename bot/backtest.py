"""Offline run of the whole engine (Features + Strategy + the dry-run fill model) over recordings — the tuner's objective.
  python -m bot.backtest FILE... [--sym TRUMPUSDT] [--equity E | --fixed] [--qstep Q] [--sides long,short] [--follow 15m|1h|brk|regime] [--sig k=v ...] [--strat k=v ...]
--sym runs another recorded symbol from the same tape with that symbol's own contract step and the sizing live would give it (see
Sizing below); a symbol outside params.books is sized as one of select.n books.
--sides long,short runs a long book and a short book on the same feature stream (쌍검술); default = strat.side only.
--follow is the side-automation counterfactual: both books exist but only the active side may trade; it starts on strat.side and
changes only while flat, to the 15m / 1H structure hint's side, (brk) the side of the last volume break, or (regime) the side the
long book's regime label favours (AGAINST -> short, FAVOR -> long); None keeps it. Metrics carry follow={flips, share} and the
events a FLIP per change.
Recordings are first condensed to one record per exchange second (bid/ask, top-5 depth, mark, trades, candle rows), grouped and
sorted by second so cross-channel timestamp inversions cannot leak future quotes into earlier seconds, and cached under data/cache/.
Before the first second the engine is seeded like the live start: 1000 closed 1m candles (ATR, regime, VP, 1m structure), 200 closed
15m candles (the 15m/1H pivot structure the stop rests on) and 80 daily candles (daily trend -> unit size), all ending before the
recording, fetched once from the public REST history and cached under data/cache/seed-*.json. A recording started mid-stream carries
no candle snapshot at all (only the recorder's first file does, ~8h), so without the seed ATR15 and the structural stop would be
missing for hours. Without network the run falls back to the recording alone (a warning on stderr). The daily trend is not
refreshed hourly as live.
Fill model = cycle.py dry mode (signal.sim_match / sim_book): a resting order fills when a trade prints through its price — except in
the second it was placed, where such a print is the exchange's post-only cancel —, or at its price once the queue ahead of it has
drained, by aggressors hitting our side and by the cancellations each second's book reveals (the displayed queue turns over 1.4-5x by
cancellation during an order's life, live record 2026-09-02); takers fill at the touch. Keep sim_trades / sim_book / reconcile in
step with bot/cycle.py. Daily stop count and the daily loss limit roll over at UTC midnight like the live engine.
Sizing = live's: the strat is `ws.strat_for(params, sym)` (the book's wallet_frac / sides), the quantity step comes from the symbol's
contract (cached under data/cache/contract-*.json), and when params size by equity (unit_frac ...) the fixed unit / cap / daily limit /
notional are derived once from --equity (default: the latest logs/state-*.json equity) at the tape's first mid — the engine cannot
resize offline, so the campaign geometry is frozen at the start. --fixed keeps the file's unit_qty / cap_usdt / daily_loss_limit /
max_notional instead (the equity-independent reference numbers in RULES 도구 절). Running another symbol without its contract step
and a matched notional rounded ZEC to TRUMP-sized units before this (RULES 도구 절).
Output: realized pnl net of fees, open pnl at the end, cycles (trim fills), adds, stops, max drawdown of realized+open, time in market,
and the sizing the run used."""
import gzip, json, os, pickle, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.signal import Features, Strategy, STRAT, SPLIT_KEYS, apply_fill, pos_stats, book_params, sim_match, sim_book, unit_under_cap, wilder_atr
from bot.ws import load_params, load_states, strat_for
from bot.bitget import Bitget

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "cache"); CACHE_VER = "v2"
OID = "cyc-"; SLIP = 0.0005; MAKER, TAKER = 0.0002, 0.0006
SIZED = {"unit_qty": "unit_frac", "cap_usdt": "cap_frac", "daily_loss_limit": "daily_loss_frac", "max_notional": "notional_frac"}   # as cycle.SIZED


def contract_meta(sym):
    """The symbol's quantity step and price tick from the exchange contract (public REST), cached; None when unreachable."""
    os.makedirs(CACHE, exist_ok=True)
    cp = os.path.join(CACHE, f"contract-{sym}.json")
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as f: return json.load(f)
    try:
        c = Bitget("", "", "").contract(sym)
        d = dict(qstep=10 ** -int(c["volumePlace"]), tick=float(c["priceEndStep"]) * 10 ** -int(c["pricePlace"]))
    except Exception as e:
        print(f"contract: {sym} unavailable ({type(e).__name__}: {str(e)[:80]}); quantity step defaults to 0.1", file=sys.stderr); return None
    tmp = f"{cp}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(d, f)
    os.replace(tmp, cp)
    return d

def latest_equity():
    """The most recent account equity any engine wrote (logs/state-*.json), or None."""
    sts = [s for s in load_states().values() if (s.get("acct") or {}).get("equity")]
    return max(sts, key=lambda s: s.get("t", ""))["acct"]["equity"] if sts else None

def size_from_equity(strat, equity, mid, qstep, atr=None):
    """What cycle.Book.resize would set at this equity and price: the file-level (pre side-split) fixed sizes for every fraction that is
    on, the fractions themselves switched off. wallet = equity x wallet_frac, as live; atr = the seeded ATR(1m) for cap_min_atr."""
    wallet = equity * strat.get("wallet_frac", 1.0); out = dict(strat)
    for fixed, frac in SIZED.items():
        if not strat.get(frac): continue
        v = wallet * strat[frac]
        if fixed == "unit_qty":
            cap = wallet * strat["cap_frac"] if strat.get("cap_frac") else strat.get("cap_usdt")
            v = unit_under_cap(v / mid, cap, atr, strat.get("cap_min_atr")) * mid      # the money cap's ATR floor, as resize
            v = max(round(v / mid / qstep) * qstep, qstep)
        out[fixed], out[frac] = round(v, 9), 0.0
    return out


# ---- per-second condensation ----------------------------------------------------
def lines(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        for line in f:
            i = line.find("\t")
            if i > 0: yield line[i + 1:]

def condense(path, sym):
    """[(sec, bid, ask, bids5, asks5, mark, trades[(px, sz, side)], candle_rows)] for one symbol, one record per second, sorted."""
    key = f'"instId":"{sym}"'; recs = {}
    for raw in lines(path):
        if key not in raw or '"local"' in raw: continue
        try: j = json.loads(raw)
        except ValueError: continue                      # a torn line (two writers, a crash mid-write) is skipped, never fatal
        ch = j["arg"]["channel"]; data = j.get("data")
        if not data: continue
        ts = int(j.get("ts") or 0)
        if ch == "books15": ts = int(data[0].get("ts") or ts)
        r = recs.setdefault(ts // 1000, [ts // 1000, None, None, None, None, None, [], []])
        if ch == "books15":
            d = data[0]; r[3] = [[float(p), float(q)] for p, q in d["bids"][:5]]; r[4] = [[float(p), float(q)] for p, q in d["asks"][:5]]
            if r[3] and r[4]: r[1], r[2] = r[3][0][0], r[4][0][0]
        elif ch == "trade":
            if j.get("action") != "snapshot": r[6] += [(float(t["price"]), float(t["size"]), t["side"]) for t in data]
        elif ch == "candle1m": r[7] += [list(x) for x in data]
        elif ch == "ticker": r[5] = float(data[0].get("markPrice") or 0) or r[5]
    out, last = [], [None, None, None, None, None]
    for sec in sorted(recs):                      # carry the last known book / mark forward into seconds without an update
        r = recs[sec]
        for i in (1, 2, 3, 4, 5):
            if r[i] is None: r[i] = last[i - 1]
            else: last[i - 1] = r[i]
        out.append(r)
    return out

def seed_history(sym, start_sec):
    """What the live engine seeds at start and the recording cannot supply: closed 1m (1000), 15m (200) and daily (80) candles ending
    before start_sec, from the public REST history, cached per (symbol, start). (None, None, None) when the exchange is unreachable."""
    os.makedirs(CACHE, exist_ok=True)
    cp = os.path.join(CACHE, f"seed-{sym}-{int(start_sec)}.json")
    if os.path.exists(cp):
        with open(cp, encoding="utf-8") as f: d = json.load(f)
        return d.get("c1"), d["c15"], d["daily"]
    try:
        b = Bitget("", "", ""); end = int(start_sec) * 1000
        c15 = [r for r in b.history_candles(sym, "15m", end, 200) if r["ts"] + 900_000 <= end]        # closed before the start only
        daily = [r for r in b.history_candles(sym, "1D", end, 80) if r["ts"] + 86_400_000 <= end]
        c1, cursor = {}, end
        for _ in range(5):                                                                             # 1000 x 1m as live, 200 per call
            rows = [r for r in b.history_candles(sym, "1m", cursor, 200) if r["ts"] + 60_000 <= cursor]
            if not rows: break
            c1.update({r["ts"]: r for r in rows}); cursor = rows[0]["ts"]
        c1 = [c1[k] for k in sorted(c1)]
    except Exception as e:
        print(f"seed: REST history unavailable ({type(e).__name__}: {str(e)[:80]}); 15m/1H structure and daily trend from the recording only", file=sys.stderr)
        return None, None, None
    tmp = f"{cp}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(dict(c1=c1, c15=c15, daily=daily), f)
    os.replace(tmp, cp)
    return c1, c15, daily

def load_seconds(path, sym):
    os.makedirs(CACHE, exist_ok=True)
    cp = os.path.join(CACHE, f"{os.path.basename(path).replace('.jsonl', '').replace('.gz', '')}.{sym}.{CACHE_VER}.pkl")
    if os.path.exists(cp) and os.path.getmtime(cp) >= os.path.getmtime(path):
        with open(cp, "rb") as f: return pickle.load(f)
    secs = condense(path, sym)
    tmp = f"{cp}.{os.getpid()}.tmp"                # atomic: a concurrent reader never sees a partial pickle
    with open(tmp, "wb") as f: pickle.dump(secs, f)
    os.replace(tmp, cp)
    return secs


# ---- engine --------------------------------------------------------------------------
class Book:
    """One side's position, orders and stop (a long book and a short book can run on the same feature stream = 쌍검술)."""
    def __init__(self, feat, strat, side, n_sides=1, qstep=0.1):
        bp = book_params(strat or {}, side, 0.001, n_sides, qstep, fee_rt=(MAKER + TAKER) * 100)   # the unit gate's floor covers a taker exit, as live
        if bp.get("add_confirm") is None: bp["add_confirm"] = 1 if n_sides > 1 else 0          # same auto rule as live
        self.strat = Strategy(bp, feat.p); self.side, self.s = side, (1 if side == "long" else -1)   # same per-side budget split as live
        self.pos = dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None, halt=None, pause=False)
        self.work, self.replaced, self.stop = dict(buy=None, trim=None), dict(buy=-1e9, trim=-1e9), None
        self.realized = self.day_realized = self.upl = 0.0; self.cycles = self.adds = self.stops = self.stops_today = 0; self.taker_t = -1e9; self.seq = 0

class Engine:
    def __init__(self, sig=None, strat=None, qstep=0.1, sides=None, follow=None):
        self.feat = Features(sig); strat = strat or {}
        sides = list(sides or strat.get("sides") or [strat.get("side", STRAT["side"])])
        self.px_tick, self.qstep = None, qstep
        self.follow, self.flips, self.active_s = follow, 0, {}       # follow="15m"|"1h": one side at a time, chosen by that structure hint, flipped only when flat (the side automation counterfactual)
        self.follow_confirm_s, self.hint_last, self.hint_t = 0, None, None   # a flip needs the hint to have held for follow_confirm_s (0 = at once)
        self.active = strat.get("side", STRAT["side"]) if follow else None   # starts on the configured side (the incumbent's, as select leaves it); the hint flips it
        self.books = {sd: Book(self.feat, strat, sd, 1 if follow else len(sides), qstep) for sd in sides}
        self.peak = self.max_dd = 0.0; self.in_mkt = self.n = 0; self.last_t = None; self.day = None
        self.events = []
        self.by_hint, self.last_real = {}, {}                    # realized pnl per book split by the 1H structure hint in force
        self.cap, self.min_mid = {}, None                        # direction capture: minute moves that happened while each book held a position

    def seed(self, c1, c15, daily):
        """Live-start history the recording lacks (seed_history); before the first second is fed."""
        if c1 or c15: self.feat.seed_candles(c1 or [], c15)
        if daily: self.feat.seed_daily(daily)

    # -- fills
    def on_fill(self, bk, role, qty, px, fee, oid, lot=None):
        n0 = len(bk.pos["lots"])
        pnl = apply_fill(bk.pos, bk.s, role == "buy", qty, px, oid=oid, fee=fee, lot=lot); bk.realized += pnl; bk.day_realized += pnl; bk.strat.on_fill(role, qty)
        w = bk.work[role]
        if role == "buy": bk.adds += 0 if (w and w["oid"] == oid and w["filled"] > 0) else 1   # an add = an order that filled, not each partial print (recon compares with live clientOids)
        else: bk.cycles += max(n0 - len(bk.pos["lots"]), 0)        # a cycle = a lot bought and sold out, however many fills the selling took
        if w and w["oid"] == oid:
            w["filled"] += qty
            if w["filled"] >= w["qty"] - self.qstep / 2: bk.work[role] = None
        self.events.append((self.feat.f.get("t"), "FILL", bk.side, role, qty, px, round(pnl, 4)))
        self.tick_book(bk, [])

    def on_stop_hit(self, bk, px):
        q, avg = pos_stats(bk.pos)
        pnl = apply_fill(bk.pos, bk.s, False, q, px, fee=q * px * TAKER); bk.realized += pnl; bk.day_realized += pnl; bk.stops += 1; bk.stops_today += 1
        bk.stop = None; bk.work = dict(buy=None, trim=None)
        self.events.append((self.feat.f.get("t"), "STOP_HIT", bk.side, q, px, round(pnl, 4)))
        if bk.stops_today >= bk.strat.p["max_stops_day"]: bk.pos["halt"] = "DAILY_STOPS"
        else: bk.pos["cooldown_until"] = self.feat.f["t"] + bk.strat.p["stop_cooldown_s"]

    def follow_step(self, sec):
        """Side automation as select would do it for the incumbent: the active side is the hint's side, changed only while every book is
        flat; a hint of None keeps the current side. Books off the active side get no signals and no resting entry."""
        f = self.feat.f
        rg = self.books["long"].strat.regime if "long" in self.books else None                  # the long book's side-relative label: AGAINST = the market runs short
        h = (self.feat.side_hint_15m if self.follow == "15m" else self.feat.side_hint_1h if self.follow == "1h"
             else ("short" if rg == "AGAINST" else "long" if rg == "FAVOR" else None) if self.follow == "regime"
             else ("short" if f.get("leg_ow") == -1 else "long" if f.get("leg_ow") == 1 else None) if self.follow == "leg"   # the current-leg read (sig.rg_leg_*), whatever the Strategy uses
             else "short" if f.get("brk") else "long" if f.get("bko") else None)          # "brk": the side of the last volume break (5-min flag) — event-based, not structure-based
        if h != self.hint_last: self.hint_last, self.hint_t = h, sec
        if h and h != self.active and sec - self.hint_t >= self.follow_confirm_s and not any(pos_stats(bk.pos)[0] for bk in self.books.values()):
            if self.active: self.flips += 1
            self.active = h; self.events.append((sec, "FLIP", h))
            for sd, bk in self.books.items():
                if sd != h: bk.work = dict(buy=None, trim=None); bk.strat.arm = None
        self.active_s[self.active or "none"] = self.active_s.get(self.active or "none", 0) + 1

    # -- per-second step (same order as cycle.py)
    def tick(self, sigs):
        f = self.feat.f
        if not f.get("atr") or f.get("bid") is None: return
        if self.px_tick is None and self.feat.tick:
            self.px_tick = self.feat.tick
            for bk in self.books.values(): bk.strat.p["tick"] = self.px_tick
        if self.px_tick is None: return
        for bk in self.books.values(): self.tick_book(bk, sigs if not self.follow or bk.side == self.active else [])

    def tick_book(self, bk, sigs):
        f = self.feat.f; qty, avg = pos_stats(bk.pos)
        if qty and bk.stop and bk.s * ((f.get("mark") or f["mid"]) - bk.stop) <= 0:
            self.on_stop_hit(bk, bk.stop * (1 - bk.s * SLIP)); return
        lim = bk.strat.p.get("daily_loss_limit")
        if lim and not bk.pos["halt"] and bk.day_realized + bk.upl <= -lim: bk.pos["halt"] = "DAILY_LOSS"; self.events.append((f["t"], "DAILY_LOSS", bk.side))
        dt = self.feat.daily_trend                                   # against the daily trend: smaller units, never a veto (as live)
        bk.pos["unit_mult"] = bk.strat.p["against_daily_mult"] if dt and dt != ("up" if bk.s > 0 else "down") else 1.0
        working = {r: (w["px"], w["qty"] - w["filled"]) for r, w in bk.work.items() if w}
        d = bk.strat.step(f, sigs, bk.pos, working)
        self.reconcile(bk, d, f["t"])

    def reconcile(self, bk, d, t):
        qty, _ = pos_stats(bk.pos)
        if qty and d.get("no_stop"): self.on_stop_hit(bk, self.feat.f["mid"] * (1 - bk.s * SLIP)); return
        for role in ("buy", "trim"):
            want, w = d[role], bk.work[role]
            if want is not None and role == "trim" and want[2] == "taker":
                if w: bk.work[role] = None
                if t - bk.taker_t >= 5:
                    bk.taker_t = t; px = self.feat.bid if bk.s > 0 else self.feat.ask; bk.seq += 1
                    self.on_fill(bk, "trim", want[1], px, want[1] * px * TAKER, f"{OID}{bk.side[0]}m{bk.seq}", lot=want[3] if len(want) > 3 else None)
                continue
            if w and want is not None and abs(want[0] - w["px"]) < self.px_tick / 2 and abs(want[1] - (w["qty"] - w["filled"])) < self.qstep / 2: continue
            if w and (want is None or t - bk.replaced[role] >= 1.0): bk.work[role] = None; bk.replaced[role] = t; w = None
            if want is not None and w is None:
                lvl = dict(self.feat.bids if (role == "buy") == (bk.s > 0) else self.feat.asks); bk.seq += 1
                bk.work[role] = dict(oid=f"{OID}{bk.side[0]}{role[0]}{bk.seq}", px=want[0], qty=want[1], filled=0.0, queue=lvl.get(want[0], 0.0),
                                     t=self.feat.sec,          # placed at the start of this second (N): N's prints may cancel it (crossed), not fill it (sim_match CROSS_S)
                                     S=lvl.get(want[0], 0.0), seen=0.0,   # the level's size at placement and the prints seen since (sim_book: cancellations drain the queue)
                                     lot=want[3] if role == "trim" and len(want) > 3 else None); bk.replaced[role] = t
        qty, _ = pos_stats(bk.pos)
        bk.stop = d["stop"] if qty else None

    def sim_trades(self, trades, sec):
        """One print is shared by every resting order of every book at that price (sim_match, as cycle.py dry mode). A print through
        the price of an order placed this second is the exchange's post-only cancel (fill None): the order is dropped and the Strategy
        re-places at the new touch on its next tick, as live."""
        for px, size, side in trades:
            orders = [((bk, role), w, (role == "buy") == (bk.s > 0)) for bk in self.books.values() for role in ("buy", "trim") if (w := bk.work[role])]
            for (bk, role), w, fill in sim_match(orders, px, size, side, self.qstep, t=sec):
                if fill is None: bk.work[role] = None; self.events.append((sec, "CANCEL", bk.side, role, w["px"], "crossed")); continue
                self.on_fill(bk, role, fill, w["px"], fill * w["px"] * MAKER, w["oid"], lot=w.get("lot"))

    def sim_book(self, bids, asks, sec):
        """The second's book snapshot: the queue ahead of resting orders drains by the cancellations it shows; an opposite touch at our
        price cancels an order placed this second (crossed on arrival) or fills an older one (sim_book in signal.py)."""
        orders = [((bk, role), w, (role == "buy") == (bk.s > 0)) for bk in self.books.values() for role in ("buy", "trim") if (w := bk.work[role])]
        for (bk, role), w, fill in sim_book(orders, bids, asks, t=sec, qstep=self.qstep):
            if fill is None: bk.work[role] = None; self.events.append((sec, "CANCEL", bk.side, role, w["px"], "crossed")); continue
            self.on_fill(bk, role, fill, w["px"], fill * w["px"] * MAKER, w["oid"], lot=w.get("lot"))

    def run(self, seconds):
        """Per condensed second N, in live order: the first message of N closes N-1 -> the tick decides on N-1's features with N-1's
        book (queue snapshots, taker touch) -> N's prints hit the orders that existed at the start of N -> N's book arrives."""
        arg = {"instType": "USDT-FUTURES", "instId": "X"}
        for sec, bid, ask, bids, asks, mark, trades, rows in seconds:
            day = sec // 86400
            if self.day is not None and day != self.day:            # UTC day rollover, as live
                for bk in self.books.values():                      # same policy as live Book.day_close: fresh budget, daily halts lift
                    bk.stops_today, bk.day_realized = 0, 0.0
                    if bk.pos["halt"] in ("DAILY_STOPS", "DAILY_LOSS"): bk.pos["halt"] = None
            self.day = day
            ts = sec * 1000 + 500; fed_rows = False
            if rows and self.feat.sec is None:      # candle history must be in before the clock starts (ATR, warm-up of the 30-min window)
                self.feat.feed(dict(arg={**arg, "channel": "candle1m"}, data=rows, ts=ts)); fed_rows = True
            sigs = self.feat._clock(sec) if self.feat.mid is not None else []     # close N-1 on N-1's book and flow only (live: the first message of N does this)
            if self.follow: self.follow_step(sec)
            if sigs or self.feat.f.get("t") != self.last_t: self.last_t = self.feat.f.get("t"); self.tick(sigs)
            if trades:
                self.sim_trades(trades, sec)
                self.feat.feed(dict(arg={**arg, "channel": "trade"}, action="update", data=[dict(price=str(p), size=str(q), side=s) for p, q, s in trades], ts=ts))
            if bids and asks:
                self.sim_book(bids, asks, sec)                           # N's book: what the level lost beyond N's prints was cancelled; a crossed touch cancels (this second) or fills
                self.feat.feed(dict(arg={**arg, "channel": "books15"}, data=[dict(bids=bids, asks=asks, ts=str(ts))], ts=ts))
            if rows and not fed_rows: self.feat.feed(dict(arg={**arg, "channel": "candle1m"}, data=rows, ts=ts))
            if mark: self.feat.feed(dict(arg={**arg, "channel": "ticker"}, data=[dict(markPrice=str(mark))], ts=ts))
            f = self.feat.f; eq = 0.0; held = False
            for bk in self.books.values():
                qty, avg = pos_stats(bk.pos); bk.upl = bk.s * (f["mid"] - avg) * qty if qty and f.get("mid") else 0.0
                eq += bk.realized + bk.upl; held = held or bool(qty)
            self.peak = max(self.peak, eq); self.max_dd = max(self.max_dd, self.peak - eq)
            if f.get("mid") and (self.min_mid is None or sec - self.min_mid[0] >= 60):
                if self.min_mid is not None:
                    d = (f["mid"] - self.min_mid[1]) / self.min_mid[1] * 100; key = "up" if d > 0 else "dn"
                    for sd, bk in self.books.items():
                        c = self.cap.setdefault(sd, dict(up_all=0.0, up_held=0.0, dn_all=0.0, dn_held=0.0)); c[key + "_all"] += abs(d)
                        if pos_stats(bk.pos)[0]: c[key + "_held"] += abs(d)
                self.min_mid = (sec, f["mid"])
            hint = self.feat.side_hint or "none"
            for sd, bk in self.books.items():
                dr = bk.realized - self.last_real.get(sd, 0.0)
                if dr: self.last_real[sd] = bk.realized; self.by_hint[f"{sd}|{hint}"] = self.by_hint.get(f"{sd}|{hint}", 0.0) + dr
            self.n += 1; self.in_mkt += 1 if held else 0
        return self.metrics()

    def metrics(self):
        per = {}
        for sd, bk in self.books.items():
            qty, avg = pos_stats(bk.pos)
            per[sd] = dict(pnl=round(bk.realized, 3), open_pnl=round(bk.upl, 3), cycles=bk.cycles, adds=bk.adds, stops=bk.stops, end_qty=qty, end_avg=avg, halt=bk.pos["halt"])
        tot = sum(v["pnl"] for v in per.values()); opn = sum(v["open_pnl"] for v in per.values())
        return dict(pnl=round(tot, 3), open_pnl=round(opn, 3), total=round(tot + opn, 3), cycles=sum(v["cycles"] for v in per.values()),
                    adds=sum(v["adds"] for v in per.values()), stops=sum(v["stops"] for v in per.values()), max_dd=round(self.max_dd, 3),
                    in_mkt=round(self.in_mkt / max(self.n, 1), 3), seconds=self.n, sides=per, fills=sum(1 for e in self.events if e[1] == "FILL"),
                    by_hint={k: round(v, 3) for k, v in sorted(self.by_hint.items())},
                    follow=dict(hint=self.follow, flips=self.flips, share={k: round(v / max(self.n, 1), 3) for k, v in self.active_s.items()}) if self.follow else None,
                    capture={sd: dict(up=round(c['up_held'] / c['up_all'], 3) if c['up_all'] else 0.0, dn=round(c['dn_held'] / c['dn_all'], 3) if c['dn_all'] else 0.0) for sd, c in self.cap.items()})


def run_files(files, sym="TRUMPUSDT", sig=None, strat=None, events=False, qstep=None, sides=None, follow=None, equity=None, fixed=False):
    """params.json is the base as live sees it — `strat_for(params, sym)` (the book's wallet_frac / sides on the common strat) — with
    sig/strat overrides on top; sides default to strat.sides; follow="15m"|"1h" runs both sides with the structure hint choosing which
    one may trade (flat-only flips). qstep defaults to the symbol's contract step; equity-scaled params are frozen into fixed sizes at
    the first mid from `equity` (default: the latest state file's) unless `fixed`, or unless the override already fixes them."""
    p = load_params() or {}
    sig = {**(p.get("sig") or {}), **(sig or {})}; base = strat_for(p, sym)
    if p.get("books") and sym not in p["books"]:                   # not a book: size it as one of the basket's slots, not as the whole wallet
        base["wallet_frac"] = 1.0 / max(int((p.get("select") or {}).get("n") or len(p["books"])), 1)
    strat = {**base, **(strat or {})}
    if qstep is None: qstep = (contract_meta(sym) or {}).get("qstep", 0.1)
    loaded, first = {}, []
    for path in files:                                             # the first file that carries this symbol: a warm-up file recorded before the
        loaded[path] = load_seconds(path, sym)                     # symbol joined the recording is empty for it (2026-09-01: ETH/XAG sized at the file's
        if loaded[path]: first = loaded[path]; break               # unit 70 = $170k and seeded nothing because only files[0] was looked at)
    seed = seed_history(sym, first[0][0]) if first else (None, None, None)   # before sizing: cap_min_atr needs the seeded ATR, as live
    if not fixed and any(strat.get(f) for f in SIZED.values()):
        equity = equity if equity is not None else latest_equity()
        mid = next(((b + a) / 2 for _, b, a, *_ in first if b and a), None)
        atr = wilder_atr(seed[0][-100:]) if seed[0] else None
        if equity and mid: strat = size_from_equity(strat, equity, mid, qstep, atr)
        else: print(f"sizing: no equity ({equity}) or no quote on the tape; the file's fixed sizes apply", file=sys.stderr)
    eng = Engine(sig, strat, qstep=qstep, sides=["long", "short"] if follow else sides, follow=follow)
    if first: eng.seed(*seed)
    for path in files: eng.run(loaded[path] if path in loaded else load_seconds(path, sym))
    m = eng.metrics()
    m["sizing"] = dict(qstep=qstep, equity=None if fixed else equity, **{k: strat.get(k) for k in SIZED})
    if events: m["events"] = eng.events
    return m

if __name__ == "__main__":
    args = sys.argv[1:]; files, sym, sig, strat, sides, follow, qstep, equity, fixed = [], "TRUMPUSDT", {}, {}, None, None, None, None, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--sym": sym = args[i + 1]; i += 2
        elif a == "--qstep": qstep = float(args[i + 1]); i += 2      # overrides the contract's quantity step
        elif a == "--equity": equity = float(args[i + 1]); i += 2    # the wallet the equity-scaled sizes are frozen from (default: the latest state file)
        elif a == "--fixed": fixed = True; i += 1                     # the file's unit_qty / cap_usdt / limits, whatever the fractions say (reference numbers)
        elif a == "--sides": sides = args[i + 1].split(","); i += 2
        elif a == "--follow": follow = args[i + 1]; i += 2
        elif a in ("--sig", "--strat"):
            k, v = args[i + 1].split("="); (sig if a == "--sig" else strat)[k] = float(v) if v.replace(".", "", 1).replace("-", "", 1).isdigit() else v; i += 2
        else: files.append(a); i += 1
    if not files: print(__doc__); sys.exit(0)
    t0 = time.time(); m = run_files(files, sym, sig, strat, events=True, qstep=qstep, sides=sides, follow=follow, equity=equity, fixed=fixed); ev = m.pop("events")
    for e in ev[-12:]: print("  ", time.strftime("%m-%d %H:%M:%S", time.gmtime(e[0] or 0)), *e[1:])
    print(json.dumps(m), f"({time.time() - t0:.0f}s)")
