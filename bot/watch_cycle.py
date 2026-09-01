"""Quiet watcher for the agent's Monitor: prints new logs/alerts.jsonl lines and FILL / RESUME / PARAMS / DAY_CLOSE / ADOPT / STOP_HIT
events as they happen, plus one summary line per side from every engine's logs/state-<SYMBOL>.json every HB seconds. Nothing else.
감시견 줄은 logs/cycle*.log 를 전부 따라간다 — 목록을 손으로 적어두면 cycle:SYMBOL 로 띄운 엔진의 재기동이 조용히 빠진다."""
import glob, json, os, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
HB = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
SHOW = {"FILL", "RESUME", "PARAMS", "DAY_CLOSE", "ADOPT", "STOP_HIT", "SIZING", "TAKER", "SWEEP", "DAILY_TREND", "SELECT", "RECORD_SET"}

def tail(path, pos):
    try:
        with open(path, encoding="utf-8") as f:
            f.seek(pos); lines = f.readlines(); return lines, f.tell()
    except FileNotFoundError: return [], pos

def summary():
    from bot.ws import load_states
    sts = load_states()                                  # 엔진마다 state-<SYMBOL>.json — 포트폴리오면 여럿이다
    if not sts: return "STATE unreadable: no state-*.json"
    return "\n".join(one(s) for s in sorted(sts.values(), key=lambda x: x.get("symbol", "")))

def one(s):
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

pa = pe = pn = 0
sup = {}                                                  # 엔진마다 로그가 따로다: logs/cycle.log, logs/cycle-SYMBOL.log
for name, var in (("alerts.jsonl", "pa"), ("events.jsonl", "pe"), ("nightly.log", "pn")):
    try: globals()[var] = os.path.getsize(os.path.join(LOGS, name))
    except OSError: pass
for f in glob.glob(os.path.join(LOGS, "cycle*.log")):
    try: sup[f] = os.path.getsize(f)
    except OSError: sup[f] = 0
print(summary(), flush=True); last_hb = time.time()
while True:
    lines, pa = tail(os.path.join(LOGS, "alerts.jsonl"), pa)
    for l in lines: print("ALERT " + l.strip()[:300], flush=True)
    lines, pe = tail(os.path.join(LOGS, "events.jsonl"), pe)
    for l in lines:
        try: j = json.loads(l)
        except ValueError: continue
        if j.get("ev") in SHOW: print(l.strip()[:300], flush=True)
    for f in glob.glob(os.path.join(LOGS, "cycle*.log")):   # 새 엔진이 뜨면 그 로그도 자동으로 따라붙는다
        lines, sup[f] = tail(f, sup.get(f, 0))
        for l in lines:
            if "SUPERVISOR" in l: print(f"SUP [{os.path.basename(f)[:-4]}] " + l.strip()[:200], flush=True)
    lines, pn = tail(os.path.join(LOGS, "nightly.log"), pn)
    for l in lines:
        if " REPORT " in l: print("NIGHTLY " + l.strip()[:200], flush=True)
    if time.time() - last_hb >= HB: print(summary(), flush=True); last_hb = time.time()
    time.sleep(5)
