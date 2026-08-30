"""Automatic symbol selection for the engine.  python -m bot.supervise select   (python -m bot.select [--once] [--dry])
Every select.every_h hours (and at start) the universe is scanned (bot.scan.rank: the concept metrics and the candle-level engine proxy
over select.days windows) and logs/scan.json is rewritten. A switch happens only when ALL of these hold:
  - the engine is flat: no lots, no working order, no pull, state.json younger than 60 s;
  - the best unflagged candidate's proxy >= select.ratio x the incumbent's proxy AND its concept >= the incumbent's (a less two-way
    symbol never wins on the proxy alone); when the incumbent is flagged (one-way now, pump shape, funding, tick) the ratio is 1;
  - the same candidate has qualified in select.confirm consecutive scans (hysteresis: yesterday's chop does not win a switch);
  - the incumbent has been held >= select.dwell_h hours (waived when it is flagged) and fewer than select.max_per_day switches today;
  - the candidate is not in select.exclude (BTC at low capital, CONCEPT).
The switch rewrites params.json atomically: strat.symbol, strat.side = the candidate's 1H structure side (long when unclear) and
strat.sides = both sides when the incumbent runs 쌍검 (else [side]),
record = the new symbol + the top select.record_top unflagged candidates with full channels (so the real tick backtest can later
validate the proxy) + BTCUSDT candles. The recorder re-subscribes on the file change; the engine restarts on the symbol change (it is
flat; its ledger for the new symbol starts fresh). Every scan also refreshes the recorded candidate set (a record-only rewrite: the
engine reloads without restarting). Events: SELECT (every scan's verdict) in logs/events.jsonl, SYMBOL_SWITCH also in logs/alerts.jsonl.
State (dwell start, streak, switches today) in logs/select-state.json. --once runs one scan; --dry never writes params or state."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.scan import rank
from bot.ws import load_params, PARAMS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
SELECT = dict(every_h=4, ratio=1.5, confirm=2, dwell_h=24, max_per_day=1, record_top=5, min_vol=5e7, days=3, exclude=["BTCUSDT"], record_extra=[])
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

def engine_flat(state, now):
    """The engine holds nothing and rests nothing, and the snapshot is fresh (an engine that is down is not 'flat')."""
    try:
        age = now - time.mktime(time.strptime(state["t"], "%Y-%m-%d %H:%M:%S"))
        books = state.get("books") or {state.get("side", "?"): state}
        return age < 60 and all(not b["pos"]["lots"] and not b["working"]["buy"] and not b["working"]["trim"] and not b.get("pull") for b in books.values())
    except Exception: return False

def record_dict(symbol, rows, sel):
    rec = {symbol: CHANNELS}
    for r in [r for r in rows if not r["flags"] and r["symbol"] != symbol and r["symbol"] not in sel["exclude"]][:int(sel["record_top"])]:
        rec[r["symbol"]] = CHANNELS
    for x in sel.get("record_extra") or []: rec.setdefault(x, CHANNELS)      # watchlist: recorded regardless of flags (evidence, never traded by select)
    rec["BTCUSDT"] = ["candle1m"]
    return rec

def decide(rows, incumbent, sel, st, flat, today, now):
    """Pure verdict: ('keep'|'wait'|'switch', why, candidate row or None). Mutates st['streak']."""
    by = {r["symbol"]: r for r in rows}; inc = by.get(incumbent)
    ok = [r for r in rows if not r["flags"] and r["symbol"] != incumbent and r["symbol"] not in sel["exclude"]]
    if not ok: st["streak"] = {}; return "keep", "no unflagged candidate", None
    best = ok[0]; inc_flagged = inc is None or bool(inc["flags"])
    ratio = 1.0 if inc_flagged else sel["ratio"]; inc_proxy = inc["proxy"] if inc else 0.0; inc_concept = inc["concept"] if inc else 0.0
    qualifies = best["proxy"] > 0 and best["proxy"] >= ratio * max(inc_proxy, 0.0) and best["concept"] >= inc_concept
    n = st.get("streak", {}).get(best["symbol"], 0) + 1 if qualifies else 0
    st["streak"] = {best["symbol"]: n} if qualifies else {}
    head = f"{best['symbol']} proxy {best['proxy']:.2f}/{best['concept']:.2f} vs {incumbent} {inc_proxy:.2f}/{inc_concept:.2f}"
    if not qualifies: return "keep", head + f" (needs x{ratio:.1f} and concept >=)", best
    if n < sel["confirm"]: return "keep", head + f" qualifies {n}/{int(sel['confirm'])}", best
    if not inc_flagged and now - st.get("since", 0) < sel["dwell_h"] * 3600: return "keep", head + f" confirmed; dwell {(now - st.get('since', 0)) / 3600:.1f}h < {sel['dwell_h']}h", best
    if st.get("switch_day") == today and st.get("switches", 0) >= sel["max_per_day"]: return "keep", head + " confirmed; already switched today", best
    if not flat: return "wait", head + " confirmed; engine not flat", best
    return "switch", head + (" (incumbent flagged)" if inc_flagged else ""), best

def switch(p, best, rows, sel):
    sp = p["strat"]; side = best.get("side") if best.get("side") in ("long", "short") else "long"
    dual = len(sp.get("sides") or []) > 1                                    # 쌍검 stays 쌍검 across a switch (user decision 2026-08-30); the structure side is the record only
    sp["symbol"], sp["side"], sp["sides"] = best["symbol"], side, (["long", "short"] if dual else [side])
    p["record"] = record_dict(best["symbol"], rows, sel)
    write_json(PARAMS, p, indent=2)
    return side

def main():
    once, dry = "--once" in sys.argv, "--dry" in sys.argv
    while True:
        p = load_params() or {}; sel = {**SELECT, **(p.get("select") or {})}
        incumbent = (p.get("strat") or {}).get("symbol")
        t0 = time.time()
        try: rows = rank(min_vol=sel["min_vol"], days=int(sel["days"]), exclude=sel["exclude"], log=log)
        except Exception as e: log(f"scan failed: {type(e).__name__}: {e}"); rows = None
        if rows and incumbent:
            os.makedirs(LOGS, exist_ok=True)
            write_json(os.path.join(LOGS, "scan.json"), dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), days=sel["days"], rows=rows))
            now = time.time(); today = time.strftime("%Y%m%d", time.gmtime(now))
            st = read_json(os.path.join(LOGS, "select-state.json"), {})
            if st.get("symbol") != incumbent: st.update(symbol=incumbent, since=now, streak={})   # the dwell clock starts when a symbol is first seen
            flat = engine_flat(read_json(os.path.join(LOGS, "state.json"), {}), now)
            action, why, best = decide(rows, incumbent, sel, st, flat, today, now)
            ev("SELECT", action=action, why=why, incumbent=incumbent, flat=flat, took_s=int(time.time() - t0),
               top=[[r["symbol"], round(r["proxy"], 2), round(r["concept"], 2), r["side"]] for r in rows[:5]])
            if action == "switch" and not dry:
                side = switch(p, best, rows, sel)
                st.update(symbol=best["symbol"], since=now, switch_day=today, switches=(st.get("switches", 0) + 1) if st.get("switch_day") == today else 1, streak={})
                ev("SYMBOL_SWITCH", alert=True, frm=incumbent, to=best["symbol"], side=side, proxy=round(best["proxy"], 2), concept=round(best["concept"], 2), why=why)
            elif not dry:
                rec = record_dict(incumbent, rows, sel)
                if rec != p.get("record"): p["record"] = rec; write_json(PARAMS, p, indent=2); ev("RECORD_SET", symbols=list(rec))
            if not dry: write_json(os.path.join(LOGS, "select-state.json"), st)
        if once: break
        time.sleep(max(60, sel["every_h"] * 3600 - (time.time() - t0)))

if __name__ == "__main__":
    main()
