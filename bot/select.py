"""Basket manager for the engine.  python -m bot.supervise select   (python -m bot.select [--once] [--dry])
Every select.every_h hours (and at start) `bot.scan.rank` scores the universe, logs/scan.json is rewritten (and every scan's rows
appended to logs/scan-history.jsonl), and `params.json["books"]` is brought toward the target basket. One engine per book
(bot/supervise.py `cycle`).

THE BASKET (2026-09-02, "집중된 바구니"): `select.n` MAIN books plus one PROBE book.
  - Main books share the main pool (1 - select.probe of the wallet), equally by default. When the live ledger says one book leads the
    runner-up by more than `select.sigma` standard errors — edge per hour = mean net %/cycle x cycles/h over the last
    `select.evidence_days`, both with >= `select.min_cycles` cycles — the leader takes `select.lead` of the pool and the rest split the
    remainder. That is the user's concentration instinct applied only where it is measurable: right, it earns most of what a single
    symbol would; wrong, it loses half of that.
  - The probe book runs the top candidate at `select.probe` of the wallet — the same engine, the same rules — so the NEXT symbol earns
    live evidence before it gets a main slot. Ranking symbols offline failed three times (RULES 도구 절): the only comparison the
    design allows is live against live from the same clock, and a single-symbol regime cannot produce it for the next symbol. After
    `select.min_cycles` cycles (or `select.probe_days` days with half of them) the probe is judged against the weakest measured main
    book: better by `select.sigma` SE -> that book winds down and the probe takes its slot once it is gone (BOOK_PROMOTE); not better
    -> the probe winds down (BOOK_PROBE_END), sits out `select.probe_cooldown_d` days, and the next candidate probes.
  - A main book LEAVES on a fact: a hard flag from the scan (volume, imp%, tick, spread, funding, pump shape, one-way now, too few
    pauses to be judged) or its OWN live ledger — >= min_cycles cycles with a mean net %/cycle below zero by more than sigma SE, on
    `select.confirm` consecutive scans (BOOK_EVICT). Never because something else scored higher today: the score orders the eligible
    set and fills empty slots, nothing more (2026-09-01: an incumbent exempted from the volume gate traded 12 more hours at
    -0.140%/cycle because the rank said so).
  - Books ENTER on the score: `select.confirm` consecutive scans in the top slots, `select.max_per_day` openings a day, never a
    `select.per_cluster`+1-th book of one driver cluster (the scan clusters symbols by the correlation of 15-min returns: four crypto
    books are one bet on the crash day), never a symbol still in its cooldown after an eviction or a finished probe.
  - `select.sigma_norm` = 1 scales the main shares by 1 / sigma_daily (the money cap then sits at the same distance in sigma on every
    book); off until the pair table (bot.pair) shows the gross loss per cycle scaling with sigma (NEXT 8).
  The wallet is never over-allocated: main shares sum to the pool over `select.n` slots (an empty slot leaves money idle), the probe
  carries its fraction, and a book winding down keeps its share until it is gone.
  Switching toward a single symbol is a decision, not a rule here: bot.pair marks the estimator's #1 against the basket average every
  day; when the #1 beats the average for weeks, `select.n` comes down (RULES).

WIND-DOWN. A leaving book is not closed at market; 순환매 sells into a stall (CONCEPT). `books[sym]["wind_down"] = 1` stops new entries
(cycle.py: a per-symbol PAUSE), trims and stops keep working, and the key is removed once that engine reports flat (read from the state
files AFTER the scan — a scan takes over a minute and the flat window is 60 s) — its engine then exits by itself (cycle.py: a pinned
symbol that left `books` is a contract change). A wound-down book whose flag is gone on a later scan adds again (BOOK_RESUME) — unless
it left on its own ledger or as a finished probe (those wait out the cooldown as candidates). `books` never becomes empty.

Also written by this job: `strat.symbol` (the highest-ranked held symbol — the default for bot.trade and for an unpinned engine),
`strat.side` (that symbol's 1H structure, a record; 쌍검 `sides` is preserved), and `record` = every book + engines seen within a day
(a book that just left keeps its tape) + the top `select.record_top` unflagged candidates + `select.record_extra` (a watchlist,
recorded whatever its flags say, never traded here) + BTCUSDT candles. Everything else in params.json belongs to the person.
The engine geometry the score charges (win / loss / fee per cycle) is re-measured from the live ledger at every scan (bot.cycles
.geometry, >= 200 cycles; else scan.EDGE; `select.edge` overrides both).
Events: SELECT (every scan's verdict, with the live evidence, the leader and the shares) in logs/events.jsonl; BOOK_ADD / BOOK_PROBE /
BOOK_PROMOTE / BOOK_MAIN / BOOK_EVICT / BOOK_PROBE_END / BOOK_WIND_DOWN / BOOK_RESUME / BOOK_DROP also in logs/alerts.jsonl.
State (add and probe streaks, evict streaks, openings today, probe start times, cooldowns) in logs/select-state.json. --once runs one
scan; --dry scans and decides, and writes only the report (logs/scan.json) — never params.json or the state."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.scan import rank, MIN_TRIALS_H
from bot.cycles import geometry, build
from bot.ws import load_params, PARAMS, load_states, portfolio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
SELECT = dict(every_h=4, n=4, confirm=2, max_per_day=2, record_top=5, min_vol=5e7, days=3, exclude=["BTCUSDT"], record_extra=[],
              probe=0.1,           # the probe book's share of the wallet (0 = no probe slot); main books share 1 - probe
              lead=0.5,            # the measured leader's share of the main pool (the rest split the remainder over n - 1 slots)
              min_cycles=60,       # live cycles a book needs before its ledger counts (leader, eviction, probe verdict)
              evidence_days=5,     # the trailing window of the live evidence (alpha rotates; TRUMP flipped sign within a day)
              sigma=2.0,           # standard errors a difference must exceed to act on it (leader tilt, promotion, eviction)
              probe_days=5,        # a probe is judged by then even with half the cycles, and ends if it cannot be judged
              probe_cooldown_d=7,  # days an evicted book or a finished probe waits before it is a candidate again
              per_cluster=2,       # main books per driver cluster (scan: 15-min return correlation >= corr_th)
              corr_th=0.5, min_trials_h=MIN_TRIALS_H,
              sigma_norm=0)        # 1: main shares x 1 / sigma_daily, renormalised — evidence first (NEXT 8)
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

def _secs(t): return time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S"))

def flat_of(states, now):
    """{symbol: True} for each engine that holds nothing, rests nothing and wrote its snapshot within 60 s. An engine that is down is
    not flat — its position is unobserved, and a book is only removed on an observation."""
    out = {}
    for sym, s in (states or {}).items():
        try:
            age = now - _secs(s["t"])
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
            if now - _secs(s["t"]) < hours * 3600: out.append(sym)
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

def evidence(now, days, done=None):
    """Live evidence per symbol from the completed lot cycles closed in the last `days` (bot.cycles): n, mean net %/cycle of the lot's
    notional and its standard error, cycles per engine-hour (hours = the span of that symbol's cycles in the window, at least one),
    edge per hour = mean x cycles/h and its SE — the score's own unit (% of one unit's notional per hour), so live books and the
    scan's candidates are read on one axis, and the comparison the design allows (live against live, same clock) has a number."""
    if done is None:
        try: done = build()[0]
        except FileNotFoundError: done = []
    since = now - days * 86400; per = {}
    for c in done:
        try: t0, t1 = _secs(c["t0"]), _secs(c["t1"])
        except Exception: continue
        if t1 < since or not c.get("qty") or not c.get("entry"): continue
        per.setdefault(c.get("symbol"), []).append((t0, t1, c["net"] / (c["qty"] * c["entry"]) * 100))
    out = {}
    for sym, xs in per.items():
        n = len(xs); m = sum(x[2] for x in xs) / n
        sd = (sum((x[2] - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5; se = sd / n ** 0.5
        hours = max((max(x[1] for x in xs) - min(x[0] for x in xs)) / 3600, 1.0); ch = n / hours
        out[sym] = dict(n=n, mean=round(m, 4), se=round(se, 4), hours=round(hours, 1), cyc_h=round(ch, 3), edge_h=round(m * ch, 4), se_h=round(se * ch, 4))
    return out

def leader_of(mains, ev, sel):
    """The main book whose live edge per hour leads the runner-up by more than `sigma` standard errors; None when fewer than two are
    measured or the gap is inside the noise (then the shares stay equal — the tilt is only ever as good as its evidence)."""
    m = sorted(((ev[s]["edge_h"], ev[s]["se_h"], s) for s in mains if s in ev and ev[s]["n"] >= int(sel["min_cycles"])), reverse=True)
    if len(m) < 2: return None
    (e1, s1, a), (e2, s2, _) = m[0], m[1]
    return a if e1 - e2 > float(sel["sigma"]) * (s1 ** 2 + s2 ** 2) ** 0.5 else None

def shares(books, sel, leader=None, sigma=None):
    """wallet_frac per book. Main books share the pool (1 - probe) over `n` SLOTS — not over the books present, so an empty slot leaves
    money idle and the sum never exceeds one wallet mid-change; the leader takes `lead` of the pool and the others split the rest over
    n - 1 slots; a probe book carries `probe`. sigma_norm: main shares x 1 / sigma_daily, renormalised to the same total."""
    n = max(int(sel["n"]), 1); probe = float(sel.get("probe") or 0.0); pool = 1.0 - probe
    out = {}
    for s, b in books.items():
        if b.get("probe"): out[s] = probe
        elif leader and n > 1: out[s] = pool * float(sel["lead"]) if s == leader else pool * (1 - float(sel["lead"])) / (n - 1)
        else: out[s] = pool / n
    if sel.get("sigma_norm") and sigma:
        w = {s: out[s] / sigma[s] for s, b in books.items() if not b.get("probe") and sigma.get(s)}
        if w:
            k = sum(out[s] for s in w) / sum(w.values())
            for s in w: out[s] = w[s] * k
    return {s: round(v, 6) for s, v in out.items()}

def verdict(rows, books, sel, st, today, ev=None, now=None):
    """Pure verdict on the basket. Mutates st (streaks). Returns dict(wind, evict, adds, probe, promote, probe_end, why):
    wind = [(symbol, why)] books flagged by the scan; evict = [(symbol, why)] main books negative on their own ledger for `confirm`
    scans; adds = main symbols to open now (in order, within the free slots and today's openings); probe = the symbol to open as the
    probe (or None); promote = (probe, weakest main, why) when the probe beat the weakest measured main; probe_end = (probe, why) when
    it did not (or could not be judged in probe_days).
    A holding missing from the scan is kept: absence is not evidence. A holding with a flag leaves whatever its rank — that asymmetry is
    the whole point (2026-09-01: the incumbent was exempted from the volume gate and traded 12 more hours at -0.140%/cycle)."""
    by = {r["symbol"]: r for r in rows}; ev = ev or {}; now = now or time.time()
    n = max(int(sel["n"]), 1); why = []; wind = []; evict = []; promote = None; probe_end = None
    mains = [s for s, b in books.items() if not b.get("probe")]; probes = [s for s, b in books.items() if b.get("probe")]
    active = [s for s in mains if not books[s].get("wind_down")]
    for s in books:                                                                   # 1. facts from the scan, any book
        r = by.get(s)
        if r is None: why.append(f"{s} not in the scan: kept")
        elif r["flags"]: wind.append((s, " ".join(r["flags"])))
    gone = {s for s, _ in wind}
    streak = st.setdefault("evict", {})                                               # 2. a main book's own ledger, confirmed over scans
    for s in active:
        e = ev.get(s)
        bad = bool(e and e["n"] >= int(sel["min_cycles"]) and e["mean"] < 0 and -e["mean"] > float(sel["sigma"]) * e["se"])
        streak[s] = streak.get(s, 0) + 1 if bad else 0
        if bad and streak[s] >= int(sel["confirm"]) and s not in gone: evict.append((s, f"live {e['mean']:+.3f}%/cycle over {e['n']} cycles (se {e['se']:.3f})"))
    for s in [s for s in streak if s not in active]: del streak[s]
    measured = sorted((ev[m]["edge_h"], ev[m]["se_h"], m) for m in active if m in ev and ev[m]["n"] >= int(sel["min_cycles"]) and m not in gone)
    for s in probes:                                                                  # 3. the probe's verdict
        if books[s].get("wind_down") or s in gone or books[s].get("promote"): continue
        e = ev.get(s); age_d = (now - st.get("probe_t", {}).get(s, now)) / 86400
        ready = bool(e and (e["n"] >= int(sel["min_cycles"]) or (age_d >= float(sel["probe_days"]) and e["n"] >= int(sel["min_cycles"]) / 2)))
        if ready and measured:
            e2, se2, weakest = measured[0]
            if e["edge_h"] - e2 > float(sel["sigma"]) * (e["se_h"] ** 2 + se2 ** 2) ** 0.5:
                promote = (s, weakest, f"probe {e['edge_h']:+.4f}%/h over {e['n']} cycles vs {weakest} {e2:+.4f}%/h"); continue
            probe_end = (s, f"not better than {weakest} ({e['edge_h']:+.4f} vs {e2:+.4f}%/h) after {e['n']} cycles")
        elif age_d >= float(sel["probe_days"]): probe_end = (s, f"unjudged after {age_d:.1f} d ({e['n'] if e else 0} cycles)")
    leaving = gone | {s for s, _ in evict} | ({promote[1]} if promote else set())
    cool = {s for s, until in st.get("cool", {}).items() if until > now}
    def room(cl): return cl is None or sum(1 for m in active if m not in leaving and (by.get(m) or {}).get("cluster") == cl) < int(sel["per_cluster"])
    cand = [r for r in rows if not r["flags"] and r.get("entry") and r["symbol"] not in books and r["symbol"] not in sel["exclude"]
            and r["symbol"] not in cool and room(r.get("cluster"))]
    free = n - len(mains)                                                             # a book winding down still holds its slot until it is gone
    top = [r["symbol"] for r in cand[:max(free, 0)]]
    st["streak"] = {s: st.get("streak", {}).get(s, 0) + 1 for s in top}               # a streak survives only while the symbol stays in the top slots
    left = int(sel["max_per_day"]) - (st.get("opens", 0) if st.get("day") == today else 0)
    adds = [s for s in top if st["streak"][s] >= int(sel["confirm"])][:max(left, 0)]
    probe = None                                                                      # 4. the probe slot: the best candidate after the main adds
    if float(sel.get("probe") or 0) > 0 and not [s for s in probes if not books[s].get("wind_down")] and not probe_end:
        nxt = [r["symbol"] for r in cand if r["symbol"] not in top][:1]
        st["pstreak"] = {s: st.get("pstreak", {}).get(s, 0) + 1 for s in nxt}
        if nxt and st["pstreak"][nxt[0]] >= int(sel["confirm"]) and left - len(adds) > 0: probe = nxt[0]
    else: st["pstreak"] = {}
    if free <= 0: why.append(f"basket full {len(mains)}/{n}")
    elif not cand: why.append(f"{free} free slot(s), no entry-eligible candidate")
    else: why.append(f"{free} free slot(s); " + ", ".join(f"{r['symbol']} {r['edge']:+.3f} ({st['streak'].get(r['symbol'], 0)}/{int(sel['confirm'])})" for r in cand[:4]))
    if (adds or probe) and left <= 0: why.append(f"{sel['max_per_day']} openings already today")
    if probes: why.append("probe " + ", ".join(f"{s}({ev[s]['n'] if s in ev else 0} cycles)" for s in probes))
    if evict: why.append("evict " + ", ".join(s for s, _ in evict))
    return dict(wind=wind, evict=evict, adds=adds, probe=probe, promote=promote, probe_end=probe_end, why="; ".join(why))

def plan(rows, held, sel, st, today):
    """Compatibility view of verdict() for a basket without a probe or live evidence: (wind, adds, why)."""
    v = verdict(rows, {s: {} for s in held}, sel, st, today)
    return v["wind"], v["adds"], v["why"]

def apply(p, rows, wind, adds, flats, sel, recent=(), evict=(), probe=None, promote=None, probe_end=None, ev=None, st=None, now=None):
    """Bring params.json to the planned basket. Returns [(action, symbol, detail)] — nothing is written by this function."""
    by = {r["symbol"]: r for r in rows}; n = max(int(sel["n"]), 1); acts = []; now = now or time.time()
    sp = p.setdefault("strat", {})
    books = p.get("books")
    if not books: books = {sp.get("symbol"): {}} if sp.get("symbol") else {}      # first run in basket mode: the running symbol becomes book one
    for s, w in wind:
        if not books.get(s, {}).get("wind_down"): books.setdefault(s, {})["wind_down"] = 1; acts.append(("wind", s, w))
    for s, w in evict:
        if s in books and not books[s].get("wind_down"): books[s].update(wind_down=1, evicted=1); acts.append(("evict", s, w))
    if promote:
        ps, loser, w = promote
        if loser in books and not books[loser].get("wind_down"): books[loser].update(wind_down=1, evicted=1)
        if ps in books: books[ps]["promote"] = 1; acts.append(("promote", ps, w))
    if probe_end:
        s, w = probe_end
        if s in books and not books[s].get("wind_down"): books[s].update(wind_down=1, ended=1); acts.append(("probe_end", s, w))
    for s in [s for s in books if books[s].get("wind_down") and s in by and not by[s]["flags"] and not books[s].get("evicted") and not books[s].get("ended")]:
        del books[s]["wind_down"]; acts.append(("resume", s, "flags cleared"))    # the fact that sent it out is gone (ER is re-judged every scan): it adds again
    for s in [s for s in books if books[s].get("wind_down") and flats.get(s)]:
        if len(books) > 1 or adds or probe:                                       # never ends empty: the last book stays, wound down, unless a replacement opens now
            if st is not None and (books[s].get("evicted") or books[s].get("ended")):
                st.setdefault("cool", {})[s] = now + float(sel["probe_cooldown_d"]) * 86400
            if st is not None: st.get("probe_t", {}).pop(s, None)
            del books[s]; acts.append(("drop", s, "flat"))
    mains = [s for s, b in books.items() if not b.get("probe")]
    for s in [s for s, b in books.items() if b.get("probe") and b.get("promote")]:
        if len(mains) < n: del books[s]["probe"]; del books[s]["promote"]; mains.append(s); acts.append(("main", s, "took the freed slot"))
    for s in adds:
        if len(mains) < n and s not in books:
            books[s] = {}; mains.append(s); acts.append(("add", s, f"edge {by[s]['edge']:+.3f} p_up {by[s]['p_up']:.2f} imp {by[s]['impact']:.4f} cluster {by[s].get('cluster')}"))
    if probe and probe not in books and not [b for b in books.values() if b.get("probe")]:
        books[probe] = {"probe": 1}; acts.append(("probe", probe, f"edge {by[probe]['edge']:+.3f} p_up {by[probe]['p_up']:.2f} cluster {by[probe].get('cluster')}"))
        if st is not None: st.setdefault("probe_t", {})[probe] = now
    leader = leader_of([s for s in mains if not books[s].get("wind_down")], ev or {}, sel)
    for s, v in shares(books, sel, leader, {r["symbol"]: r.get("sigma_d") for r in rows}).items(): books[s]["wallet_frac"] = v
    if books: p["books"] = books                                                  # an empty books would send ws.portfolio() back to whole-wallet strat.symbol
    held = list(books)
    if sp.get("symbol") not in held and held:                                     # the default symbol for bot.trade and for an unpinned engine
        first = next((r["symbol"] for r in rows if r["symbol"] in held), held[0])
        sp["symbol"] = first
        if (by.get(first) or {}).get("side") in ("long", "short"): sp["side"] = by[first]["side"]
    p["record"] = record_dict(held, rows, sel, recent)
    return acts

ALERT = {"add": "BOOK_ADD", "probe": "BOOK_PROBE", "promote": "BOOK_PROMOTE", "main": "BOOK_MAIN", "evict": "BOOK_EVICT", "probe_end": "BOOK_PROBE_END",
         "wind": "BOOK_WIND_DOWN", "drop": "BOOK_DROP", "resume": "BOOK_RESUME"}

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
                         equity=eq, n_books=int(sel["n"]), edge=edge, min_trials_h=float(sel["min_trials_h"]), corr_th=float(sel["corr_th"]))
        except Exception as e: log(f"scan failed: {type(e).__name__}: {e}"); rows = None
        if rows:
            os.makedirs(LOGS, exist_ok=True)
            rec = dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), days=sel["days"], edge=edge, rows=rows)
            write_json(os.path.join(LOGS, "scan.json"), rec)
            with open(os.path.join(LOGS, "scan-history.jsonl"), "a", encoding="utf-8") as f: f.write(json.dumps(rec) + "\n")   # every scan's estimator rows kept for the symbol x day pairing (bot.pair)
            now = time.time(); today = time.strftime("%Y%m%d", time.gmtime(now))
            st = read_json(os.path.join(LOGS, "select-state.json"), {})
            if st.get("day") != today: st["day"], st["opens"] = today, 0
            flats = flats_now()                              # after the scan: the verdict needs this minute's state files
            books = p.get("books") or {}
            try: evd = evidence(now, float(sel["evidence_days"]))
            except Exception as e: log(f"evidence failed: {type(e).__name__}: {e}"); evd = {}
            v = verdict(rows, books, sel, st, today, evd, now)
            leader = leader_of([s for s, b in books.items() if not b.get("probe") and not b.get("wind_down")], evd, sel)
            ev("SELECT", n=int(sel["n"]), held=held, flat=flats, wind=[s for s, _ in v["wind"]], evict=[s for s, _ in v["evict"]], adds=v["adds"],
               probe=v["probe"], promote=v["promote"][:2] if v["promote"] else None, probe_end=v["probe_end"][0] if v["probe_end"] else None,
               leader=leader, evidence={s: evd[s] for s in held if s in evd}, why=v["why"], took_s=int(time.time() - t0), edge=edge,
               top=[[r["symbol"], round(r["edge"], 3), round(r["p_up"], 2), round(r["trials_h"], 2), round(r["impact"], 4), bool(r.get("entry")), r["side"], r.get("cluster")]
                    for r in rows[:6]])
            if not dry:
                before, rec0 = json.dumps(p, sort_keys=True), set(p.get("record") or {})
                acts = apply(p, rows, v["wind"], v["adds"], flats, sel, recent=recent_engines(load_states(), now), evict=v["evict"], probe=v["probe"],
                             promote=v["promote"], probe_end=v["probe_end"], ev=evd, st=st, now=now)
                if json.dumps(p, sort_keys=True) != before: write_json(PARAMS, p, indent=2)
                for kind, sym, detail in acts:
                    if kind in ("add", "probe"): st["opens"] = st.get("opens", 0) + 1; st["streak"] = {}; st["pstreak"] = {}
                    ev(ALERT[kind], alert=True, symbol=sym, why=detail, books=list(p.get("books") or {}),
                       shares={s: b.get("wallet_frac") for s, b in (p.get("books") or {}).items()})
                if not acts and set(p.get("record") or {}) != rec0: ev("RECORD_SET", symbols=list(p.get("record") or {}))
                write_json(os.path.join(LOGS, "select-state.json"), st)
        if once: break
        time.sleep(max(60, sel["every_h"] * 3600 - (time.time() - t0)))

if __name__ == "__main__":
    main()
