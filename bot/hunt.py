"""Pump-coin hunting pipeline — the SIDE pipeline (2026-09-03, user's experiment "until $1000"): one book, one side, the whole wallet,
the side chosen by the coin's lifecycle PHASE (bot/whale.py).
  python -m bot.supervise hunt      (python -m bot.hunt [--once] [--dry])

The basket selector (bot/scan.py + bot/select.py) is untouched and stays the contract; this module inherits its skeleton — scan ->
record every scan (logs/hunt.json, logs/hunt-history.jsonl) -> verdict on `confirm` consecutive scans -> params.json["books"] ->
wind-down -> flat -> drop -> the next coin. Two writers of `books` must never run at once: `hunt.on` = 1 makes this job the owner
(bot.select must be stopped; the job refuses to write while logs/select.pid is alive), `hunt.on` = 0 keeps it a report-only scanner.

THEORY (CONCEPT 실험 모드, 세력대항마): an operator runs a coin through phases and the phase decides our side —
  markup   -> LONG book: the cycle buys the shakeouts' deceleration (the sweep-and-reclaim) and trims into the pops;
  climax   -> the long stops adding (wind_down) and leaves at the next stall; nothing opens;
  markdown -> SHORT book: the cycle sells the bounces' deceleration and buys back the drops' stall;
  squeeze  -> the short leaves (late shorts are the operator's next meal); dead -> leave the coin; quiet/unknown -> nothing opens.
Footprints and thresholds: bot/whale.py (stage 1: candles + ticker; stage 2: CVD / OI / funding series and the fingerprint tables).
Common vetoes: 24h volume, ATR(1m) band the 1m rules were tuned in, contract leverage, churn; funding must not tax our side.
A phase exit does not start a cooldown (the same coin flips long -> short on the same scan it goes flat); an episode death does.
Rank among the eligible = 1h two-way path (churn) — the order for the empty slot, never a reason to replace a holding.
Events: HUNT (every scan) in logs/events.jsonl; HUNT_ADD / HUNT_WIND_DOWN / HUNT_DROP / HUNT_BLOCKED also in logs/alerts.jsonl.
State (streaks, cooldowns, the held coin's side / peak volume / climax high / exit reason) in logs/hunt-state.json."""
import json, os, statistics, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import Bitget
from bot.scan import PRODUCT
from bot.signal import wilder_atr
from bot.whale import footprints, phase as whale_phase, longer_history, WHALE
from bot.select import log, read_json, write_json, ev, flats_now, recent_engines, CHANNELS
from bot.ws import load_params, PARAMS, load_states

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
HUNT = dict(on=0,                # 1: this job owns params.books (bot.select stopped); 0: report only, never writes params or state
            every_min=10, confirm=2, exit_confirm=1, long_on=1, short_on=1,   # opening risk waits `confirm` scans; leaving is fast (`exit_confirm`, CONCEPT: the
            #                                                                   risk-opening side bears the higher bar). Scan often — the churn we eat is minutes-scale
            quiet_frac=0.5,      # leave when the last-24h two-way path falls under this share of the peak seen while held: the action left THIS coin, chase a hotter one
            min_vol=1e7,         # 24h quote volume floor (fills and footprint at this wallet)
            universe=1e7,        # the volume floor for pulling daily candles
            min_ratio=4.0,       # 24h volume over the median of the prior 7 UTC days: an episode (a fresh listing reads 99)
            min_twoway=15.0,     # 1h two-way path over the last 24h, %/day (TRUMP 20-36 on its good days, 12 on its dead one)
            min_atr=0.15, max_atr=1.2,   # ATR(1m) % band the 1m rules were tuned in / gappy prints above (USELESS 1.49)
            min_fund=-0.05,      # funding %/8h floor for a short (negative = shorts pay; T -0.29 would tax a short 0.9%/day)
            max_fund=0.3,        # funding %/8h ceiling for a long (longs crowded and paying)
            min_lever=10,        # the contract must allow at least this leverage (AKE max 10)
            exit_twoway=8.0,     # wind down when the last-24h two-way path is under this
            cooldown_h=24, exclude=["BTCUSDT"], record_top=3,
            min_hours=6)         # closed 1H bars a coin needs to be read (a listing a few hours old)

def _pct(a, b): return (a / b - 1) * 100 if b else 0.0

def measures(hours, minutes, bars15, days, ticker, held=None):
    """The shape from candles: whale.footprints (run / off / churn / structure / phase inputs) plus ATR(1m) %."""
    f = footprints(hours, bars15, days, ticker, held)
    f["qv_shape"] = f.pop("qv")                                  # the volume of the venue the shape came from (Binance for a young Bitget listing);
    atr1 = wilder_atr(minutes[-100:])                             # r["qv"] stays Bitget's 24h volume (the toll is paid there)
    f["atr_pct"] = round(atr1 / f["px"] * 100, 3) if atr1 else None
    f["phase"], f["votes"] = whale_phase(f)
    f["side"] = "long" if f["phase"] == "markup" else "short" if f["phase"] == "markdown" else None
    return f

def flags_of(r, hunt):
    """Entry vetoes (a flagged coin is not a candidate whatever its churn): the phase names the side, the rest is the toll."""
    f = []
    if r["qv"] < hunt["min_vol"]: f.append(f"vol{r['qv'] / 1e6:.0f}M")
    if r.get("atr_pct") is None or r["atr_pct"] > hunt["max_atr"] or r["atr_pct"] < hunt["min_atr"]: f.append(f"atr{r.get('atr_pct')}")
    if (r.get("lever_max") or 0) < hunt["min_lever"]: f.append(f"lever{r.get('lever_max')}")
    if (r.get("twoway24") or 0) < hunt["min_twoway"]: f.append(f"twoway{r.get('twoway24')}")
    ph = r.get("phase")
    if ph == "markup":
        if not hunt["long_on"]: f.append("long_off")
        if r["fund"] is not None and r["fund"] > hunt["max_fund"]: f.append(f"fund{r['fund']:+.2f}%")
    elif ph == "markdown":
        if not hunt["short_on"]: f.append("short_off")
        if r["fund"] is not None and r["fund"] < hunt["min_fund"]: f.append(f"fund{r['fund']:+.2f}%")
    else: f.append(f"phase:{ph}")
    return f

def exit_flags(r, held, hunt):
    """Why a held coin leaves: the phase turned against its side, the episode died, funding taxes it, or the tape went gappy."""
    f = []; side = held.get("side") or "short"; ph = r.get("phase")
    if r["qv"] < hunt["min_vol"]: f.append(f"vol{r['qv'] / 1e6:.0f}M")
    if r.get("dead"): f.append("dead")
    tw = r.get("twoway24") or 0.0; tw_peak = held.get("tw_peak") or 0.0
    if tw < hunt["exit_twoway"]: f.append(f"flat{tw}")                                          # absolute floor: no churn left to trade
    elif tw_peak >= hunt["min_twoway"] and tw < hunt["quiet_frac"] * tw_peak: f.append(f"quiet{tw:.0f}/{tw_peak:.0f}")   # the coin cooled off its own hot: chase
    if side == "long":
        if ph in ("climax", "markdown", "squeeze"): f.append(f"phase:{ph}")
        elif (r.get("off") or 0) >= WHALE["far_off"] and (r.get("off_close") or 0) >= WHALE["far_close"]: f.append(f"far{r.get('off')}")   # far under the top AND its highest close, whatever the structure reads (a bounce that flips
        if r["fund"] is not None and r["fund"] > hunt["max_fund"]: f.append(f"fund{r['fund']:+.2f}%")   # the 15m read to "long" 70% under the top is not a markup — audit 2026-09-03)
    else:
        if ph in ("markup", "squeeze"): f.append(f"phase:{ph}")
        if held.get("climax") and r["px"] > held["climax"]: f.append("newhigh")
        if r["fund"] is not None and r["fund"] < hunt["min_fund"]: f.append(f"fund{r['fund']:+.2f}%")
    return f                                                      # ATR is an entry question only: a held coin's ATR exploding is the pump itself

def scan(hunt, held=(), st=None, b=None, log=log):
    """Rows for every contract with 24h volume >= `universe` (daily candles for the ratio), the full shape for those with an episode
    (ratio >= min_ratio, a fresh listing counts) or held. Public REST only. Sorted: eligible by two-way path (desc), then by ratio."""
    b = b or Bitget("", "", ""); t0 = time.time(); hs = (st or {}).get("held") or {}
    contracts = {c["symbol"]: c for c in b.get("/api/v2/mix/market/contracts", auth=False, productType=PRODUCT) if c.get("symbolStatus") == "normal"}
    tickers = {t["symbol"]: t for t in b.get("/api/v2/mix/market/tickers", auth=False, productType=PRODUCT)}
    pool = [s for s, t in tickers.items() if s in contracts and s not in hunt["exclude"] and (float(t.get("quoteVolume") or 0) >= hunt["universe"] or s in held)]
    log(f"hunt: {len(pool)} contracts with 24h volume >= {hunt['universe'] / 1e6:.0f}M; daily candles ...")
    rows, dailies = [], {}
    for s in pool:
        t = tickers[s]; px = float(t["lastPr"]); qv = float(t.get("quoteVolume") or 0)
        try: days = b.candles(s, "1D", limit=10)[:-1]
        except Exception as e: log(f"  {s}: 1D failed {type(e).__name__}"); continue
        dailies[s] = days; prior = [d["qv"] for d in days[-7:]]
        base = statistics.median(prior) if len(prior) >= 3 else 0.0
        rows.append(dict(symbol=s, px=px, qv=qv, base7=base, ratio=99.0 if len(prior) < 3 else round(qv / base, 1), new=len(prior) < 3,
                         chg24=round(float(t.get("change24h") or 0) * 100, 1), fund=round(float(t.get("fundingRate") or 0) * 100, 3),
                         oi=float(t.get("holdingAmount") or 0) * px, spread_bp=round(_pct(float(t.get("askPr") or px), float(t.get("bidPr") or px)) * 100, 1),
                         lever_max=int(float(contracts[s].get("maxLever") or 0)), min_notional=float(contracts[s].get("minTradeNum") or 0) * px,
                         tick_pct=round(float(contracts[s].get("priceEndStep") or 1) * 10 ** -int(contracts[s].get("pricePlace") or 0) / px * 100, 4)))
    deep = [r for r in rows if r["ratio"] >= hunt["min_ratio"] or r["symbol"] in held]
    log(f"hunt: {len(deep)} with an episode (ratio >= {hunt['min_ratio']}x or new) or held; shapes ...")
    for r in deep:
        s = r["symbol"]
        try:
            hours = b.candles(s, "1H", limit=120)[:-1]; minutes = b.candles(s, "1m", limit=200)[:-1]; bars15 = b.candles(s, "15m", limit=200)[:-1]
            hours, bars15, days, src = longer_history(s, hours, bars15, dailies[s])          # a listing hours old on Bitget: Binance's chart for the shape
            if len(hours) < int(hunt["min_hours"]) or len(bars15) < 20: raise ValueError(f"too few bars ({len(hours)}h, {len(bars15)}x15m)")
            r.update(measures(hours, minutes, bars15, days, dict(qv=None if src == "binance" else r["qv"], fund=r["fund"]), hs.get(s))); r["src"] = src
        except Exception as e:
            log(f"  {s}: shape failed {type(e).__name__}: {str(e)[:60]}"); r.update(phase="unread", votes=[], side=None, twoway24=None, run=None, off=None, high48=None, atr_pct=None, hint15=None, dead=False)
        r["flags"] = flags_of(r, hunt)
    for r in rows:
        if "flags" not in r: r.update(phase="shallow", votes=[], side=None, twoway24=None, run=None, off=None, high48=None, atr_pct=None, hint15=None, dead=False, flags=[f"ratio{r['ratio']:.1f}x"])
    ok = sorted([r for r in rows if not r["flags"]], key=lambda r: -(r["twoway24"] or 0))
    rest = sorted([r for r in rows if r["flags"]], key=lambda r: -r["ratio"])
    log(f"hunt: {len(ok)} eligible, {len(rest)} flagged, {time.time() - t0:.0f}s")
    return ok + rest

def table(rows, n=15):
    out = [f"{'symbol':12}{'px':>10}{'qv24':>7}{'ratio':>7}{'chg24':>7}{'run':>6}{'off':>6}{'2way':>6}{'atr%':>6}{'fund':>7}{'lev':>4}{'hint':>6}  {'phase':9}{'side':6}flags | votes"]
    for r in rows[:n]:
        g = lambda k, d=0: r[k] if r.get(k) is not None else d
        out.append(f"{r['symbol']:12}{r['px']:>10.5g}{r['qv'] / 1e6:>6.0f}M{r['ratio']:>6.1f}x{r['chg24']:>+6.1f}%{g('run'):>6.0f}{g('off'):>6.1f}{g('twoway24'):>6.0f}"
                   f"{g('atr_pct'):>6.2f}{r['fund']:>+7.3f}{r.get('lever_max') or 0:>4}{str(r.get('hint15')):>6}  {str(r.get('phase')):9}{str(r.get('side')):6}{' '.join(r['flags'])} | {' '.join(r.get('votes') or [])}")
    return "\n".join(out)

def verdict(rows, books, hunt, st, now):
    """What this scan says. books = params.books. Returns dict(refuse|wind|add|top|cur); add/top = (symbol, side). A non-hunt book
    in `books` (a basket, a hand book) makes the job refuse: it never rewrites a basket. Streaks live in st (`streak`, `xstreak`)."""
    by = {r["symbol"]: r for r in rows}
    held = [s for s, bk in books.items() if bk.get("hunt")]
    other = [s for s in books if s not in held]
    if other: return dict(refuse=f"books holds non-hunt symbols {other}: stop this job or empty the basket first", cur=None, wind=None, add=None, top=None)
    if len(held) > 1: return dict(refuse=f"more than one hunt book {held}", cur=None, wind=None, add=None, top=None)
    cur = held[0] if held else None
    wind = resume = None
    if cur and books[cur].get("wind_down") and cur in by and not _leave_coin((st.get("held", {}).get(cur) or {}).get("exit", "")):
        # a phase-flip exit is undone when the read comes back to our side for `confirm` scans before the book is flat (a single bad
        # 15m close must not dump a good book: exit_confirm is 1 — audit 2026-09-03)
        r = by[cur]; mine = (books[cur].get("sides") or ["short"])[0]
        back = r.get("side") == mine and not exit_flags(r, st.get("held", {}).get(cur, {}), hunt)
        st.setdefault("rstreak", {})[cur] = st.get("rstreak", {}).get(cur, 0) + 1 if back else 0
        if back and st["rstreak"][cur] >= int(hunt["confirm"]): resume = cur; st["rstreak"][cur] = 0
    if cur and not books[cur].get("wind_down"):
        r = by.get(cur)
        if r and r.get("phase") not in ("unread", "shallow"):        # absent or unread = not evidence: keep
            hs = st.setdefault("held", {}).setdefault(cur, {})
            hs["peak"] = max(hs.get("peak") or 0.0, r.get("qv_shape") or r["qv"]); hs.setdefault("side", (books[cur].get("sides") or ["short"])[0])
            hs["tw_peak"] = max(hs.get("tw_peak") or 0.0, r.get("twoway24") or 0.0)   # the churn when this coin was hot: leaving reads against it (quiet_frac)
            xf = exit_flags(r, hs, hunt)
            st.setdefault("xstreak", {})[cur] = st.get("xstreak", {}).get(cur, 0) + 1 if xf else 0
            if xf and st["xstreak"][cur] >= int(hunt["exit_confirm"]): wind = (cur, ",".join(xf)); hs["exit"] = wind[1]
    cool = st.get("cool") or {}
    def free(r):   # not held, or the leaving book itself on the OTHER side (the lifecycle flip: a long wound down at the climax comes back short)
        bk = books.get(r["symbol"])
        return bk is None or (bk.get("wind_down") and r["side"] != (bk.get("sides") or [None])[0])
    cands = [(r["symbol"], r["side"]) for r in rows if not r["flags"] and r.get("side") and free(r) and cool.get(r["symbol"], 0) <= now]
    top = cands[0] if cands else None
    key = f"{top[0]}:{top[1]}" if top else None
    st["streak"] = {key: (st.get("streak") or {}).get(key, 0) + 1} if key else {}
    slot_open = cur is None or books[cur].get("wind_down") or wind is not None
    add = top if top and slot_open and st["streak"][key] >= int(hunt["confirm"]) else None
    if cur and not slot_open and top and top[0] == cur: add = None
    if resume: add = None                                                   # the book stays: nothing replaces it this scan
    return dict(refuse=None, cur=cur, wind=wind, add=add, top=top, resume=resume)

def _illiquid(why): return any(k in (why or "") for k in ("dead", "vol"))   # volume gone: dumping into thin books hurts — leave gently (stalls above cost, or the cap)
def _leave_coin(why): return _illiquid(why) or any(k in (why or "") for k in ("flat", "quiet"))   # the episode is over or the coin went quiet: cool down, chase a different one
#            everything else (a phase flip: climax / markdown / markup / squeeze / newhigh) is a same-coin side change — no cooldown, exit fast into a stall

def apply(p, rows, v, flats, hunt, st, now, recent=()):
    """Bring params.json to the verdict. One live hunt book at a time: the leaving book is dropped only when it is flat AND a
    replacement opens in the same write (books never empties — an empty `books` would send ws.portfolio() to whole-wallet
    strat.symbol on the common sides). The same coin may come back at once on the other side after a phase exit (no cooldown);
    an episode death starts the cooldown. Returns [(action, symbol, detail)]; nothing is written here."""
    by = {r["symbol"]: r for r in rows}; acts = []
    books = p.get("books") or {}; sp = p.setdefault("strat", {})
    if v["wind"]:
        s, why = v["wind"]
        if s in books and not books[s].get("wind_down"):
            books[s]["wind_down"] = 1                                       # no more adds; trims and the stop keep working
            if not _illiquid(why): books[s]["exit"] = 1                     # a phase flip OR the coin gone quiet: the engine sells the whole position into the next stall whatever the cost (leave fast)
            acts.append(("wind", s, why))                                   # (an episode death leaves gently: stalls above cost, or the cap)
    if v.get("resume"):                                                     # a phase-flip exit whose read reverted before the book was flat: undo it (audit 2026-09-03)
        s = v["resume"]
        if s in books and books[s].get("wind_down") and not _leave_coin((st.get("held", {}).get(s) or {}).get("exit", "")):
            books[s].pop("wind_down", None); books[s].pop("exit", None); st.get("held", {}).get(s, {}).pop("exit", None); acts.append(("resume", s, "phase back on our side"))
    leaving = [s for s in books if books[s].get("wind_down")]; live = [s for s in books if not books[s].get("wind_down")]
    add = v["add"]
    cooling = {s for s in leaving if _leave_coin((st.get("held", {}).get(s) or {}).get("exit", ""))}   # a quiet/dead leaver must not come straight back on the other side (audit 2026-09-03)
    if add and add[0] in cooling: add = None
    if add and not live and all(flats.get(s) for s in leaving):
        for s in leaving:
            why = (st.get("held", {}).get(s) or {}).get("exit", "")
            del books[s]
            if _leave_coin(why): st.setdefault("cool", {})[s] = now + float(hunt["cooldown_h"]) * 3600
            st.get("held", {}).pop(s, None); acts.append(("drop", s, f"flat ({why or 'replaced'})"))
        sym, side = add; r = by[sym]
        books[sym] = {"wallet_frac": 1.0, "sides": [side], "hunt": 1}
        st.setdefault("held", {})[sym] = dict(peak=r.get("qv_shape") or r["qv"], climax=r.get("high48"), side=side, t=now); st["streak"] = {}
        acts.append(("add", sym, f"{side} phase {r.get('phase')} votes {' '.join(r.get('votes') or [])} ratio {r['ratio']}x run {r.get('run')}% off {r.get('off')}% twoway {r.get('twoway24')} atr {r.get('atr_pct')} fund {r['fund']}"))
    if books: p["books"] = books
    held = list(books)
    if held and sp.get("symbol") not in held: sp["symbol"] = held[0]
    if held: sp["side"] = (books[held[0]].get("sides") or [sp.get("side")])[0]
    rec = {s: CHANNELS for s in held + [x for x in recent if x not in held]}
    for r in [r for r in rows if not r["flags"] and r["symbol"] not in rec][:int(hunt["record_top"])]: rec[r["symbol"]] = CHANNELS
    rec["BTCUSDT"] = ["candle1m"]; p["record"] = rec
    return acts

ALERT = {"add": "HUNT_ADD", "wind": "HUNT_WIND_DOWN", "drop": "HUNT_DROP", "resume": "HUNT_RESUME"}

def pid_alive(path):
    try: pid = int(open(path).read().strip())
    except Exception: return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, errors="replace").stdout
    return str(pid) in out

def main():
    once, dry = "--once" in sys.argv, "--dry" in sys.argv
    while True:
        p = load_params() or {}; hunt = {**HUNT, **(p.get("hunt") or {})}
        books = p.get("books") or {}; held = [s for s, bk in books.items() if bk.get("hunt")]
        t0 = time.time(); st = read_json(os.path.join(LOGS, "hunt-state.json"), {})
        try: rows = scan(hunt, held=tuple(held), st=st)
        except Exception as e: log(f"hunt scan failed: {type(e).__name__}: {e}"); rows = None
        if rows:
            os.makedirs(LOGS, exist_ok=True)
            rec = dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), hunt={k: v for k, v in hunt.items() if k != "exclude"}, whale=WHALE, rows=rows)
            write_json(os.path.join(LOGS, "hunt.json"), rec)
            with open(os.path.join(LOGS, "hunt-history.jsonl"), "a", encoding="utf-8") as f: f.write(json.dumps(rec) + "\n")
            log("\n" + table(rows))
            now = time.time(); flats = flats_now()
            v = verdict(rows, books, hunt, st, now)
            owner = bool(hunt.get("on")) and not dry
            ev("HUNT", on=int(bool(hunt.get("on"))), dry=dry, held=held, flat={s: flats.get(s) for s in held}, cur=v["cur"], wind=v["wind"], add=v["add"], top=v["top"],
               refuse=v["refuse"], streak=st.get("streak"), xstreak={s: st.get("xstreak", {}).get(s) for s in held},
               phases={r["symbol"]: [r.get("phase"), r.get("votes")] for r in rows if r.get("phase") not in ("shallow", None)}, took_s=int(now - t0),
               rows=[[r["symbol"], r.get("phase"), r.get("side"), r["ratio"], r.get("run"), r.get("off"), r.get("twoway24"), r.get("atr_pct"), r["fund"], r.get("hint15"), " ".join(r["flags"])] for r in rows[:8]])
            if v["refuse"]: log(f"hunt: {v['refuse']}")
            elif owner:
                if pid_alive(os.path.join(LOGS, "select.pid")):
                    ev("HUNT_BLOCKED", alert=True, why="bot.select is running: two writers of params.books — stop it (or set hunt.on 0)")
                else:
                    before = json.dumps(p, sort_keys=True)
                    acts = apply(p, rows, v, flats, hunt, st, now, recent=recent_engines(load_states(), now))
                    if json.dumps(p, sort_keys=True) != before: write_json(PARAMS, p, indent=2)
                    for kind, sym, detail in acts:
                        ev(ALERT[kind], alert=True, symbol=sym, why=detail, books={s: (b.get("sides") or [None])[0] for s, b in (p.get("books") or {}).items()})
                    write_json(os.path.join(LOGS, "hunt-state.json"), st)
            else:
                log(f"hunt: report only ({'--dry' if dry else 'hunt.on=0'}); would: wind={v['wind']} add={v['add']} top={v['top']}")
        if once: break
        time.sleep(max(60, float(hunt["every_min"]) * 60 - (time.time() - t0)))

if __name__ == "__main__":
    main()
