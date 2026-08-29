"""Quiet watcher for the agent's Monitor: prints new logs/alerts.jsonl lines and FILL / RESUME / PARAMS / DAY_CLOSE / ADOPT / STOP_HIT
events as they happen, plus one summary line per side from logs/state.json every HB seconds. Nothing else."""
import json, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
HB = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
SHOW = {"FILL", "RESUME", "PARAMS", "DAY_CLOSE", "ADOPT", "STOP_HIT", "SIZING", "TAKER", "SWEEP", "DAILY_TREND"}

def tail(path, pos):
    try:
        with open(path, encoding="utf-8") as f:
            f.seek(pos); lines = f.readlines(); return lines, f.tell()
    except FileNotFoundError: return [], pos

def summary():
    try:
        with open(os.path.join(LOGS, "state.json"), encoding="utf-8") as f: s = json.load(f)
    except Exception as e: return f"STATE unreadable: {e}"
    f = s.get("f", {}); age = int(time.time() - time.mktime(time.strptime(s["t"], "%Y-%m-%d %H:%M:%S")))
    head = (f"HB {s['symbol']} {s['mode']} age={age}s up={s['up_s']}s ws={s['ws']['pub']}/{s['ws']['prv']} mid={f.get('mid')} "
            f"atr%={round(f['atr'] / f['mid'] * 100, 3) if f.get('atr') and f.get('mid') else None} hint={f.get('side_hint')} sigs={[x['sig'] for x in s.get('signals', [])]} errors={s['errors']}")
    books = s.get("books") or {s.get("side", "?"): s}
    for sd, b in books.items():
        p, w = b["pos"], b["working"]
        head += (f"\n  {sd}: qty={p['qty']} avg={p['avg']} upl={p['upl']} realized={b['realized']} halt={p['halt']} pause={p['pause']} regime={b.get('regime')} "
                 f"buy={w['buy'] and (w['buy']['px'], w['buy']['qty'])} trim={w['trim'] and (w['trim']['px'], w['trim']['qty'])} stop={b['stop'] and b['stop']['px']} "
                 f"unit={b.get('unit_qty')} cap={b.get('cap_usdt')} stops={b.get('stops_today')}")
    return head

pa = pe = ps = pn = 0
for name, var in (("alerts.jsonl", "pa"), ("events.jsonl", "pe"), ("cycle.log", "ps"), ("nightly.log", "pn")):
    try: globals()[var] = os.path.getsize(os.path.join(LOGS, name))
    except OSError: pass
print(summary(), flush=True); last_hb = time.time()
while True:
    lines, pa = tail(os.path.join(LOGS, "alerts.jsonl"), pa)
    for l in lines: print("ALERT " + l.strip()[:300], flush=True)
    lines, pe = tail(os.path.join(LOGS, "events.jsonl"), pe)
    for l in lines:
        try: j = json.loads(l)
        except ValueError: continue
        if j.get("ev") in SHOW: print(l.strip()[:300], flush=True)
    lines, ps = tail(os.path.join(LOGS, "cycle.log"), ps)
    for l in lines:
        if "SUPERVISOR" in l: print("SUP " + l.strip()[:200], flush=True)
    lines, pn = tail(os.path.join(LOGS, "nightly.log"), pn)
    for l in lines:
        if " REPORT " in l: print("NIGHTLY " + l.strip()[:200], flush=True)
    if time.time() - last_hb >= HB: print(summary(), flush=True); last_hb = time.time()
    time.sleep(5)
