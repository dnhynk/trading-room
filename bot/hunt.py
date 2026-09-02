"""Short-hunting pipeline for pump coins — the SIDE pipeline (2026-09-03, user's experiment "until $1000").
  python -m bot.supervise hunt      (python -m bot.hunt [--once] [--dry])

The basket selector (bot/scan.py + bot/select.py) is untouched and stays the contract; this module inherits its skeleton — scan ->
record every scan (logs/hunt.json, logs/hunt-history.jsonl) -> verdict on `confirm` consecutive scans -> params.json["books"] ->
wind-down -> flat -> drop -> the next coin — for ONE book, ONE side, the whole wallet. Two writers of `books` must never run at once:
`hunt.on` = 1 makes this job the owner (bot.select must be stopped; the job refuses to write while logs/select.pid is alive), `hunt.on`
= 0 keeps it a report-only scanner that can run beside the basket selector forever.

THEORY (CONCEPT 종목과 사이징, 실험 모드): the cycle earns in churn and the money is direction; on a pump coin the direction with
the best base rate is the decline after the climax, so the short cycle sells the deceleration of bounces and buys back the stall of
drops. A coin is hunted while its EPISODE is alive and the top is in:
  - episode: 24h volume >= `min_ratio` x the median of its own prior 7 UTC days (fresh money, measured against the symbol's own
    baseline — the basket scanner's 3-day median level is what hid TRUMP's decay, NEXT 6), and >= `min_vol` absolute;
  - climax done: a run of >= `min_run` % into the 48h high and the price now >= `min_off` % under it (the short sells bounces
    after the top, never the top), and the engine's own 15m structure read (signal.structure_side) says "short" when `structure` = 1;
  - something to harvest: 1h two-way path over the last 24h >= `min_twoway` %/day (TRUMP's good days 20-36, its dead day 12);
  - the engine's 1m rules were tuned at ATR(1m) 0.15-0.7%: `min_atr` <= ATR% <= `max_atr` (USELESS at 1.49% printed gaps);
  - shorts must not be taxed: funding >= `min_fund` %/8h; the contract must allow `min_lever`.
It LEAVES (wind_down, `confirm` consecutive scans; never a market dump, CONCEPT) when the episode dies — 24h volume under
`exit_ratio` x the peak seen while held, or two-way path under `exit_twoway` — or the top is not in after all: a new high over the
climax recorded at entry, or the 15m structure turning "long". A dropped coin waits `cooldown_h`. Rank among the eligible = two-way
path (churn); the score is an order for the empty slot, never a reason to replace a holding (RULES select 절, kept). Sizing is the
engine's (`unit_frac`, `cap_frac`, `cap_min_atr`) — this job only names the coin.
Events: HUNT (every scan) in logs/events.jsonl; HUNT_ADD / HUNT_WIND_DOWN / HUNT_DROP / HUNT_BLOCKED also in logs/alerts.jsonl.
State (add/exit streaks, cooldowns, the held coin's peak volume and climax high) in logs/hunt-state.json."""
import json, os, statistics, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import Bitget
from bot.scan import PRODUCT
from bot.signal import wilder_atr, structure_side
from bot.select import log, read_json, write_json, ev, flats_now, recent_engines, CHANNELS
from bot.ws import load_params, PARAMS, load_states

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
HUNT = dict(on=0,                # 1: this job owns params.books (bot.select stopped); 0: report only, never writes params or state
            every_min=15, side="short", confirm=2,
            min_vol=1e7,         # 24h quote volume floor (fills and footprint at this wallet)
            min_ratio=4.0,       # 24h volume over the median of the prior 7 UTC days: an episode (UAI 14x, T 250x, MAGMA 1.8x on 09-02)
            min_run=25.0,        # % run into the 48h high from the low before it: a pump, not a drift
            min_off=3.0,         # % under the 48h high now: the top is in (UAI 09-03 01:50 ~10%)
            min_twoway=15.0,     # 1h two-way path over the last 24h, %/day (TRUMP 20-36 on its good days, 12 on its dead one)
            min_atr=0.15, max_atr=1.2,   # ATR(1m) % band the 1m rules were tuned in / gappy prints above
            min_fund=-0.05,      # funding %/8h floor (negative = shorts pay; T -0.29 would tax a short 0.9%/day)
            min_lever=20,        # the contract must allow the leverage the engine sets (strat.lever)
            structure=1,         # require signal.structure_side on 15m bars == side
            exit_ratio=0.25,     # wind down when 24h volume < exit_ratio x the peak seen while held (TRUMP's best day was at 1/3 of peak)
            exit_twoway=8.0,     # wind down when the last-24h two-way path is under this
            cooldown_h=24, exclude=["BTCUSDT"], record_top=3, universe=1e7)   # universe: the volume floor for pulling daily candles

def _pct(a, b): return (a / b - 1) * 100 if b else 0.0

def measures(hours, minutes, bars15, px):
    """The shape numbers from candles: hours = closed 1H (oldest->newest, >= 24), minutes = closed 1m, bars15 = closed 15m."""
    h24 = hours[-24:]; h6 = hours[-6:]; o24 = h24[0]["o"]; o6 = h6[0]["o"]
    twoway24 = sum(abs(x["c"] - x["o"]) / o24 for x in h24) * 100 - abs(_pct(h24[-1]["c"], o24))
    twoway6 = sum(abs(x["c"] - x["o"]) / o6 for x in h6) * 100 - abs(_pct(h6[-1]["c"], o6))
    net24 = _pct(h24[-1]["c"], o24)
    h48 = hours[-48:]; i = max(range(len(h48)), key=lambda k: h48[k]["h"]); high48 = h48[i]["h"]
    before = hours[max(0, len(hours) - 48 + i - 72):len(hours) - 48 + i + 1] or h48[:i + 1]
    run = _pct(high48, min(x["l"] for x in before)); off = -_pct(px, high48)
    atr1 = wilder_atr(minutes[-100:]); atr15 = wilder_atr(bars15)
    return dict(twoway24=round(twoway24, 1), twoway6=round(twoway6, 1), net24=round(net24, 1), run=round(run, 1), off=round(off, 1),
                high48=high48, atr_pct=round(atr1 / px * 100, 3) if atr1 else None, atr15_pct=round(atr15 / px * 100, 2) if atr15 else None,
                hint15=structure_side(bars15, atr15) if atr15 else None)

def flags_of(r, hunt):
    """Entry vetoes (a flagged coin is not a candidate whatever its churn)."""
    f = []
    if r["qv"] < hunt["min_vol"]: f.append(f"vol{r['qv'] / 1e6:.0f}M")
    if r["ratio"] < hunt["min_ratio"]: f.append(f"ratio{r['ratio']:.1f}x")
    if r["run"] < hunt["min_run"]: f.append(f"run{r['run']:.0f}%")
    if r["off"] < hunt["min_off"]: f.append(f"top{r['off']:.1f}%")
    if r["twoway24"] < hunt["min_twoway"]: f.append(f"twoway{r['twoway24']:.0f}")
    if r["atr_pct"] is None or r["atr_pct"] > hunt["max_atr"] or r["atr_pct"] < hunt["min_atr"]: f.append(f"atr{r['atr_pct']}")
    if r["fund"] < hunt["min_fund"]: f.append(f"fund{r['fund']:+.2f}%")
    if (r.get("lever_max") or 0) < hunt["min_lever"]: f.append(f"lever{r.get('lever_max')}")
    if hunt["structure"] and r.get("hint15") != hunt["side"]: f.append(f"hint{r.get('hint15')}")
    return f

def exit_flags(r, held, hunt):
    """Why a held coin leaves: the episode died or the top was not in. `held` = the state kept for it (peak volume, climax high)."""
    f = []
    if r["qv"] < hunt["min_vol"]: f.append(f"vol{r['qv'] / 1e6:.0f}M")
    peak = max(held.get("peak") or 0.0, r["qv"])
    if r["qv"] < hunt["exit_ratio"] * peak: f.append(f"dead{r['qv'] / peak:.2f}")
    if r["twoway24"] < hunt["exit_twoway"]: f.append(f"flat{r['twoway24']:.0f}")
    if held.get("climax") and r["px"] > held["climax"]: f.append("newhigh")
    if r["fund"] < hunt["min_fund"]: f.append(f"fund{r['fund']:+.2f}%")
    if r["atr_pct"] is not None and r["atr_pct"] > hunt["max_atr"]: f.append(f"atr{r['atr_pct']}")
    if hunt["structure"] and r.get("hint15") not in (None, hunt["side"]): f.append(f"hint{r.get('hint15')}")
    return f

def scan(hunt, held=(), b=None, log=log):
    """Rows for every contract with 24h volume >= `universe` (daily candles for the ratio), the full shape for those with an episode
    (ratio >= min_ratio) or held. Public REST only. Sorted: eligible by two-way path (desc), then the rest by ratio."""
    b = b or Bitget("", "", ""); t0 = time.time()
    contracts = {c["symbol"]: c for c in b.get("/api/v2/mix/market/contracts", auth=False, productType=PRODUCT) if c.get("symbolStatus") == "normal"}
    tickers = {t["symbol"]: t for t in b.get("/api/v2/mix/market/tickers", auth=False, productType=PRODUCT)}
    pool = [s for s, t in tickers.items() if s in contracts and s not in hunt["exclude"] and (float(t.get("quoteVolume") or 0) >= hunt["universe"] or s in held)]
    log(f"hunt: {len(pool)} contracts with 24h volume >= {hunt['universe'] / 1e6:.0f}M; daily candles ...")
    rows = []
    for s in pool:
        t = tickers[s]; px = float(t["lastPr"]); qv = float(t.get("quoteVolume") or 0)
        try: days = b.candles(s, "1D", limit=10)
        except Exception as e: log(f"  {s}: 1D failed {type(e).__name__}"); continue
        prior = [d["qv"] for d in days[-8:-1]]
        base = statistics.median(prior) if len(prior) >= 3 else 0.0
        ratio = qv / base if base else 0.0
        rows.append(dict(symbol=s, px=px, qv=qv, base7=base, ratio=round(ratio, 1), chg24=round(float(t.get("change24h") or 0) * 100, 1),
                         fund=round(float(t.get("fundingRate") or 0) * 100, 3), oi=float(t.get("holdingAmount") or 0) * px,
                         spread_bp=round(_pct(float(t.get("askPr") or px), float(t.get("bidPr") or px)) * 100, 1),
                         lever_max=int(float(contracts[s].get("maxLever") or 0)), min_notional=float(contracts[s].get("minTradeNum") or 0) * px,
                         tick_pct=round(float(contracts[s].get("priceEndStep") or 1) * 10 ** -int(contracts[s].get("pricePlace") or 0) / px * 100, 4)))
    deep = [r for r in rows if r["ratio"] >= hunt["min_ratio"] or r["symbol"] in held]
    log(f"hunt: {len(deep)} with an episode (ratio >= {hunt['min_ratio']}x) or held; shapes ...")
    for r in deep:
        s = r["symbol"]
        try:
            hours = b.candles(s, "1H", limit=120)[:-1]; minutes = b.candles(s, "1m", limit=200)[:-1]; bars15 = b.candles(s, "15m", limit=200)[:-1]
            if len(hours) < 24: raise ValueError("too few 1H bars")
            r.update(measures(hours, minutes, bars15, r["px"]))
        except Exception as e:
            log(f"  {s}: shape failed {type(e).__name__}: {str(e)[:60]}"); r.update(twoway24=0.0, twoway6=0.0, net24=0.0, run=0.0, off=0.0, high48=None, atr_pct=None, atr15_pct=None, hint15=None)
        r["flags"] = flags_of(r, hunt)
    for r in rows:
        if "flags" not in r: r.update(twoway24=None, twoway6=None, net24=None, run=None, off=None, high48=None, atr_pct=None, atr15_pct=None, hint15=None, flags=[f"ratio{r['ratio']:.1f}x"])
    ok = sorted([r for r in rows if not r["flags"]], key=lambda r: -r["twoway24"])
    rest = sorted([r for r in rows if r["flags"]], key=lambda r: -r["ratio"])
    log(f"hunt: {len(ok)} eligible, {len(rest)} flagged, {time.time() - t0:.0f}s")
    return ok + rest

def table(rows, n=15):
    out = [f"{'symbol':12}{'px':>10}{'qv24':>7}{'ratio':>7}{'chg24':>7}{'run':>6}{'off':>6}{'2way24':>7}{'2way6':>6}{'atr%':>6}{'fund':>7}{'lev':>4}{'hint':>6}  flags"]
    for r in rows[:n]:
        out.append(f"{r['symbol']:12}{r['px']:>10.5g}{r['qv'] / 1e6:>6.0f}M{r['ratio']:>6.1f}x{r['chg24']:>+6.1f}%"
                   f"{(r['run'] if r['run'] is not None else 0):>6.0f}{(r['off'] if r['off'] is not None else 0):>6.1f}"
                   f"{(r['twoway24'] if r['twoway24'] is not None else 0):>7.0f}{(r['twoway6'] if r['twoway6'] is not None else 0):>6.0f}"
                   f"{(r['atr_pct'] if r['atr_pct'] is not None else 0):>6.2f}{r['fund']:>+7.3f}{r.get('lever_max') or 0:>4}{str(r.get('hint15')):>6}  {' '.join(r['flags'])}")
    return "\n".join(out)

def verdict(rows, books, hunt, st, now):
    """What this scan says. books = params.books. Returns dict(refuse|wind|add|top|cur). A non-hunt book in `books` (a basket, a hand
    book) makes the job refuse: it never rewrites a basket. Streaks live in st (`streak` adds, `xstreak` exits)."""
    by = {r["symbol"]: r for r in rows}
    held = [s for s, bk in books.items() if bk.get("hunt")]
    other = [s for s in books if s not in held]
    if other: return dict(refuse=f"books holds non-hunt symbols {other}: stop this job or empty the basket first", cur=None, wind=None, add=None, top=None)
    if len(held) > 1: return dict(refuse=f"more than one hunt book {held}", cur=None, wind=None, add=None, top=None)
    cur = held[0] if held else None
    wind = None
    if cur and not books[cur].get("wind_down"):
        r = by.get(cur)
        if r:                                                      # absent from the scan = not evidence: keep
            hs = st.setdefault("held", {}).setdefault(cur, {})
            hs["peak"] = max(hs.get("peak") or 0.0, r["qv"])
            xf = exit_flags(r, hs, hunt)
            st.setdefault("xstreak", {})[cur] = st.get("xstreak", {}).get(cur, 0) + 1 if xf else 0
            if xf and st["xstreak"][cur] >= int(hunt["confirm"]): wind = (cur, ",".join(xf))
    cool = st.get("cool") or {}
    cands = [r["symbol"] for r in rows if not r["flags"] and r["symbol"] not in books and cool.get(r["symbol"], 0) <= now]
    top = cands[0] if cands else None
    st["streak"] = {top: (st.get("streak") or {}).get(top, 0) + 1} if top else {}
    slot_open = cur is None or books[cur].get("wind_down") or wind is not None
    add = top if top and slot_open and st["streak"][top] >= int(hunt["confirm"]) else None
    return dict(refuse=None, cur=cur, wind=wind, add=add, top=top)

def apply(p, rows, v, flats, hunt, st, now, recent=()):
    """Bring params.json to the verdict. One live hunt book at a time: the leaving book is dropped only when it is flat AND a
    replacement opens in the same write (books never empties — an empty `books` would send ws.portfolio() to whole-wallet
    strat.symbol on the common sides). Returns [(action, symbol, detail)]; nothing is written here."""
    by = {r["symbol"]: r for r in rows}; acts = []
    books = p.get("books") or {}; sp = p.setdefault("strat", {})
    if v["wind"]:
        s, why = v["wind"]
        if s in books and not books[s].get("wind_down"): books[s]["wind_down"] = 1; acts.append(("wind", s, why))
    leaving = [s for s in books if books[s].get("wind_down")]; live = [s for s in books if not books[s].get("wind_down")]
    if v["add"] and not live and all(flats.get(s) for s in leaving):
        for s in leaving:
            del books[s]; st.setdefault("cool", {})[s] = now + float(hunt["cooldown_h"]) * 3600; st.get("held", {}).pop(s, None)
            acts.append(("drop", s, "flat"))
        r = by[v["add"]]
        books[v["add"]] = {"wallet_frac": 1.0, "sides": [hunt["side"]], "hunt": 1}
        st.setdefault("held", {})[v["add"]] = dict(peak=r["qv"], climax=r.get("high48"), t=now); st["streak"] = {}
        acts.append(("add", v["add"], f"ratio {r['ratio']}x run {r['run']}% off {r['off']}% twoway {r['twoway24']} atr {r['atr_pct']} hint {r.get('hint15')}"))
    if books: p["books"] = books
    held = list(books)
    if held and sp.get("symbol") not in held: sp["symbol"] = held[0]; sp["side"] = hunt["side"]
    rec = {s: CHANNELS for s in held + [x for x in recent if x not in held]}
    for r in [r for r in rows if not r["flags"] and r["symbol"] not in rec][:int(hunt["record_top"])]: rec[r["symbol"]] = CHANNELS
    rec["BTCUSDT"] = ["candle1m"]; p["record"] = rec
    return acts

ALERT = {"add": "HUNT_ADD", "wind": "HUNT_WIND_DOWN", "drop": "HUNT_DROP"}

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
        t0 = time.time()
        try: rows = scan(hunt, held=tuple(held))
        except Exception as e: log(f"hunt scan failed: {type(e).__name__}: {e}"); rows = None
        if rows:
            os.makedirs(LOGS, exist_ok=True)
            rec = dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), hunt={k: v for k, v in hunt.items() if k != "exclude"}, rows=rows)
            write_json(os.path.join(LOGS, "hunt.json"), rec)
            with open(os.path.join(LOGS, "hunt-history.jsonl"), "a", encoding="utf-8") as f: f.write(json.dumps(rec) + "\n")
            log("\n" + table(rows))
            now = time.time(); st = read_json(os.path.join(LOGS, "hunt-state.json"), {})
            flats = flats_now()
            v = verdict(rows, books, hunt, st, now)
            owner = bool(hunt.get("on")) and not dry
            ev("HUNT", on=int(bool(hunt.get("on"))), dry=dry, held=held, flat={s: flats.get(s) for s in held}, cur=v["cur"], wind=v["wind"], add=v["add"], top=v["top"],
               refuse=v["refuse"], streak=st.get("streak"), xstreak={s: st.get("xstreak", {}).get(s) for s in held}, took_s=int(now - t0),
               rows=[[r["symbol"], r["ratio"], r["run"], r["off"], r["twoway24"], r["atr_pct"], r["fund"], r.get("hint15"), " ".join(r["flags"])] for r in rows[:8]])
            if v["refuse"]: log(f"hunt: {v['refuse']}")
            elif owner:
                if pid_alive(os.path.join(LOGS, "select.pid")):
                    ev("HUNT_BLOCKED", alert=True, why="bot.select is running: two writers of params.books — stop it (or set hunt.on 0)")
                else:
                    before = json.dumps(p, sort_keys=True)
                    acts = apply(p, rows, v, flats, hunt, st, now, recent=recent_engines(load_states(), now))
                    if json.dumps(p, sort_keys=True) != before: write_json(PARAMS, p, indent=2)
                    for kind, sym, detail in acts:
                        ev(ALERT[kind], alert=True, symbol=sym, why=detail, books=list(p.get("books") or {}))
                    write_json(os.path.join(LOGS, "hunt-state.json"), st)
            else:
                log(f"hunt: report only ({'--dry' if dry else 'hunt.on=0'}); would: wind={v['wind']} add={v['add']} top={v['top']}")
        if once: break
        time.sleep(max(60, float(hunt["every_min"]) * 60 - (time.time() - t0)))

if __name__ == "__main__":
    main()
