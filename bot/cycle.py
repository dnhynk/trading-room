"""순환매 harness.  python -m bot.cycle   (kept alive by: python -m bot.supervise cycle)
params.json (hot-reloaded every second): "strat" = symbol, sides (["long"], ["short"] or ["long","short"] = 쌍검술; falls back to
"side"), mode dry|live, sizes, steps, cap, daily_loss_limit, adopt; "sig" = signal thresholds. Key meanings: bot/signal.py.
A change of symbol/sides/mode or of a stateful signal window exits the process (the supervisor restarts it on the new contract);
every other key applies live. With two sides the fixed and fraction budgets (unit, cap, daily limit, notional) are split per side.
One feature stream (bot/signal.py Features) feeds one Book per side; each Book has its own Strategy, position, orders, stop, daily
counters, clientOid prefix (cycL- / cycS-) and OS lock; in hedge mode the exchange keeps the two positions and stops apart.
Every exchange second: features -> Strategy.step -> reconcile the exchange (live) or the simulated book (dry) to the desired set:
one entry order resting at the touch while armed, one LIFO trim, one position stop (pos_loss, mark price). Fills come from the
private `fill` channel (live; every push is a "snapshot", deduped by tradeId) or from public trades crossing our price / exhausting
the queue ahead of us (dry); each fill re-runs the step immediately, so the opposite side is re-placed at once.
logs/: state.json (atomic snapshot, every 5s and on change; per-side under "books"), events.jsonl (everything), alerts.jsonl (HALT,
STOP_HIT, DAILY_LOSS, WS_DOWN/WS_UP, EXTERNAL_FILL, STOP_FAILED, STOP_MODIFY_FAIL, STOP_THROUGH, EMERGENCY_CLOSE, MARGIN_LOCKED,
REGIME_CHANGE, SIDE_HINT, PARAMS_INVALID,
PARAMS_DEFERRED, STATE_DISCARDED, EMERGENCY_CANCEL_UNCONFIRMED, TAKER_UNCONFIRMED, ERROR, EXIT) for the agent's Monitor. Control files
in the repo root: STOP (cancel our orders, exit; supervisor stays down while it exists), PAUSE (no new entries while present; trims and
stops stay), RESUME (clears HALTs; deleted once applied). `books[sym].wind_down` is the same as PAUSE for one symbol — bot/select.py
sets it on a book that lost its eligibility, and drops the key once this engine reports flat, which exits the engine too.
A symbol/sides/mode or signal-window change while a live book holds a
position or an order is deferred (PARAMS_DEFERRED) until every book is flat; a mode switch discards the previous mode's book state.
Live mode owns each (symbol, side) exclusively: a position size that differs from our lots for 10s -> HALT (UNOWNED_POSITION when we
hold nothing, else EXTERNAL_FILL). A close fill the book did not order is a stop hit only when its clientOid is one of our stop plan
orderIds (how Bitget fills a triggered plan); otherwise it waits up to 15s for the algo push before it is called external. A live
position gets its exchange stop at resync and every second it has none (fallback: the persisted stop or the money cap). Live orders
are placed only while the private feed is up (all five channels acknowledged); a silent public feed cancels the resting entry. Orders
whose response was lost stay tracked until the exchange settles them (limit: by clientOid; market: no second one until known).
An existing stop is never moved against the position; a stop the market has already passed (40917) or a position without a valid
stop level is market-closed (after the resting orders are confirmed gone) and booked as a stop hit; one stop order counts once."""
import asyncio, glob, json, os, sys, time
from collections import deque
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import from_env, BitgetError
from bot.signal import Features, Strategy, STRAT, SIG, apply_fill, pos_stats, book_params, sim_match, sim_book, unit_under_cap
from bot.ws import WS, PUB_URL, PRV_URL, PRIVATE_ARGS, INST, load_params, PARAMS, strat_for, portfolio, outside_books

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
STATE, EVENTS, ALERTS = (os.path.join(LOGS, f) for f in ("state.json", "events.jsonl", "alerts.jsonl"))
PUB_CH = ("trade", "books15", "candle1m", "ticker")
ALERT = {"HALT", "STOP_HIT", "DAILY_LOSS", "WS_DOWN", "WS_UP", "EXTERNAL_FILL", "STOP_FAILED", "STOP_MODIFY_FAIL", "STOP_THROUGH", "EMERGENCY_CLOSE", "MARGIN_LOCKED", "MARGIN_MODE_MISMATCH", "REGIME_CHANGE", "SIDE_HINT","PARAMS_INVALID",
         "PARAMS_DEFERRED", "STATE_DISCARDED", "EMERGENCY_CANCEL_UNCONFIRMED", "TAKER_UNCONFIRMED", "ERROR", "EXIT"}
QUIET = {"PLACE", "CANCEL", "REPLACE", "ARM", "DISARM", "SKIP", "PULL_TRIM", "WS", "STOP_LIQ_GUARD"}   # events.jsonl only, not stdout
GONE = ("not exist", "does not exist", "already", "finished", "completed")            # exchange says the order is terminal (never a bare "cancel")
SLIP = 0.0005          # dry-mode stop-out slippage


def rnd(x): return round(x, 6) if isinstance(x, float) else x

def quantize_unit(tgt, cur, qstep):
    """Round a unit target to whole exchange steps, with a half-step dead zone around the unit we already hold.
    Plain rounding flaps when one qstep is a large fraction of the unit: ETHUSDT 2026-09-01 sat at 5.5 steps
    (target 0.0547, qstep 0.01) and SIZING rewrote 0.05 / 0.06 / 0.05 / 0.06 every minute — a 20% swing in unit size
    and a Monitor line each time. The dead zone (3/4 of a step either way, so half a step wide once rounding is
    accounted for) is a mechanism constant, not a tunable: anything in (0.5, 1.0) removes the oscillation."""
    u = max(round(tgt / qstep) * qstep, qstep)
    if cur and abs(tgt - cur) < 0.75 * qstep: return cur
    return u

POSITIVE = dict(strat=("unit_qty", "max_units", "max_notional", "cap_usdt", "pop_min_pct", "tick", "qstep", "buy_ttl_s", "confirm_within_s", "wallet_frac"),
                sig=("vol_hl", "v_hl", "a_lag", "swing_s", "brk_lookback", "depth_levels", "rg_window", "vp_window", "vp_bucket_ticks", "stop_lookback", "s8_h"))
OPTIONAL_NUM = ("stop_structural", "add_confirm", "unit_frac", "cap_frac", "daily_loss_frac", "notional_frac", "daily_loss_limit", "fee_rt_pct")   # None or a non-negative number
# 자본 비례 한도: 고정 키 -> 그 키를 대신하는 지갑 배수. resize·load·apply_params·snapshot이 전부 여기서 읽는다 —
# 같은 대응을 여러 곳에 적어두면 하나가 뒤처진다(2026-09-01 max_notional이 그렇게 낡았다). 새 한도는 여기만 더한다.
SIZED = {"unit_qty": "unit_frac", "cap_usdt": "cap_frac", "daily_loss_limit": "daily_loss_frac", "max_notional": "notional_frac"}
TYPED = {"symbol": lambda v: isinstance(v, str) and v.endswith("USDT"), "side": lambda v: v in ("long", "short"),
         "sides": lambda v: v is None or (isinstance(v, list) and bool(v) and all(x in ("long", "short") for x in v)),
         "mode": lambda v: v in (None, "dry", "live"), "adopt": lambda v: v is None or isinstance(v, bool),
         "margin_mode": lambda v: v in (None, "crossed", "isolated")}
STATEFUL = ("vol_hl", "v_hl", "a_lag", "swing_s", "brk_lookback", "depth_levels")   # signal windows built at start: a change restarts the process

def valid_params(sp, sig):
    """Every key must carry the type its use needs: numbers (bool excluded) non-negative, POSITIVE windows/divisors > 0, OPTIONAL_NUM
    None or a number, strings/bools per TYPED; a key the defaults do not know must be a number. Checked at start and on every hot reload."""
    bad = []
    for defaults, given, pos in ((STRAT, sp, POSITIVE["strat"]), (SIG, sig, POSITIVE["sig"])):
        for k in set(defaults) | set(given):
            dv = defaults.get(k); v = given.get(k, dv)
            if k in TYPED:
                if not TYPED[k](v): bad.append(k)
            elif k in OPTIONAL_NUM:
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0): bad.append(k)
            elif isinstance(dv, bool):
                if not isinstance(v, bool) and v not in (0, 1): bad.append(k)
            elif isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (k in pos and v <= 0): bad.append(k)
    return sorted(set(bad))


class Book:
    """One side of the engine: Strategy + position + orders + stop + daily counters. Shares the parent's features, REST and feeds."""
    def __init__(self, cy, side):
        self.cy, self.side, self.s = cy, side, (1 if side == "long" else -1)
        self.OIDP = f"cyc{'L' if self.s > 0 else 'S'}-"
        self.sp = book_params(cy.sp, side, cy.px_tick, len(cy.sides), cy.qstep, fee_rt=(cy.maker + cy.taker) * 100); self.dyn, self.sized_t = {}, 0.0
        if self.sp.get("add_confirm") is None: self.sp["add_confirm"] = 1 if len(cy.sides) > 1 else 0
        self.strat = Strategy(self.sp, cy.feat.p)
        self.pos = dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None, halt=None, pause=False)
        self.realized, self.stops_today = 0.0, 0
        self.work, self.replaced = dict(buy=None, trim=None), dict(buy=0.0, trim=0.0)
        self.stop, self.stop_fail, self.stop_trig_t, self.preset_plan, self.preset_retry_t = None, 0, 0.0, None, 0.0
        self.exch = dict(total=0.0, avg=None, upl=0.0, mark=None)
        self.mismatch_since, self.lever, self.margin_alert_t, self.taker_t, self.seq = None, None, 0.0, 0.0, 0
        self.stop_oids, self.stop_hit_oids, self.unmatched_close = deque(maxlen=20), deque(maxlen=20), []   # known stop plan ids; stop orders already counted; close fills awaiting identity
        self.market_pending, self.stop_try_t, self.guarded = None, 0.0, None   # a market order whose response was lost (no second one until settled); fallback-stop throttle; last liq guard
        self.trim_lot = {}                                      # trim clientOid -> the lot its fills reduce (0 = the core lot, a de-risk cut under units; None = LIFO)
        self._stop_lock = asyncio.Lock()                        # set_stop is find-then-place: two callers (a fill's reconcile, housekeeping's ensure_stop) must not both find nothing
        self._lock = self.acquire_lock()

    def acquire_lock(self):
        """OS-level exclusive lock on logs/cycle-<symbol>-<side>.lock; released automatically when the process dies."""
        path = os.path.join(LOGS, f"cycle-{self.cy.symbol}-{self.side}.lock")
        fh = open(path, "a+")
        try:
            import msvcrt; fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl; fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(json.dumps(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), ev="EXIT", why=f"another engine holds {path}")), flush=True); os._exit(3)
        fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
        return fh

    # ---- helpers -------------------------------------------------------------
    def ev(self, kind, **kw): self.cy.ev(kind, side=self.side, **kw)
    def fpx(self, x): return self.cy.fpx(x)
    def fq(self, q): return self.cy.fq(q)
    def rest_on_bid(self, role): return (role == "buy") == (self.s > 0)
    @property
    def feat(self): return self.cy.feat
    @property
    def mode(self): return self.cy.mode
    @property
    def symbol(self): return self.cy.symbol

    def halt(self, why, **kw):
        if self.pos["halt"] == why: return
        self.pos["halt"] = why; self.ev("HALT", why=why, **kw)

    # ---- persistence ----------------------------------------------------------
    def load(self, st):
        """Restore from state.json: the per-side block (books.<side>) or the pre-refactor flat layout when it was this side."""
        b = (st.get("books") or {}).get(self.side)
        if b is None and st.get("side") == self.side and "pos" in st: b = st
        if b is None: return
        if st.get("mode") != self.mode:                        # a mode switch starts a fresh ledger: nothing simulated, counted or halted crosses over
            self.ev("STATE_DISCARDED", was=st.get("mode"), lots=b["pos"].get("lots"), realized=b.get("realized"), halt=b["pos"].get("halt")); return
        self.pos.update({k: b["pos"][k] for k in ("lots", "avg", "last", "last_buy_px", "last_trim_px", "halt") if k in b["pos"]})
        if st.get("day") == self.cy.day: self.realized, self.stops_today = b.get("realized", 0.0), b.get("stops_today", 0)
        elif self.pos["halt"] in ("DAILY_STOPS", "DAILY_LOSS"): self.pos["halt"] = None            # a new UTC day lifts yesterday's daily halts, restart or not
        if self.pos["lots"] and b.get("struct_stop"): self.strat.struct_stop = b["struct_stop"]   # frozen at open; a restart must not re-freeze it
        if self.pos["lots"] and b.get("stop"): self.strat.stop_px = b["stop"]["px"]                # and the stop already set can only tighten
        if self.pos["lots"] and b.get("stop") and b["stop"].get("order_id") and self.mode == "live": self.stop = b["stop"]   # its fills are recognised at once; resync confirms it
        if b.get("cooldown_until"): self.pos["cooldown_until"] = b["cooldown_until"]              # the post-stop cooldown survives a restart
        if b.get("preset_plan") and self.mode == "live": self.preset_plan = b["preset_plan"]     # the entry's preset stop still to be dropped
        self.dyn = {k: v for k, v in (b.get("sizing") or {}).items() if k in SIZED and self.sp.get(SIZED[k])}   # equity-scaled sizes survive a restart (a position keeps its budget)
        if self.dyn: self.sp.update(self.dyn); self.strat.p = self.sp

    def snapshot(self):
        qty, avg = pos_stats(self.pos); f = self.feat.f
        upl = self.exch["upl"] if self.mode == "live" else (self.s * (f["mid"] - avg) * qty if qty and f.get("mid") else 0.0)
        return dict(pos=dict(lots=self.pos["lots"], qty=rnd(qty), avg=rnd(avg), upl=rnd(upl), last=self.pos["last"], last_buy_px=self.pos["last_buy_px"],
                             last_trim_px=self.pos["last_trim_px"], halt=self.pos["halt"], pause=self.pos["pause"]),
                    realized=rnd(self.realized), working={r: (w and dict(px=w["px"], qty=w["qty"], filled=w["filled"], oid=w["oid"])) for r, w in self.work.items()},
                    stop=self.stop, struct_stop=self.strat.struct_stop, stops_today=self.stops_today, cooldown_until=self.pos.get("cooldown_until"), preset_plan=self.preset_plan,
                    exch=self.exch, lever=self.lever, sizing=self.dyn, arm=self.strat.arm, pull=self.strat.pull, regime=self.strat.regime,
                    **{k: self.sp.get(k) for k in SIZED},   # 자본 비례 한도는 전부 실효값으로 — params 절은 파일 값이고 sizing은 덮어쓴 것만 담는다
                    unit_mult=self.pos.get("unit_mult", 1.0),
                    fail_n=self.strat.fail_n, gate_eff=self.strat.gate_eff, add_confirm=self.sp.get("add_confirm"))

    # ---- per-second step --------------------------------------------------------
    async def tick(self, sigs):
        f = self.feat.f
        if not f.get("atr") or f.get("bid") is None: return
        self.pos["pause"] = (os.path.exists(os.path.join(ROOT, "PAUSE")) or bool(self.sp.get("wind_down"))   # wind_down = 이 심볼만의 PAUSE (select가 자격 잃은 책을 flat 으로 몬다)
                             or bool(self.unmatched_close))   # 정체불명 close 를 분류하는 15초 동안은 담지 않는다 — 그 사이 담은 로트를 나중에 손절 수량이 LIFO 로 지운다
        self.pos["avail"], self.pos["lever"] = (self.cy.acct["avail"] if self.mode == "live" else None), self.lever
        dt = self.feat.daily_trend                                   # against the daily trend: smaller units, never a veto
        self.pos["unit_mult"] = self.sp["against_daily_mult"] if dt and dt != ("up" if self.s > 0 else "down") else 1.0
        if self.mode == "dry": await self.sim_stop(f)
        self.check_daily(f)
        working = {r: (w["px"], w["qty"] - w["filled"]) for r, w in self.work.items() if w}
        d = self.strat.step(f, sigs, self.pos, working)
        for kind, kw in d["events"]:
            self.ev(kind, **kw)
            if kind == "SKIP" and kw.get("why") == "margin" and time.time() - self.margin_alert_t >= 600:
                self.margin_alert_t = time.time(); self.ev("MARGIN_LOCKED", avail=kw.get("avail"), lever=self.lever, unit_qty=self.sp["unit_qty"], mid=f["mid"])
        await self.reconcile(d)

    def check_daily(self, f):
        qty, avg = pos_stats(self.pos)
        upl = self.exch["upl"] if self.mode == "live" else (self.s * (f["mid"] - avg) * qty if qty else 0.0)
        lim = self.sp.get("daily_loss_limit")
        if lim and self.realized + upl <= -lim and not self.pos["halt"]:
            self.ev("DAILY_LOSS", realized=rnd(self.realized), upl=rnd(upl), limit=lim); self.halt("DAILY_LOSS")

    def day_close(self, day):
        """UTC rollover: a new day gets a fresh loss budget and stop count; daily halts lift (same policy as the backtest)."""
        self.ev("DAY_CLOSE", day=day, realized=rnd(self.realized), stops=self.stops_today, halt=self.pos["halt"]); self.realized, self.stops_today = 0.0, 0
        if self.pos["halt"] in ("DAILY_STOPS", "DAILY_LOSS"): self.pos["halt"] = None

    def apply_params(self, sp, gone=False):
        """New file params: fractions switched off return their keys to the file's fixed values; dynamic overrides outrank the file.
        gone = this symbol left params.books: strat_for then falls back to the common defaults (wallet_frac 1.0, wind_down off), so while
        the engine waits for flat it keeps its wallet share and never adds again — a removed book only winds down."""
        for fixed, frac in SIZED.items():
            if not sp.get(frac): self.dyn.pop(fixed, None)
        keep = dict(wallet_frac=self.sp.get("wallet_frac", 1.0), wind_down=True) if gone else {}
        self.sp = {**book_params(sp, self.side, self.cy.px_tick, len(self.cy.sides), self.cy.qstep, fee_rt=(self.cy.maker + self.cy.taker) * 100), **self.dyn, **keep}
        if self.sp.get("add_confirm") is None: self.sp["add_confirm"] = 1 if len(self.cy.sides) > 1 else 0   # two books: each book's add is the other's trim -> higher bar
        self.strat.p = self.sp

    def resize(self):
        """Equity-scaled sizing (compounding, and de-leveraging after losses): unit notional = wallet x unit_frac, cap = wallet x cap_frac,
        daily limit = wallet x daily_loss_frac, position notional cap = wallet x notional_frac (fractions already split per side). Wallet =
        account equity minus unrealized pnl; recomputed at most every 60s, moving the unit at most 25% per step. With a position only
        REDUCTIONS of the unit, the notional cap and the daily limit apply (a lowered profile, or a wallet shrunk by realized losses,
        stops the ladder at once: fewer adds allowed, smaller ones); increases wait for flat, and the money cap is the campaign's — it is
        never re-derived under an open position (the exchange stop tightens only by its own rules). 2026-09-03: the hunt profile was cut
        4/0.77/16 -> 2/0.4/8 at 20:08 under a 2-unit EGLD long that never went flat, so the old 4x unit kept adding into the slide
        (20:21, -3.9). Fixed params are the fallback — a fixed cap next to a scaling unit goes stale as the wallet compounds and starves the book."""
        self.sized_t = time.time()
        if not any(self.sp.get(k) for k in SIZED.values()) or self.cy.acct["equity"] is None: return
        qty, _ = pos_stats(self.pos); mid = self.feat.f.get("mid")
        if not mid: return
        # 지갑은 계좌 전체다 — 엔진이 여럿이면 각자 자기 몫만 써야 한다(안 나누면 심볼 수만큼 노출이 배가 된다)
        wallet = (self.cy.acct["equity"] - (self.cy.acct["upl_all"] or 0.0)) * self.sp.get("wallet_frac", 1.0); new = {}
        if wallet <= 0: return                                    # an empty wallet sizes nothing and is no reference for the next resize (2026-09-03: a first SIZING at
        if self.sp.get("unit_frac"):                              # wallet 0 pinned the unit to the qstep floor, and the damp could never lift it: 0.1 x 1.25 quantizes back to 0.1)
            tgt = wallet * self.sp["unit_frac"] / mid             # 양자화 전 목표
            cap = wallet * self.sp["cap_frac"] if self.sp.get("cap_frac") else self.sp.get("cap_usdt")
            tgt = unit_under_cap(tgt, cap, self.feat.f.get("atr"), self.sp.get("cap_min_atr"))   # 돈 한도는 그대로, 유닛이 줄어 한도가 ≥ k ATR 아래에
            cur = self.dyn.get("unit_qty"); step = self.cy.qstep or 0.0   # 이전 동적 값이 있을 때만 damp 한다. 파일의 unit_qty 는 심볼별 계약수라
            if cur and cur > step:                                # 다른 심볼로 새로 뜬 엔진의 기준이 못 된다(ZECUSDT 841$ 에 TRUMP 기준 70 이 걸려 60배 유닛); a unit AT the
                tgt = max(min(tgt, max(cur * 1.25, cur + step)), max(min(cur * 0.75, cur - step), step))   # floor is no reference either. The band is at least one qstep
            new["unit_qty"] = round(quantize_unit(tgt, cur, self.cy.qstep), self.cy.vp)                    # wide: x1.25 of a few steps quantizes back to where it was
        if self.sp.get("cap_frac"): new["cap_usdt"] = round(wallet * self.sp["cap_frac"], 2)
        if self.sp.get("daily_loss_frac"): new["daily_loss_limit"] = round(wallet * self.sp["daily_loss_frac"], 2)
        if self.sp.get("notional_frac"): new["max_notional"] = round(wallet * self.sp["notional_frac"], 2)
        if qty: new = {k: v for k, v in new.items() if k != "cap_usdt" and self.sp.get(k) is not None and v < self.sp[k]}   # positioned: reductions only, never the cap
        if any(abs(self.sp.get(k, 0) - v) > 0.02 * max(abs(v), 1e-9) for k, v in new.items()):   # only moves of >= 2%: no per-minute jitter
            self.dyn.update(new); self.sp.update(new); self.strat.p = self.sp
            atr, uq, cap = self.feat.f.get("atr"), self.sp.get("unit_qty"), self.sp.get("cap_usdt")
            self.ev("SIZING", wallet=round(wallet, 2), mid=mid, atr=atr, cap_atr=round(cap / (uq * atr), 1) if atr and uq and cap else None, **new)   # cap_atr = 1유닛 기준 한도의 ATR 거리

    # ---- order management ---------------------------------------------------------
    async def reconcile(self, d):
        qty, _ = pos_stats(self.pos)
        if qty and d.get("no_stop"):                        # no valid stop can exist (price already beyond the cap): the position ends now, before any new order
            if self.mode == "live": self.ev("STOP_THROUGH", why="no valid stop level"); await self.emergency_close("no valid stop level")
            else: await self.on_stop_hit(self.feat.f["mid"] * (1 - self.s * SLIP))
            return
        for role in ("buy", "trim"):
            want, w = d[role], self.work[role]
            if w and (w.get("cancel_pending") or w.get("unconfirmed") or w.get("settling")): continue    # its exchange state is being settled (a cancel, a lost submission, or fills still in flight); no replacement until then
            if want is not None and role == "trim" and want[2] == "taker":
                if w: await self.cancel(role); w = self.work[role]
                if w or self.market_pending: continue            # the maker order rests until its cancel is confirmed, and a market order with a lost response is settled first
                if want[1] < self.cy.qstep - 1e-9: continue                               # below one exchange step: nothing the exchange can fill (the Strategy quantises pulls; this is the backstop)
                if time.time() - self.taker_t >= 5: await self.taker(want[1], want[3] if len(want) > 3 else None)
                continue
            if w and want is not None and abs(want[0] - w["px"]) < self.cy.px_tick / 2 and abs(want[1] - (w["qty"] - w["filled"])) < self.cy.qstep / 2: continue
            if w and (want is None or time.time() - self.replaced[role] >= 1.0):
                await self.cancel(role); w = self.work[role]
            if want is not None and w is None: await self.place(role, want[0], want[1], want[3] if role == "trim" and len(want) > 3 else None)
        if qty and d["stop"] is not None:
            px = self.guard(d["stop"], pos_stats(self.pos)[1])
            if self.stop is None or abs(px - self.stop["px"]) >= self.cy.px_tick / 2:
                await self.set_stop(px)
                if px != d["stop"] and self.stop: self.strat.adopt_stop(self.stop["px"])   # the guarded level is the stop from now on (never loosened back)
        if not qty: self.stop = None

    def remember_lot(self, oid, lot):
        """Which lot a trim's fills reduce (cycles and the ledger read it back as FILL.lot); the map stays small."""
        if lot is None: return
        self.trim_lot[oid] = lot
        for k in list(self.trim_lot)[:-50]: del self.trim_lot[k]

    async def place(self, role, px, qty, lot=None):
        if not self.cy.live_ok(): self.ev("ORDER_BLOCKED", role=role, why="private feed down"); return
        self.seq += 1; oid = f"{self.OIDP}{role[0]}{int(time.time() * 1000)}{self.seq % 1000:03d}"
        w = dict(oid=oid, order_id=None, px=px, qty=qty, filled=0.0, t=time.time()); self.remember_lot(oid, lot)
        lvl = dict(self.feat.bids if self.rest_on_bid(role) else self.feat.asks)
        w["queue"] = w["S"] = lvl.get(px, 0.0); w["seen"] = 0.0   # contracts already resting at our price (dry: the fill model's queue, S/seen for sim_book; live: the record — a miss is a queue that did not drain, not a price that did not come)
        if self.mode != "dry":
            try:
                sl = None
                if role == "buy" and not self.pos["lots"]:      # first unit: the order carries the stop so the fill is protected from its first millisecond
                    sl = self.fpx(self.guard(px - self.s * self.sp["cap_usdt"] / qty, px))   # the money cap, as the Strategy's exchange stop (the premise level is soft)
                r = await self.cy.rest(self.cy.b.limit_order, self.symbol, "buy" if self.s > 0 else "sell", self.fpx(px), self.fq(qty),
                                       trade_side="open" if role == "buy" else "close", post_only=True, client_oid=oid, sl=sl)
                w["order_id"] = r.get("orderId"); w["preset_sl"] = sl
            except BitgetError as e:
                self.ev("REJECT", role=role, px=px, qty=qty, err=str(e)[:160]); return
            except Exception as e:                           # timeout etc.: the exchange may have accepted it — track it and settle by clientOid
                w["unconfirmed"] = time.time(); self.ev("PLACE_UNCONFIRMED", role=role, px=px, qty=qty, oid=oid, err=f"{type(e).__name__}: {str(e)[:120]}")
        self.work[role] = w; self.replaced[role] = time.time()
        self.ev("PLACE", role=role, px=px, qty=qty, oid=oid, queue=w.get("queue"), mid=self.feat.mid)   # mid = arrival price (slippage / impact measurement joins FILL by oid)

    async def taker(self, qty, lot=None):
        """Reduce by qty at market (the speed-based trim that a maker order did not fill in time); lot = the lot it reduces (core cut)."""
        if not self.cy.live_ok(): self.ev("ORDER_BLOCKED", role="taker", why="private feed down"); return
        oid = f"{self.OIDP}m{int(time.time() * 1000)}"; self.taker_t = time.time(); self.remember_lot(oid, lot)
        if self.mode == "dry":
            px = self.feat.bid if self.s > 0 else self.feat.ask
            self.ev("TAKER", qty=qty, px=px, oid=oid, mid=self.feat.mid)
            await self.on_fill("trim", qty, px, qty * px * self.cy.taker, oid, "taker", lot=lot); return
        try:
            await self.cy.rest(self.cy.b.market_order, self.symbol, "buy" if self.s > 0 else "sell", self.fq(qty), trade_side="close", client_oid=oid)
            self.ev("TAKER", qty=qty, oid=oid, mid=self.feat.mid)
        except BitgetError as e: self.ev("REJECT", role="taker", qty=qty, err=str(e)[:160])
        except Exception as e:                                # timeout: it may have executed — no second market order until its state is known (settle_orders)
            self.market_pending = dict(oid=oid, t=time.time(), qty=qty); self.ev("TAKER_UNCONFIRMED", qty=qty, oid=oid, err=f"{type(e).__name__}: {str(e)[:120]}")

    async def cancel(self, role):
        w = self.work[role]
        if not w: return
        if self.mode == "live" and not w["order_id"]:          # submission still unconfirmed: nothing to cancel by id yet — settle_orders finds it (then cancels) or drops it
            if not w.get("cancel_wanted"): w["cancel_wanted"] = True; self.ev("CANCEL_DEFERRED", role=role, oid=w["oid"])
            self.replaced[role] = time.time(); return
        if self.mode == "live" and w["order_id"]:
            try: await self.cy.rest(self.cy.b.cancel_order, self.symbol, w["order_id"])
            except Exception as e:
                gone = any(k in str(e).lower() for k in GONE)
                w["cancel_fail"] = w.get("cancel_fail", 0) + 1
                self.ev("CANCEL_FAIL", role=role, oid=w["oid"], n=w["cancel_fail"], gone=gone, err=str(e)[:160])
                if gone: w["settling"], w["settling_t"], w["cancel_pending"] = w["qty"], time.time(), None   # finished on the exchange: its fills (if any) are still in flight —
                #                                                        hold the slot until they are booked (or the resync lets it go after 10 s), never re-order the remainder now
                elif w["cancel_fail"] >= 5: self.ev("ERROR", where="cancel", msg=f"{role} order {w['oid']} cannot be cancelled; still tracked")
                return                                                 # otherwise keep tracking it and retry on the next tick
            w["cancel_pending"] = time.time(); self.replaced[role] = time.time()     # keep it until the exchange confirms (orders channel / order_detail)
            self.ev("CANCEL", role=role, px=w["px"], filled=w["filled"], oid=w["oid"], pending=True); return
        self.work[role] = None; self.replaced[role] = time.time()
        self.ev("CANCEL", role=role, px=w["px"], filled=w["filled"], oid=w["oid"])

    async def settle_orders(self):
        """Live housekeeping: resolve orders whose exchange state is unknown — cancels awaiting confirmation and submissions that timed out."""
        for role in ("buy", "trim"):
            w = self.work[role]
            if not w: continue
            if w.get("unconfirmed") and not w["order_id"] and time.time() - w["unconfirmed"] >= 5:
                try:
                    pend = [o for o in (await self.cy.rest(self.cy.b.pending_orders, self.symbol)).get("entrustedList") or [] if o.get("clientOid") == w["oid"]]
                    if pend:
                        w["order_id"], w["unconfirmed"] = pend[0]["orderId"], None; self.ev("PLACE_CONFIRMED", role=role, oid=w["oid"])
                        if w.get("cancel_wanted"): await self.cancel(role)          # the cancel that had to wait for an id
                    elif time.time() - w["unconfirmed"] >= 15: self.work[role] = None; self.ev("PLACE_DROPPED", role=role, oid=w["oid"])   # never reached the book (fills, if any, came by clientOid)
                except Exception as e: self.cy.err("settle place", e)
            elif w.get("cancel_pending") and time.time() - w["cancel_pending"] >= 5:
                try:
                    o = await self.cy.rest(self.cy.b.order_detail, self.symbol, w["order_id"]); st = (o or {}).get("status")
                    if st in ("cancelled", "canceled", "filled"): self.work[role] = None; self.ev("CANCEL_CONFIRMED", role=role, oid=w["oid"], status=st)
                    else: w["cancel_pending"] = None; self.ev("CANCEL_RETRY", role=role, oid=w["oid"], status=st)   # still live: the next tick cancels again
                except Exception as e: self.cy.err("settle cancel", e)
        mp = self.market_pending
        if mp and time.time() - mp["t"] >= 3:                  # a market order with a lost response: ask by clientOid before any other market order
            try:
                o = await self.cy.rest(self.cy.b.order_detail, self.symbol, client_oid=mp["oid"])
                self.market_pending = None; self.ev("TAKER_SETTLED", oid=mp["oid"], status=(o or {}).get("status"))   # its fills come by the fill channel
            except Exception as e:
                if any(k in str(e).lower() for k in GONE): self.market_pending = None; self.ev("TAKER_SETTLED", oid=mp["oid"], status="never reached the exchange")
                elif time.time() - mp["t"] >= 30: self.market_pending = None; self.ev("ERROR", where="taker settle", msg=f"{mp['oid']} unresolved after 30s: {str(e)[:120]}")

    async def set_stop(self, px):
        async with self._stop_lock: await self._set_stop(px)   # one caller at a time: find-then-place is not atomic (a fill's reconcile and ensure_stop both call it)

    async def _set_stop(self, px):
        if self.mode == "dry":
            self.stop = dict(px=px, order_id=None); self.ev("STOP_SET", px=px); return
        if self.stop and self.stop.get("order_id"):
            # an existing stop is never cancelled by the script: if modify fails (any error) the old, valid stop stays and the agent is told;
            # modify failures never count toward the no-stop escalation
            try:
                await self.cy.rest(self.cy.b.modify_pos_tpsl, self.symbol, self.stop["order_id"], self.fpx(px), self.side)
                self.stop["px"] = px; self.ev("STOP_SET", px=px, order_id=self.stop["order_id"])
            except Exception as e:
                if isinstance(e, BitgetError) and str(e.code) == "40917":
                    self.ev("STOP_THROUGH", px=px, err=str(e)[:120]); await self.emergency_close("stop level already passed"); return
                self.modify_fail = getattr(self, "modify_fail", 0) + 1
                self.ev("STOP_MODIFY_FAIL", px=px, keep=self.stop["px"], n=self.modify_fail, err=f"{type(e).__name__}: {str(e)[:140]}")
            return
        try:                                 # no stop at all: placement failures (including network errors) escalate to the emergency close
            ex = await self.find_pos_loss()                       # never a second pos_loss: one the book lost track of is adopted (then moved)
            if ex:
                self.stop, self.stop_fail = ex, 0; self.ev("ADOPT_STOP", px=ex["px"], order_id=ex["order_id"], via="set_stop")
                if abs(ex["px"] - px) >= self.cy.px_tick / 2: await self._set_stop(px)
                return
            r = await self.cy.rest(self.cy.b.place_pos_tpsl, self.symbol, self.side, sl=self.fpx(px)); self.stop = dict(px=px, order_id=r["pos_loss"]["orderId"])
            self.stop_fail = 0; self.ev("STOP_SET", px=px, order_id=self.stop["order_id"])
        except Exception as e:
            if isinstance(e, BitgetError) and str(e.code) == "40917":       # "stop price must be < mark price": the market is already through our stop
                self.ev("STOP_THROUGH", px=px, err=str(e)[:120]); await self.emergency_close("stop level already passed"); return
            if not isinstance(e, BitgetError):                               # timeout / network: the exchange may hold it — look before counting a failure
                try:
                    ex = await self.find_pos_loss()
                    if ex: self.stop, self.stop_fail = ex, 0; self.ev("STOP_SET", px=ex["px"], order_id=ex["order_id"], via="confirmed after timeout"); return
                except Exception as e2: self.cy.err("set_stop confirm", e2)
            self.stop_fail += 1; self.ev("STOP_SET_FAIL", px=px, n=self.stop_fail, err=f"{type(e).__name__}: {str(e)[:140]}")

    def fallback_stop(self):
        """A stop level that needs no features: the stop already set (persisted across a restart) or the money cap below the average
        (over the full unit when the position is smaller — same rule as the Strategy)."""
        qty, avg = pos_stats(self.pos)
        return self.guard(self.strat.stop_px if self.strat.stop_px is not None else avg - self.s * self.sp["cap_usdt"] / max(qty, self.sp["unit_qty"]), avg)

    def guard(self, px, ref):
        """Liquidation guard: a stop is never left beyond the liquidation price — the exchange's own figure when it is a real price on the
        loss side of the position (crossed: Bitget reports a NEGATIVE sentinel when the whole account backs the position, i.e. no
        liquidation in range — 2026-08-30 23:53 a short's liq −8.34 turned into a stop request of −8.25, rejected 43011), else in
        isolated mode ref x (1 -/+ 0.9/lever) (the position's own margin is the real stop when the cap is wider than it); crossed
        without a real figure: no guard, the money cap stands."""
        liq = self.exch.get("liq"); g = None
        if liq and liq > 0 and self.s * (ref - liq) > 0: g = liq * (1 + self.s * 0.01)
        elif self.cy.b.margin_mode != "crossed" and self.lever and ref: g = ref * (1 - self.s * 0.9 / self.lever)
        if g is not None and self.s * (px - g) < 0:
            if self.guarded != round(g, 6): self.guarded = round(g, 6); self.ev("STOP_LIQ_GUARD", wanted=round(px, 6), stop=self.guarded, liq=liq, lever=self.lever)
            px = g
        if px < (self.cy.px_tick or 0.001): px = self.cy.px_tick or 0.001              # last line: an exchange trigger must be a positive price (43011 otherwise), whatever computed it
        return px

    async def ensure_stop(self):
        """A live position never waits for a public tick to get its exchange stop (resync, and every second while it has none)."""
        if not pos_stats(self.pos)[0] or self.stop is not None or self.stop_fail >= 3 or time.time() - self.stop_try_t < 2: return
        self.stop_try_t = time.time(); await self.set_stop(self.fallback_stop())
        if self.stop: self.strat.adopt_stop(self.stop["px"])

    async def find_pos_loss(self):
        """The exchange's whole-position stop on our side, as dict(px, order_id), or None."""
        for o in (await self.cy.rest(self.cy.b.pending_plan_orders, self.symbol)).get("entrustedList") or []:
            if o.get("planType") in ("psl", "pos_loss") and o.get("posSide", self.side) == self.side: return dict(px=float(o["triggerPrice"]), order_id=o["orderId"])
        return None

    async def drop_preset(self):
        """Cancel the entry order's preset stop once the whole-position pos_loss is registered (never before). The id is kept until
        the exchange confirms; a failure is retried from housekeeping."""
        pid = self.preset_plan; self.preset_retry_t = time.time()
        try:
            r = await self.cy.rest(self.cy.b.cancel_plan, self.symbol, pid, "loss_plan")
            if (r or {}).get("failureList"): self.ev("PRESET_DROP_FAIL", order_id=pid, err=str(r["failureList"])[:120]); return
            self.preset_plan = None; self.ev("PRESET_DROPPED", order_id=pid)
        except Exception as e: self.ev("PRESET_DROP_FAIL", order_id=pid, err=str(e)[:120])

    async def emergency_close(self, why):
        """Market-close our lots now — after every resting order is confirmed gone (a resting close order freezes quantity that a
        market close cannot take; a failed cancel is retried, an unconfirmed submission settled first). The fill (clientOid cyc?-x...)
        is booked as a stop hit."""
        if not pos_stats(self.pos)[0]: return
        for _ in range(12):
            for role in ("buy", "trim"):
                w = self.work[role]
                if w and not w.get("cancel_pending"): await self.cancel(role)      # first pass, and again after a failed cancel
            if not any(self.work.values()): break
            await asyncio.sleep(0.5)
        else: self.ev("EMERGENCY_CANCEL_UNCONFIRMED", working={r: (w and w["oid"]) for r, w in self.work.items()})
        qty, avg = pos_stats(self.pos)                        # re-read: a fill may have landed while the cancels settled
        if not qty: return
        oid = f"{self.OIDP}x{int(time.time() * 1000)}"
        for attempt in (1, 2):
            try:
                await self.cy.rest(self.cy.b.market_order, self.symbol, "buy" if self.s > 0 else "sell", self.fq(qty), trade_side="close", client_oid=oid)
                self.ev("EMERGENCY_CLOSE", qty=qty, avg=avg, why=why, oid=oid); return
            except BitgetError as e:
                self.cy.err("emergency_close", e)
                if attempt == 1: await asyncio.sleep(2)     # the cancel may still be settling; one retry
            except Exception as e:                          # timeout: look before a second close could double the reduction
                self.cy.err("emergency_close", e); await asyncio.sleep(2)
                try:
                    await self.cy.rest(self.cy.b.order_detail, self.symbol, client_oid=oid)
                    self.ev("EMERGENCY_CLOSE", qty=qty, avg=avg, why=why, oid=oid, confirmed="after timeout"); return
                except Exception as e2:
                    if not any(k in str(e2).lower() for k in GONE): self.cy.err("emergency_close detail", e2); return   # unknown: resync / the position push decide, no blind repeat

    async def stop_failed(self):
        qty, avg = pos_stats(self.pos)
        self.ev("STOP_FAILED", qty=qty, avg=avg, msg="3 consecutive failures: market-closing our lots")
        self.stop_fail = 0; self.halt("STOP_FAILED")
        await self.emergency_close("stop placement failed 3x")

    async def cancel_all(self):
        for role in ("buy", "trim"):
            if self.work[role]:
                try: await self.cancel(role)
                except Exception as e: self.cy.err("cancel_all", e)

    # ---- fills --------------------------------------------------------------------
    async def on_fill(self, role, qty, px, fee, oid, scope="maker", lot=None):
        if lot is None and role == "trim": lot = self.trim_lot.get(oid)
        pnl = apply_fill(self.pos, self.s, role == "buy", qty, px, oid=oid, fee=fee, lot=lot)
        self.realized += pnl; self.strat.on_fill(role, qty)
        w = self.work[role]
        if w and w["oid"] == oid:
            w["filled"] += qty
            done = w["filled"] >= w["qty"] - self.cy.qstep / 2 or (w.get("settling") and w["filled"] >= w["settling"] - self.cy.qstep / 2)
            if done: self.work[role] = None                    # every fill the exchange counted is booked: the slot is free again
        q, avg = pos_stats(self.pos)
        self.ev("FILL", role=role, qty=qty, px=px, fee=rnd(fee), pnl=rnd(pnl), scope=scope, pos_qty=rnd(q), avg=rnd(avg), realized=rnd(self.realized), oid=oid,
                lot="core" if lot == 0 else None,   # a de-risk cut under units reduces the core lot, not the LIFO unit (bot.cycles follows this)
                mid=self.feat.mid)                  # mid at the fill: with PLACE/TAKER.mid (arrival) this is the slippage and impact record (NEXT 6)
        await self.tick([])          # re-place the opposite side at once

    async def on_stop_hit(self, px, qty=None, fee=None, oid=None):
        """A stop (or emergency close) fill. oid = the exchange order that filled: one stop order counts once, however many fills it splits into."""
        q, avg = pos_stats(self.pos); qty = min(qty or q, q)
        pnl = apply_fill(self.pos, self.s, False, qty, px, fee=fee if fee is not None else qty * px * self.cy.taker)
        self.realized += pnl; self.stop = None
        again = oid is not None and oid in self.stop_hit_oids
        if oid is not None: self.stop_hit_oids.append(oid)
        if not again: self.stops_today += 1
        for role in ("buy", "trim"):                       # resting orders must leave the exchange too, or an entry could reopen the position
            if not self.work[role]: continue
            if self.mode == "live": await self.cancel(role)          # tracked until the exchange confirms; a fill that sneaks in is booked as the real fill it is
            else: self.work[role] = None
        self.ev("STOP_HIT", px=px, qty=qty, avg=avg, pnl=rnd(pnl), realized=rnd(self.realized), stops_today=self.stops_today, oid=oid, partial=again or None)
        if again: return
        if self.stops_today >= self.sp["max_stops_day"]: self.halt("DAILY_STOPS", stops=self.stops_today)
        else: self.pos["cooldown_until"] = time.time() + self.sp["stop_cooldown_s"]   # re-enter on the next deceleration, not at once

    async def on_private_fill(self, r, qty, px, fee):
        oid = r.get("clientOid") or ""
        if oid.startswith(self.OIDP):
            if oid[len(self.OIDP)] == "x": await self.on_stop_hit(px, qty, fee, oid=r.get("orderId")); return     # our emergency close = a stop hit
            await self.on_fill("buy" if r.get("tradeSide") == "open" else "trim", qty, px, fee, oid, r.get("tradeScope", "?")); return
        if r.get("tradeSide") != "close" or not self.pos["lots"]:
            self.ev("EXTERNAL_FILL", fill_side=r.get("side"), tradeSide=r.get("tradeSide"), qty=qty, px=px, oid=oid); self.halt("EXTERNAL_FILL"); return
        # a triggered plan order fills with clientOid = the plan's orderId (recorded 2026-08-29, psl and sl alike): the order's identity says
        # it was our stop, whichever channel arrives first
        plans = {self.stop and self.stop.get("order_id"), self.preset_plan} - {None}
        if oid in plans or oid in self.stop_oids or oid in self.stop_hit_oids: await self.on_stop_hit(px, qty, fee, oid=oid); return
        self.unmatched_close.append(dict(t=time.time(), qty=qty, px=px, fee=fee, oid=oid, side=r.get("side")))   # classified when the algo push names it, or after 15s
        self.ev("CLOSE_FILL_PENDING", qty=qty, px=px, oid=oid)
        w = self.work["buy"]         # 그 사이 새 로트가 생기면 장부와 거래소의 로트가 어긋난다(수량은 같아 불일치 감시가 못 잡는다): 대기 담기를 지금 거둔다
        if w and not w.get("cancel_pending"): self.ev("ENTRY_CANCEL", why="close fill pending", oid=w["oid"]); await self.cancel("buy"); self.strat.arm = None

    async def settle_close_fills(self, plan_oid=None):
        """Close fills whose order the book did not know: a stop hit once the algo channel names their order (identity only — never a
        time window, which would swallow a manual close), an external fill -> HALT after 15s without it."""
        keep = []
        for x in self.unmatched_close:
            if x["oid"] == plan_oid or x["oid"] in self.stop_oids: await self.on_stop_hit(x["px"], x["qty"], x["fee"], oid=x["oid"])
            elif time.time() - x["t"] >= 15: self.ev("EXTERNAL_FILL", fill_side=x["side"], tradeSide="close", qty=x["qty"], px=x["px"], oid=x["oid"]); self.halt("EXTERNAL_FILL")
            else: keep.append(x)
        self.unmatched_close = keep

    def on_order(self, r):
        oid = r.get("clientOid") or ""; kind = oid[len(self.OIDP)]
        if kind in ("m", "x"): return                                   # market orders: settled by their fills
        role = "buy" if kind == "b" else "trim"; w = self.work[role]; st = r.get("status")
        if w and w["oid"] == oid:
            w["order_id"] = w["order_id"] or r.get("orderId"); w["unconfirmed"] = None
            if st in ("cancelled", "canceled", "filled"):
                acc = float(r.get("accBaseVolume") or 0)
                if acc > w["filled"] + self.cy.qstep / 2:            # the exchange finished it before its fills reached us (the orders channel outran the fill
                    w["settling"], w["settling_t"] = acc, time.time()   # channel by a second, EGLD 2026-09-03 16:38: 48.3 "filled" while 7.3 was booked -> the
                else: self.work[role] = None                          # "remainder" 41.0 was re-ordered and both filled). Hold the slot until the fills are booked.
        elif w is None and st in ("live", "partially_filled", "new"):   # an order of ours the book forgot (restart, timeout): track it, never duplicate it
            self.work[role] = dict(oid=oid, order_id=r.get("orderId"), px=float(r["price"]), qty=float(r["size"]), filled=float(r.get("accBaseVolume") or 0), t=time.time())
            self.ev("ADOPT_ORDER", role=role, px=self.work[role]["px"], qty=self.work[role]["qty"], oid=oid, via="orders channel")

    def on_position(self, r):
        self.exch.update(total=float(r["total"]) if r else 0.0, avg=float(r["openPriceAvg"]) if r else None,
                         upl=float(r.get("unrealizedPL") or 0) if r else 0.0, mark=float(r["markPrice"]) if r and r.get("markPrice") else self.exch["mark"],
                         liq=(float(r.get("liquidationPrice") or 0) or None) if r else None)
        if self.mode == "live": self.check_mismatch(self.exch["total"])

    def check_mismatch(self, total):
        """Exchange size vs our lots (position pushes and resync alike): a gap is tolerated for 10s — own fills still in flight, a close fill
        being classified — then HALT: UNOWNED_POSITION when we hold nothing (the user's position, never touched), EXTERNAL_FILL otherwise."""
        qty, _ = pos_stats(self.pos)
        if abs(total - qty) <= self.cy.qstep / 2: self.mismatch_since = None; return
        if self.unmatched_close: return
        self.mismatch_since = self.mismatch_since or time.time()
        if time.time() - self.mismatch_since > 10 and not self.pos["halt"]:
            self.halt("UNOWNED_POSITION" if not qty else "EXTERNAL_FILL", exch_total=total, our_qty=qty, msg="size differs from our lots for 10s: adopt (empty lots) or flatten, then RESUME")

    async def on_algo(self, r):
        st, pt = r.get("status"), r.get("planType")
        if pt in ("sl", "loss_plan"):    # the preset stop of our first entry (WS says "sl", REST says "loss_plan"): protects the fill until our pos_loss is live
            if st == "live":
                self.preset_plan = r["orderId"]
                if self.stop and self.stop.get("order_id"): await self.drop_preset()
            elif st in ("executed", "triggered", "executing"):
                self.stop_trig_t = time.time(); self.stop_oids.append(r["orderId"]); await self.settle_close_fills(r["orderId"])
                if st != "executing": self.preset_plan = None
            else: self.preset_plan = None
            return
        if pt not in ("psl", "pos_loss"): return
        if st == "live":
            self.stop = dict(px=float(r["triggerPrice"]), order_id=r["orderId"])
            if self.preset_plan: await self.drop_preset()
        elif st in ("executed", "triggered", "executing"):
            self.stop_trig_t = time.time(); self.stop_oids.append(r["orderId"]); await self.settle_close_fills(r["orderId"])
            if st != "executing" and self.stop and self.stop.get("order_id") == r.get("orderId"): self.stop = None
        elif self.stop and self.stop.get("order_id") == r.get("orderId"): self.stop = None

    # ---- live reconciliation: at start (after the private feed is up), after every private reconnect, and every 60s ----------------
    async def resync(self, initial=False):
        """REST is the authority: position size vs our lots (adopt at start when allowed, else HALT), our resting orders (adopt the
        ones the book forgot, cancel duplicates), and the position stop."""
        mine = [p for p in await self.cy.rest(self.cy.b.positions) if p["symbol"] == self.symbol and p["holdSide"] == self.side and float(p.get("total", 0)) > 0]
        pos = mine[0] if mine else None
        total = float(pos["total"]) if pos else 0.0
        qty, _ = pos_stats(self.pos)
        if total and not qty and initial and self.sp.get("adopt"):
            self.pos.update(lots=[[total, float(pos["openPriceAvg"]), "adopt"]], avg=float(pos["openPriceAvg"]), last="buy", last_buy_px=float(pos["openPriceAvg"]))
            self.ev("ADOPT", qty=total, avg=float(pos["openPriceAvg"]))
        self.on_position(pos)                                  # None when flat; a size gap runs the same 10s grace as a position push (own fills may still be queued)
        pend = [o for o in (await self.cy.rest(self.cy.b.pending_orders, self.symbol)).get("entrustedList") or [] if (o.get("clientOid") or "").startswith(self.OIDP)]
        seen = set()
        for o in pend:
            oid = o["clientOid"]; role = "buy" if oid[len(self.OIDP)] == "b" else "trim"; w = self.work[role]; seen.add(oid)
            if w is None or w["oid"] == oid:
                if w is None: self.ev("ADOPT_ORDER", role=role, px=float(o["price"]), qty=float(o["size"]), oid=oid, via="resync")
                self.work[role] = dict(oid=oid, order_id=o["orderId"], px=float(o["price"]), qty=float(o["size"]), filled=float(o.get("baseVolume") or 0), t=time.time())
            else:
                try: await self.cy.rest(self.cy.b.cancel_order, self.symbol, o["orderId"]); self.ev("CANCEL", role=role, px=o.get("price"), oid=oid, why="duplicate at resync")
                except Exception as e: self.cy.err("resync cancel", e)
        for role in ("buy", "trim"):                       # tracked but not on the exchange and not settling: it is gone
            w = self.work[role]
            if w and w["oid"] not in seen and not w.get("unconfirmed") and not w.get("cancel_pending") and time.time() - w["t"] > 10 \
                    and not (w.get("settling") and time.time() - w.get("settling_t", 0) < 10):   # a settling order's fills get 10 s to arrive before the slot is released
                self.work[role] = None; self.ev("ORDER_GONE", role=role, oid=w["oid"], via="resync")
        found = preset = None
        for o in (await self.cy.rest(self.cy.b.pending_plan_orders, self.symbol)).get("entrustedList") or []:
            if o.get("posSide", self.side) != self.side: continue
            if o.get("planType") in ("psl", "pos_loss"):
                found = o["orderId"]
                if not self.stop or self.stop.get("order_id") != o["orderId"]:
                    self.stop = dict(px=float(o["triggerPrice"]), order_id=o["orderId"]); self.strat.adopt_stop(self.stop["px"]); self.ev("ADOPT_STOP", px=self.stop["px"])
            elif o.get("planType") in ("sl", "loss_plan"): preset = o["orderId"]      # an entry's preset stop the book forgot (restart before it was dropped)
        if not found and self.stop and self.stop.get("order_id"): self.stop = None   # the exchange has no such stop any more
        if preset != self.preset_plan:
            if preset: self.ev("ADOPT_PRESET", order_id=preset, was=self.preset_plan)
            self.preset_plan = preset
        await self.ensure_stop()                               # a position (adopted, restarted, or whose stop vanished) gets its stop now, not at the next tick

    async def start_live(self): await self.resync(initial=True)

    # ---- dry-run fill model (sim_match in signal.py, shared with the backtest) ------------------------------------------------
    async def sim_stop(self, f):
        qty, _ = pos_stats(self.pos)
        if not qty or not self.stop: return
        mark = f.get("mark") or f["mid"]
        if self.s * (mark - self.stop["px"]) <= 0:
            await self.on_stop_hit(self.stop["px"] * (1 - self.s * SLIP))


class Cycle:
    def __init__(self, symbol=None):
        self.want = symbol                                            # CLI 로 못박은 심볼: params 의 strat.symbol 이 바뀌어도 이 엔진은 안 따라간다
        self.p = load_params() or {}; self.pmtime = os.path.getmtime(PARAMS)
        sp = strat_for(self.p, symbol)
        self.symbol, self.mode = sp["symbol"], sp.get("mode", "dry")
        self.state = os.path.join(LOGS, f"state-{self.symbol}.json")   # 엔진마다 자기 파일 — 두 엔진이 한 파일을 덮어쓰지 않는다
        self.sides = list(sp.get("sides") or [sp["side"]])
        self.b = from_env(); self.b.sync_time(); self.b.refresh_mode(self.symbol)
        c = self.b.contract(self.symbol)
        self.pp, self.vp = int(c["pricePlace"]), int(c["volumePlace"])
        self.px_tick, self.qstep = float(c["priceEndStep"]) * 10 ** -self.pp, 10 ** -self.vp
        self.maker, self.taker = float(c["makerFeeRate"]), float(c["takerFeeRate"])
        self.sp = sp
        os.makedirs(LOGS, exist_ok=True); self.feat = Features()      # defaults first, so an invalid file can still be reported through ev()
        if outside_books(self.p, self.symbol):        # books 가 진실이다: 그 밖의 계약은 아무도 소유하지 않고 지갑 몫도 없다(주문 하나 내기 전에 나간다)
            self.ev("EXIT", why=f"{self.symbol} is not in params.books {sorted(self.p['books'])}"); os._exit(0)
        bad = valid_params(sp, self.p.get("sig") or {})
        if bad or any(sd not in ("long", "short") for sd in self.sides) or len(set(self.sides)) != len(self.sides) or self.mode not in ("dry", "live"):
            self.ev("EXIT", why=f"invalid params at start: {bad or [self.sides, self.mode]}"); os._exit(1)
        self.feat = Features(self.p.get("sig"))
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.acct = dict(avail=None, equity=None, upl_all=None)
        self.lever_t, self.daily_t = 0.0, 0.0
        self.q, self.last_t, self.sigs = asyncio.Queue(), None, deque(maxlen=5)
        self.seen_trades, self.t0_ms = deque(maxlen=2000), int(time.time() * 1000)
        self.prv_down, self.hint_since, self.hint_alert_t, self.resync_due, self.resync_t = None, None, 0.0, False, 0.0
        self.dirty, self.errors, self.t0, self.pub_down, self.down_alerted, self.pending_restart = True, 0, time.time(), None, False, None
        if self.mode == "live" and not self.b.hedge:
            self.ev("EXIT", why="one-way position mode is not supported (order side semantics differ); switch the account to hedge mode"); os._exit(0)
        self.books = {sd: Book(self, sd) for sd in self.sides}
        self.load_state()

    # ---- persistence / logging ----------------------------------------------
    def load_state(self):
        """자기 심볼의 스냅샷을 싣고, 포트폴리오 밖 심볼에 live 물량이 남아 있으면 멈춘다.
        state-<심볼>.json 이 아직 없으면 예전 단일 state.json 에서 한 번 물려받는다(전환 직후 1회)."""
        st = None
        for path in (self.state, STATE):
            try:
                with open(path, encoding="utf-8") as f: cand = json.load(f)
            except Exception: continue
            if cand.get("symbol") == self.symbol: st = cand; break
        mine = set(portfolio(self.p)) | {self.symbol}
        for path in glob.glob(os.path.join(LOGS, "state-*.json")):    # 다른 계약에 남은 live 물량: 사람이 정리하거나 인수해야 한다.
            # 옛 단일 state.json 은 여기서 보지 않는다 — 이관 뒤 남는 낡은 파일이 영원히 OLD_POSITION 을 걸게 된다
            try:
                with open(path, encoding="utf-8") as f: other = json.load(f)
            except Exception: continue
            if other.get("symbol") in mine or other.get("mode") != "live" or self.mode != "live": continue
            if any((b.get("pos") or {}).get("lots") for b in (other.get("books") or {}).values()):
                for bk in self.books.values(): bk.pos["halt"] = "OLD_POSITION"
                self.ev("HALT", why="OLD_POSITION", symbol_left=other.get("symbol")); break
        if st:
            for bk in self.books.values(): bk.load(st)

    def write_state(self):
        f = self.feat.f; first = self.books[self.sides[0]].snapshot()
        st = dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), symbol=self.symbol, sides=self.sides, side=self.sides[0], mode=self.mode, day=self.day,
                  up_s=int(time.time() - self.t0), ws=dict(pub=self.pub.connected, prv=self.prv.connected, sec=f.get("t")),
                  books={sd: bk.snapshot() for sd, bk in self.books.items()}, acct=self.acct, f={k: rnd(v) for k, v in f.items()},
                  signals=list(self.sigs), params=dict(strat=self.sp, sig=self.feat.p), errors=self.errors, **first)   # first side also flat, for older readers
        tmp = self.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh: json.dump(st, fh)
        os.replace(tmp, self.state); self.dirty = False

    def ev(self, kind, **kw):
        # 엔진이 여럿이면 심볼 없이는 로그를 갈라 읽을 수 없다. kw 를 뒤에 둬서 호출자가 이미 symbol 을 넘기면(START) 그쪽이 이긴다
        line = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "sec": self.feat.f.get("t"), "ev": kind,
                           "symbol": getattr(self, "symbol", None), **kw}, ensure_ascii=False)
        with open(EVENTS, "a", encoding="utf-8") as fh: fh.write(line + "\n")
        if kind in ALERT:
            with open(ALERTS, "a", encoding="utf-8") as fh: fh.write(line + "\n")
        if kind not in QUIET: print(line[:400], flush=True)
        self.dirty = True

    def err(self, where, e):
        self.errors += 1
        self.ev("ERROR" if self.errors <= 3 or self.errors % 20 == 0 else "ERR", where=where, msg=f"{type(e).__name__}: {str(e)[:200]}")

    # ---- run loop -----------------------------------------------------------
    async def run(self):
        self.ev("START", mode=self.mode, symbol=self.symbol, sides=self.sides, tick=self.px_tick, qstep=self.qstep,
                books={sd: dict(halt=bk.pos["halt"], lots=bk.pos["lots"], unit=bk.sp["unit_qty"], cap=bk.sp["cap_usdt"]) for sd, bk in self.books.items()})
        self.pub = WS(PUB_URL, lambda r: self.q.put_nowait(("pub", r)), self.on_local, name="pub")
        self.prv = WS(PRV_URL, lambda r: self.q.put_nowait(("prv", r)), self.on_local, auth=(self.b.key, self.b.secret, self.b.passphrase), name="prv")
        await self.pub.set_args([{"instType": INST, "channel": ch, "instId": self.symbol} for ch in PUB_CH])
        await self.prv.set_args(PRIVATE_ARGS)
        await self.refresh_lever()
        try:
            rows15 = (await self.rest(self.b.candles, self.symbol, "15m", 200))[:-1]
            self.feat.seed_candles((await self.rest(self.b.candles, self.symbol, "1m", 1000))[:-1], rows15)
            await self.refresh_daily()
            self.ev("SEED", candles=len(self.feat.candles), c15=len(self.feat.c15), atr=self.feat.atr, side_hint=self.feat.side_hint,
                    side_hint_15m=self.feat.side_hint_15m, daily_trend=self.feat.daily_trend)
        except Exception as e: self.err("seed", e)
        tasks = [asyncio.create_task(self.pub.run()), asyncio.create_task(self.prv.run())]
        if self.mode == "live":                               # subscribe first, then take the REST snapshot: nothing can fill in between unseen
            for _ in range(60):
                if self.prv.connected: break
                await asyncio.sleep(0.5)
            if not self.prv.connected: self.ev("EXIT", why="private feed did not come up within 30s"); os._exit(2)
            for bk in self.books.values(): await bk.start_live()
        await asyncio.gather(*tasks, self.consume(), self.housekeeping())

    def on_local(self, e):
        self.ev("WS", **e)
        if e["local"] == "WS_SUB_ERROR":                       # a refused channel is a configuration error: never run half-subscribed
            self.ev("EXIT", why=f"subscription refused on {e['name']}: {e.get('msg')}"); self.write_state(); os._exit(2)
        if e["name"] == "pub":
            if e["local"] == "WS_DOWN" and self.pub_down is None: self.pub_down = time.time()
            if e["local"] == "WS_UP":
                if self.down_alerted: self.ev("WS_UP", down_s=int(time.time() - self.pub_down))
                self.pub_down, self.down_alerted = None, False
        else:
            if e["local"] == "WS_DOWN" and self.prv_down is None: self.prv_down = time.time()
            if e["local"] == "WS_UP":
                if self.prv_down is not None and self.mode == "live": self.resync_due = True   # anything that happened while blind is re-read from REST
                self.prv_down = None

    def live_ok(self):
        """Live orders only while the private feed is up (fills/cancels are otherwise invisible)."""
        return self.mode == "dry" or (getattr(self, "prv", None) is not None and self.prv.connected)

    async def consume(self):
        while True:
            src, raw = await self.q.get()
            try: await self.handle(src, raw)
            except Exception as e: self.err("handle", e)

    async def handle(self, src, raw):
        j = json.loads(raw); arg = j.get("arg") or {}; ch = arg.get("channel"); data = j.get("data")
        if not ch or not data:
            if j.get("event") == "error": self.ev("ERROR", msg=raw[:200])
            return
        if src == "pub":
            if arg.get("instId") != self.symbol: return
            if ch == "trade" and j.get("action") != "snapshot" and self.mode == "dry": await self.sim_trades(data)
            sigs = self.feat.feed(j)
            if ch == "books15" and self.mode == "dry": await self.sim_book()
            if sigs or self.feat.f.get("t") != self.last_t:
                self.last_t = self.feat.f.get("t")
                for x in sigs:
                    self.sigs.append(dict(t=x["t"], sig=x["sig"], mid=x["mid"]))
                    self.ev("SIGNAL", **{k: rnd(v) for k, v in x.items() if k != "t"})
                for bk in self.books.values(): await bk.tick(sigs)
        else:
            await self.private(ch, data)

    async def sim_trades(self, data):
        """Dry fills: one print is shared by every resting order of every book at that price (placement order), never consumed twice.
        A print through the price of an order younger than CROSS_S is the exchange's post-only cancel, not a fill (sim_match)."""
        for t in data:
            orders = [((bk, role), w, bk.rest_on_bid(role)) for bk in self.books.values() for role in ("buy", "trim") if (w := bk.work[role])]
            for (bk, role), w, fill in sim_match(orders, float(t["price"]), float(t["size"]), t.get("side"), self.qstep, t=time.time()):
                if fill is None: bk.work[role] = None; bk.ev("CANCEL", role=role, px=w["px"], filled=w["filled"], oid=w["oid"], why="crossed"); continue
                await bk.on_fill(role, fill, w["px"], fill * w["px"] * self.maker, w["oid"], "sim")

    async def sim_book(self):
        """Dry: a book snapshot drains the queue ahead of resting orders by the cancellations it shows; an opposite touch at our price
        cancels an order younger than CROSS_S (crossed on arrival) or fills an older one (sim_book in signal.py)."""
        orders = [((bk, role), w, bk.rest_on_bid(role)) for bk in self.books.values() for role in ("buy", "trim") if (w := bk.work[role])]
        for (bk, role), w, fill in sim_book(orders, self.feat.bids, self.feat.asks, t=time.time(), qstep=self.qstep):
            if fill is None: bk.work[role] = None; bk.ev("CANCEL", role=role, px=w["px"], filled=w["filled"], oid=w["oid"], why="crossed"); continue
            await bk.on_fill(role, fill, w["px"], fill * w["px"] * self.maker, w["oid"], "sim")

    async def private(self, ch, data):
        if ch == "account":
            for r in data:
                if r.get("marginCoin") == "USDT": self.acct.update(avail=float(r["available"]), equity=float(r["equity"]), upl_all=float(r.get("unrealizedPL") or 0))
            return
        if self.mode != "live": return        # a dry book never takes exchange events: the user's own fills/stops on this symbol must not enter the simulation
        if ch == "fill":                      # Bitget pushes every fill as action "snapshot": dedupe by tradeId, skip history from before we started
            for r in data:
                if r.get("symbol") != self.symbol or r.get("tradeId") in self.seen_trades or int(r.get("cTime") or 0) < self.t0_ms - 2000: continue
                self.seen_trades.append(r.get("tradeId"))
                qty, px = float(r["baseVolume"]), float(r["price"]); fee = -sum(float(x.get("totalFee") or 0) for x in r.get("feeDetail") or [])
                hold = "long" if (r.get("side") == "buy") == (r.get("tradeSide") == "open") else "short"
                oid = r.get("clientOid") or ""
                bk = next((b for b in self.books.values() if oid.startswith(b.OIDP)), None) or self.books.get(hold)
                if bk: await bk.on_private_fill(r, qty, px, fee)
        elif ch == "orders":                  # also pushed as "snapshot"; status updates are idempotent
            for r in data:
                if r.get("instId") != self.symbol: continue
                oid = r.get("clientOid") or ""
                bk = next((b for b in self.books.values() if oid.startswith(b.OIDP)), None)
                if bk: bk.on_order(r)
        elif ch == "positions":
            for sd, bk in self.books.items():
                mine = [r for r in data if r.get("instId") == self.symbol and r.get("holdSide") == sd]
                bk.on_position(mine[0] if mine else None)
            self.dirty = True
        elif ch == "orders-algo":
            for r in data:
                if r.get("instId") != self.symbol: continue
                bk = self.books.get(r.get("posSide"))
                if bk: await bk.on_algo(r)

    async def housekeeping(self):
        last_state = 0.0
        while True:
            await asyncio.sleep(1)
            try:
                if os.path.exists(os.path.join(ROOT, "STOP")): await self.shutdown("STOP file")
                rp = os.path.join(ROOT, "RESUME")
                if os.path.exists(rp):                              # always consumed: a stale RESUME must not lift a later HALT
                    for bk in self.books.values():
                        if bk.pos["halt"]: bk.ev("RESUME", was=bk.pos["halt"]); bk.pos["halt"] = None; bk.mismatch_since = None
                    if not any(bk.pos["halt"] for bk in self.books.values()): self.ev("RESUME", was=None)
                    os.remove(rp)
                if self.mode == "live" and self.prv.connected and (self.resync_due or time.time() - self.resync_t >= 60):
                    self.resync_due, self.resync_t = False, time.time()
                    for bk in self.books.values():
                        try: await bk.resync()
                        except Exception as e: self.err("resync", e)
                if self.mode == "live":
                    for bk in self.books.values():
                        await bk.settle_orders()
                        if bk.unmatched_close: await bk.settle_close_fills()
                        if bk.preset_plan and (not bk.pos["lots"] or (bk.stop and bk.stop.get("order_id"))) and time.time() - bk.preset_retry_t >= 10: await bk.drop_preset()
                        if self.prv.connected: await bk.ensure_stop()
                    if self.pending_restart and not any(pos_stats(bk.pos)[0] or any(bk.work.values()) for bk in self.books.values()):
                        await self.shutdown(f"params {self.pending_restart[0]} change (deferred until flat)")
                silent = not self.pub.connected or time.time() - self.pub._rx > 30 or (self.mode == "live" and not self.prv.connected)
                if silent:                                          # blind: a resting entry could fill on a signal nobody can revoke; the TTL/re-acceleration cancels need ticks
                    for bk in self.books.values():
                        w = bk.work["buy"]
                        if w and not w.get("cancel_pending"): bk.ev("ENTRY_CANCEL", why="feed silent", oid=w["oid"]); await bk.cancel("buy"); bk.strat.arm = None
                await self.reload_params()
                day = time.strftime("%Y-%m-%d", time.gmtime())
                if day != self.day:
                    for bk in self.books.values(): bk.day_close(self.day)
                    self.day = day
                if self.pub_down and not self.down_alerted and time.time() - self.pub_down > 60:
                    self.ev("WS_DOWN", down_s=int(time.time() - self.pub_down)); self.down_alerted = True
                if self.prv_down and time.time() - self.prv_down > 60 and int(time.time() - self.prv_down) % 300 < 1:
                    self.ev("WS_DOWN", feed="private", down_s=int(time.time() - self.prv_down))
                for bk in self.books.values():
                    if self.mode == "live" and bk.stop_fail >= 3: await bk.stop_failed()
                    if time.time() - bk.sized_t >= 60: bk.resize()
                if time.time() - self.lever_t >= 300: await self.refresh_lever()
                if time.time() - self.daily_t >= 3600: await self.refresh_daily()
                hint = self.feat.side_hint
                if len(self.sides) == 1 and hint and hint != self.sides[0]:   # the 15m structure points the other way: tell the agent (flip when flat, via params)
                    self.hint_since = self.hint_since or time.time()
                    if time.time() - self.hint_since >= 2700 and time.time() - self.hint_alert_t >= 3600:
                        self.hint_alert_t = time.time(); self.ev("SIDE_HINT", hint=hint, side=self.sides[0], qty=pos_stats(self.books[self.sides[0]].pos)[0])
                else: self.hint_since = None
                if self.dirty or time.time() - last_state >= 5: self.write_state(); last_state = time.time()
            except Exception as e: self.err("housekeeping", e)

    async def reload_params(self):
        try: m = os.path.getmtime(PARAMS)
        except OSError: return
        if m == self.pmtime: return
        self.pmtime = m; p = load_params()
        if p is None: self.ev("PARAMS_INVALID", msg="unreadable json; keeping previous"); return
        sp = strat_for(p, self.want); sides = list(sp.get("sides") or [sp["side"]]); sig = p.get("sig") or {}
        bad = valid_params(sp, sig)
        if bad or any(sd not in ("long", "short") for sd in sides) or len(set(sides)) != len(sides) or sp.get("mode", "dry") not in ("dry", "live") or not isinstance(sp.get("symbol"), str):
            self.ev("PARAMS_INVALID", msg=f"bad keys {bad or [sides, sp.get('mode'), sp.get('symbol')]}; keeping previous"); return
        new = [sp["symbol"], sides, sp.get("mode", "dry")]
        gone = outside_books(p, sp["symbol"])   # select removed this book (only ever while it is flat), or this engine was never in it: it is done
        stateful = any(sig.get(k, SIG[k]) != self.feat.p[k] for k in STATEFUL)         # EMAs/deques are built at start
        restart = (("book removed", sp["symbol"]) if gone else ("contract", new) if new != [self.symbol, self.sides, self.mode]
                   else (("sig window", {k: sig.get(k, SIG[k]) for k in STATEFUL}) if stateful else None))
        if restart:
            if self.mode == "live" and any(pos_stats(bk.pos)[0] or any(bk.work.values()) for bk in self.books.values()):
                if self.pending_restart != restart: self.pending_restart = restart; self.ev("PARAMS_DEFERRED", msg=f"{restart[0]} change waits until every book is flat with no working order", new=restart[1])
            else:
                self.ev("PARAMS", msg=f"{restart[0]} changed: restarting", new=restart[1]); await self.shutdown(f"params {restart[0]} change")
        else: self.pending_restart = None
        changed = {k: v for k, v in sp.items() if self.sp.get(k) != v}
        self.sp, self.p = sp, p
        for bk in self.books.values(): bk.apply_params(sp, gone=gone)
        if not (restart and restart[0] == "sig window"):       # the signal set applies as one snapshot (a key removed from the file returns to its default); a pending window change waits whole
            self.feat.p.clear(); self.feat.p.update({**SIG, **sig})
        self.ev("PARAMS", changed=changed, sig=sig)

    async def refresh_daily(self):
        """Daily candles (incl. the open day) every hour -> Features.daily_trend (size scaling only)."""
        self.daily_t = time.time()
        try:
            was = self.feat.daily_trend
            self.feat.seed_daily(await self.rest(self.b.candles, self.symbol, "1D", 80))
            if self.feat.daily_trend != was: self.ev("DAILY_TREND", trend=self.feat.daily_trend, was=was)
        except Exception as e: self.err("refresh_daily", e)

    async def refresh_lever(self):
        """Leverage per side from the account (the user changes it from the UI; crossed: one leverage for both sides) and the account's
        margin mode, which every order must carry; None leverage disables the margin gate."""
        self.lever_t = time.time()
        try:
            a = await self.rest(self.b.account, self.symbol)
            mode = a.get("marginMode") or self.b.margin_mode
            if mode != self.b.margin_mode: self.ev("MARGIN_MODE", mode=mode, was=self.b.margin_mode)
            self.b.margin_mode = mode
            for sd, bk in self.books.items():
                lv = float((a.get("crossedMarginLeverage") if mode == "crossed" else a.get("isolatedLongLever" if sd == "long" else "isolatedShortLever")) or 0) or None
                if lv != bk.lever: bk.ev("LEVER", lever=lv, was=bk.lever, avail=float(a.get("available") or 0))
                bk.lever = lv
            # the leverage is not a size (units are notional from the wallet share) but the margin each position locks, i.e. how loosely the
            # margin gate lets the basket pile up — one number for every book, or the brake differs by symbol. A symbol that joins the basket
            # arrives with the exchange's default (HYPEUSDT came at 20x, 2026-09-02), so the engine sets params `lever` itself, only while flat
            want = float(self.sp.get("lever") or 0); want_mode = self.sp.get("margin_mode")
            off = [bk.lever for bk in self.books.values() if bk.lever and abs(bk.lever - want) > 1e-9]
            flat = all(not bk.pos["lots"] and not bk.work["buy"] and not bk.work["trim"] for bk in self.books.values())
            # the margin mode is a contract too (the exchange keeps it per symbol): in isolated mode the liquidation price is the position's
            # own and the liquidation guard, not the money cap, becomes the stop (HYPE 20x isolated: stop at -3% instead of the cap's -10%,
            # two stop-outs -21.6 on 2026-09-02 — audit 7). Switched only while flat (the exchange refuses otherwise); alerted while positioned
            if want_mode and mode != want_mode and self.mode == "live":
                if flat:
                    try:
                        await self.rest(self.b.set_margin_mode, self.symbol, want_mode)
                        self.ev("MARGIN_MODE_SET", mode=want_mode, was=mode); self.b.margin_mode = mode = want_mode
                        prev = next((bk.lever for bk in self.books.values() if bk.lever), None)
                        for bk in self.books.values(): bk.lever = None            # the other mode's leverage: set below, whatever it reads
                        off = [prev] if want else []                              # the new mode's leverage is set unconditionally (its own field is unknown until the next read)
                    except Exception as e: self.err("set_margin_mode", e)
                elif time.time() - getattr(self, "mm_alert_t", 0.0) >= 3600:
                    self.mm_alert_t = time.time(); self.ev("MARGIN_MODE_MISMATCH", mode=mode, want=want_mode, lever=off[0] if off else want)
            if want and off and self.mode == "live" and flat:
                try:
                    if mode == "crossed": await self.rest(self.b.set_leverage, self.symbol, int(want))
                    else:
                        for sd in self.books: await self.rest(self.b.set_leverage, self.symbol, int(want), hold_side=sd)
                    self.ev("LEVER_SET", lever=want, was=off[0], mode=mode)
                    for bk in self.books.values(): bk.lever = want
                except Exception as e: self.err("set_leverage", e)
        except Exception as e: self.err("refresh_lever", e)

    async def shutdown(self, why):
        if self.mode == "live":                               # cancel our resting orders and wait for the exchange to confirm before leaving
            for bk in self.books.values(): await bk.cancel_all()
            left = None
            for _ in range(10):
                await asyncio.sleep(0.5)
                try:
                    pend = (await self.rest(self.b.pending_orders, self.symbol)).get("entrustedList") or []
                    left = [o["clientOid"] for o in pend if any((o.get("clientOid") or "").startswith(bk.OIDP) for bk in self.books.values())]
                    if not left: break
                    for o in pend:
                        if o.get("clientOid") in left:
                            try: await self.rest(self.b.cancel_order, self.symbol, o["orderId"])
                            except Exception: pass
                except Exception as e: self.err("shutdown", e)
            if left: self.ev("ERROR", where="shutdown", msg=f"orders still resting at exit: {left}")
        self.ev("EXIT", why=why); self.write_state(); os._exit(0)

    # ---- shared helpers ---------------------------------------------------------
    def fpx(self, x): return f"{x:.{self.pp}f}"
    def fq(self, q): return f"{round(q / self.qstep) * self.qstep:.{self.vp}f}"

    async def rest(self, fn, *a, **kw):
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*a, **kw))


if __name__ == "__main__":
    asyncio.run(Cycle(sys.argv[1] if len(sys.argv) > 1 else None).run())   # 심볼을 주면 그 심볼에 못박힌다(포트폴리오); 없으면 strat.symbol
