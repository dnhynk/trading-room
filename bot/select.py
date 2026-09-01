"""Basket manager for the engine.  python -m bot.supervise select   (python -m bot.select [--once] [--dry])
Every select.every_h hours (and at start) `bot.scan.rank` scores the universe, logs/scan.json is rewritten, and `params.json["books"]`
is brought toward the target basket of `select.n` symbols. One engine per book (bot/supervise.py `cycle`), equal weight
(`wallet_frac` = 1/n on every book, so the sum never exceeds one wallet even mid-change).

WHAT DECIDES WHAT (2026-09-01, after 4 days of live results were compared against a backtest instead of against each other):
  - A book LEAVES only on a fact — a hard flag from the scan: 24h volume under the gate, imp% >= fee%, tick, spread, funding, pump
    shape, one-way now. Never because something else scored higher today. No offline ranker is validated at this sample size: the
    candle proxy inverted the tick engine's order 4 times out of 4, the tick backtest's own noise is 3x the difference it was asked
    to measure, and the current score's p_up is measured against live to be low and four times too flat (RULES 도구 절). A rank-based
    replacement rule is what a 1.5x `concept` hurdle was, and that ranking had no cost term at all.
  - A book ENTERS on the score, which is only ever used to ORDER the eligible set: the top entry-eligible candidates fill free slots,
    after `select.confirm` consecutive scans (hysteresis) and within `select.max_per_day` openings a day. The score's own gate is
    soft — a pessimistic estimator may keep us out of a symbol and must never push us out of one.
  - RANKING BETWEEN HELD SYMBOLS is live evidence only, and there is not enough of it yet: the exit rule for a symbol that simply
    earns less than its neighbours is the open item, and its unit is per-symbol live results (NEXT 8), never a backtest.
Holding n symbols instead of one is what makes that discipline affordable: the wallet splits n ways so imp% falls with sqrt(n), a bad
pick costs 1/n of the book instead of all of it, and every day produces n paired same-clock observations of the one comparison that
is allowed — live against live.

WIND-DOWN. A flagged book is not closed at market; 순환매 sells into a stall (CONCEPT). `books[sym]["wind_down"] = 1` stops new
entries (cycle.py treats it as a per-symbol PAUSE), trims and stops keep working, and the key is removed once that engine reports
flat (read from the state files AFTER the scan — a scan takes over a minute and the flat window is 60 s) — at which point its engine
exits by itself (cycle.py: a pinned symbol that left `books` is a contract change) and the slot is free. A wound-down book whose flag
is gone on a later scan (ER is re-judged every scan) adds again (BOOK_RESUME). `books` never becomes empty: the last book stays, wound
down, rather than falling back to whole-wallet sizing on strat.symbol.

Also written by this job: `strat.symbol` (the highest-ranked held symbol — the default for bot.trade and for an unpinned engine),
`strat.side` (that symbol's 1H structure, a record; 쌍검 `sides` is preserved), and `record` = every book + engines seen within a day
(a book that just left keeps its tape) + the top `select.record_top` unflagged candidates + `select.record_extra` (a watchlist,
recorded whatever its flags say, never traded here) + BTCUSDT candles. Everything else in params.json belongs to the person.
The engine geometry the score charges (win / loss / fee per cycle) is re-measured from the live ledger at every scan (bot.cycles
.geometry, >= 200 cycles; else scan.EDGE; `select.edge` overrides both).
Events: SELECT (every scan's verdict) in logs/events.jsonl; BOOK_ADD / BOOK_WIND_DOWN / BOOK_RESUME / BOOK_DROP also in logs/alerts.jsonl.
State (add streaks, openings today) in logs/select-state.json. --once runs one scan; --dry scans and decides, and writes only the
report (logs/scan.json) — never params.json or the state."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.scan import rank
from bot.cycles import geometry
from bot.ws import load_params, PARAMS, load_states, portfolio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
SELECT = dict(every_h=4, n=4, confirm=2, max_per_day=2, record_top=5, min_vol=5e7, days=3, exclude=["BTCUSDT"], record_extra=[])
CHANNELS = ["trade", "books15", "ticker", "candle1m"]

def log(s): print(time.strftime("%Y-%m-%d %H:%M:%S ") + s, flush=True)

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def write_json(path, obj, indent=None):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, indent=indent)
    os.replace(tmp, path)

def ev(kind, alert=False, **kw):
    line = json.dumps(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), ev=kind, **kw), ensure_ascii=False)
    os.makedirs(LOGS, exist_ok=True)
    with open(os.path.join(LOGS, "events.jsonl"), "a", encoding="utf-8") as f: f.write(line + "\n")
    if alert:
        with open(os.path.join(LOGS, "alerts.jsonl"), "a", encoding="utf-8") as f: f.write(line + "\n")
    log(line[:300])

def flat_of(states, now):
    """{symbol: True} for each engine that holds nothing, rests nothing and wrote its snapshot within 60 s. An engine that is down is
    not flat — its position is unobserved, and a book is only removed on an observation."""
    out = {}
    for sym, s in (states or {}).items():
        try:
            age = now - time.mktime(time.strptime(s["t"], "%Y-%m-%d %H:%M:%S"))
            books = s.get("books") or {s.get("side", "?"): s}
            out[sym] = age < 60 and all(not b["pos"]["lots"] and not b["working"]["buy"] and not b["working"]["trim"] and not b.get("pull")
                                        for b in books.values())
        except Exception: out[sym] = False
    return out

def flats_now():
    """The engines' flat verdicts read at this moment. Read after the scan, never before it: a scan takes over a minute and a state file
    older than 60 s is not flat by definition, so states read before the scan could never say flat and BOOK_DROP never fired (every
    SELECT verdict through 2026-09-02 shows flat=False for every book, the wound-down one included)."""
    return flat_of(load_states(), time.time())

def recent_engines(states, now, hours=24):
    """Symbols whose engine wrote a state within `hours`: a book that just left keeps its tape for a day (the post-mortem needs it)."""
    out = []
    for sym, s in (states or {}).items():
        try:
            if now - time.mktime(time.strptime(s["t"], "%Y-%m-%d %H:%M:%S")) < hours * 3600: out.append(sym)
        except Exception: pass
    return out

def record_dict(held, rows, sel, recent=()):
    """Full channels for every book plus the top unflagged candidates — a symbol has a tape before it is ever traded, and a symbol we
    dropped keeps one for a day (`recent`). `record_extra` is a watchlist: recorded whatever its flags say, never a candidate here."""
    rec = {s: CHANNELS for s in list(held) + [x for x in recent if x not in held]}
    ok = [r for r in rows if not r["flags"] and r["symbol"] not in rec and r["symbol"] not in sel["exclude"]]
    for r in ok[:int(sel["record_top"])]: rec[r["symbol"]] = CHANNELS
    for x in sel.get("record_extra") or []: rec.setdefault(x, CHANNELS)
    rec["BTCUSDT"] = ["candle1m"]
    return rec

def plan(rows, held, sel, st, today):
    """Pure verdict: (wind, adds, why). `wind` = [(symbol, why)] holdings that must leave, `adds` = symbols to open now, in order and
    at most the number of free slots. Mutates st['streak'] (the add hysteresis).
    A holding missing from the scan is kept: absence is not evidence. A holding with a flag leaves whatever its rank — that
    asymmetry is the whole point (2026-09-01: the incumbent was exempted from the volume gate and traded 12 more hours at
    -0.140%/cycle)."""
    by = {r["symbol"]: r for r in rows}
    n = max(int(sel["n"]), 1); why = []; wind = []
    for s in held:
        r = by.get(s)
        if r is None: why.append(f"{s} not in the scan: kept")
        elif r["flags"]: wind.append((s, " ".join(r["flags"])))
    free = n - len(held)
    cand = [r for r in rows if not r["flags"] and r.get("entry") and r["symbol"] not in held and r["symbol"] not in sel["exclude"]]
    top = [r["symbol"] for r in cand[:max(free, 0)]]
    st["streak"] = {s: st.get("streak", {}).get(s, 0) + 1 for s in top}          # a streak survives only while the symbol stays in the top slots
    left = int(sel["max_per_day"]) - (st.get("opens", 0) if st.get("day") == today else 0)
    adds = [s for s in top if st["streak"][s] >= int(sel["confirm"])][:max(left, 0)]
    if free <= 0: why.append(f"basket full {len(held)}/{n}")
    elif not cand: why.append(f"{free} free slot(s), no entry-eligible candidate")
    else: why.append(f"{free} free slot(s); " + ", ".join(f"{r['symbol']} {r['edge']:+.3f} ({st['streak'].get(r['symbol'], 0)}/{int(sel['confirm'])})" for r in cand[:4]))
    if adds and left <= 0: why.append(f"{sel['max_per_day']} openings already today")
    return wind, adds, "; ".join(why)

def apply(p, rows, wind, adds, flats, sel, recent=()):
    """Bring params.json to the planned basket. Returns [(action, symbol, detail)] — nothing is written by this function."""
    by = {r["symbol"]: r for r in rows}; n = max(int(sel["n"]), 1); acts = []
    sp = p.setdefault("strat", {})
    books = p.get("books")
    if not books: books = {sp.get("symbol"): {}} if sp.get("symbol") else {}      # first run in basket mode: the running symbol becomes book one
    for s, w in wind:
        if not books.get(s, {}).get("wind_down"): books.setdefault(s, {})["wind_down"] = 1; acts.append(("wind", s, w))
    for s in [s for s in books if books[s].get("wind_down") and s in by and not by[s]["flags"]]:
        del books[s]["wind_down"]; acts.append(("resume", s, "flags cleared"))    # the fact that sent it out is gone (ER is re-judged every scan): it adds again
    for s in [s for s in books if books[s].get("wind_down") and flats.get(s)]:
        if len(books) > 1 or adds: del books[s]; acts.append(("drop", s, "flat"))   # never ends empty: the last book stays, wound down, unless a replacement opens now
    for s in adds:
        if len(books) < n and s not in books: books[s] = {}; acts.append(("add", s, f"edge {by[s]['edge']:+.3f} p_up {by[s]['p_up']:.2f} imp {by[s]['impact']:.4f}"))
    for s in books: books[s]["wallet_frac"] = round(1.0 / n, 6)                   # 1/n, not 1/len(books): the sum stays <= 1 while a slot is empty
    if books: p["books"] = books                                                  # an empty books would send ws.portfolio() back to whole-wallet strat.symbol
    held = list(books)
    if sp.get("symbol") not in held and held:                                     # the default symbol for bot.trade and for an unpinned engine
        first = next((r["symbol"] for r in rows if r["symbol"] in held), held[0])
        sp["symbol"] = first
        if (by.get(first) or {}).get("side") in ("long", "short"): sp["side"] = by[first]["side"]
    p["record"] = record_dict(held, rows, sel, recent)
    return acts

def main():
    once, dry = "--once" in sys.argv, "--dry" in sys.argv
    while True:
        p = load_params() or {}; sel = {**SELECT, **(p.get("select") or {})}
        held = [s for s in portfolio(p) if s]
        t0 = time.time(); states = load_states()
        eq = next((((s.get("acct") or {}).get("equity")) for s in states.values() if (s.get("acct") or {}).get("equity")), None)
        edge = sel.get("edge") or geometry()                 # the engine's measured geometry, re-read from the live ledger every scan (None: scan.EDGE)
        try: rows = rank(min_vol=sel["min_vol"], days=int(sel["days"]), exclude=sel["exclude"], log=log,
                         always=tuple(held),               # a held symbol is always measured, and never exempted from a gate by it
                         equity=eq, n_books=int(sel["n"]), edge=edge)
        except Exception as e: log(f"scan failed: {type(e).__name__}: {e}"); rows = None
        if rows:
            os.makedirs(LOGS, exist_ok=True)
            write_json(os.path.join(LOGS, "scan.json"), dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), days=sel["days"], edge=edge, rows=rows))
            now = time.time(); today = time.strftime("%Y%m%d", time.gmtime(now))
            st = read_json(os.path.join(LOGS, "select-state.json"), {})
            if st.get("day") != today: st["day"], st["opens"] = today, 0
            flats = flats_now()                              # after the scan: the verdict needs this minute's state files
            wind, adds, why = plan(rows, held, sel, st, today)
            ev("SELECT", n=int(sel["n"]), held=held, flat=flats, wind=[s for s, _ in wind], adds=adds, why=why, took_s=int(time.time() - t0), edge=edge,
               top=[[r["symbol"], round(r["edge"], 3), round(r["p_up"], 2), round(r["trials_h"], 2), round(r["impact"], 4), bool(r.get("entry")), r["side"]]
                    for r in rows[:6]])
            if not dry:
                before, rec0 = json.dumps(p, sort_keys=True), set(p.get("record") or {})
                acts = apply(p, rows, wind, adds, flats, sel, recent=recent_engines(load_states(), now))
                if json.dumps(p, sort_keys=True) != before: write_json(PARAMS, p, indent=2)
                for kind, sym, detail in acts:
                    if kind == "add": st["opens"] = st.get("opens", 0) + 1; st["streak"] = {}
                    ev({"add": "BOOK_ADD", "wind": "BOOK_WIND_DOWN", "drop": "BOOK_DROP", "resume": "BOOK_RESUME"}[kind], alert=True, symbol=sym, why=detail,
                       books=list(p.get("books") or {}))
                if not acts and set(p.get("record") or {}) != rec0: ev("RECORD_SET", symbols=list(p.get("record") or {}))
                write_json(os.path.join(LOGS, "select-state.json"), st)
        if once: break
        time.sleep(max(60, sel["every_h"] * 3600 - (time.time() - t0)))

if __name__ == "__main__":
    main()
