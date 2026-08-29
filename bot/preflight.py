"""Read-only pre-switch checks for going live.  python -m bot.preflight
Prints PASS / WARN / FAIL per item and exits 1 on any FAIL. Nothing is changed."""
import json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import from_env
from bot.signal import STRAT, SIG
from bot.ws import load_params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
out = []
def rep(level, msg): out.append((level, msg)); print(f"{level:<4} {msg}", flush=True)

def main():
    p = load_params() or {}; sp = {**STRAT, **p.get("strat", {})}; sides = list(sp.get("sides") or [sp["side"]]); sym = sp["symbol"]
    rep("INFO", f"params: {sym} sides={sides} mode={sp.get('mode')} adopt={sp.get('adopt')} unit_frac={sp.get('unit_frac')} cap_frac={sp.get('cap_frac')}")
    from bot.cycle import valid_params
    bad = valid_params(sp, p.get("sig") or {})
    rep("FAIL" if bad else "PASS", f"params validation: {bad or 'ok'}")
    b = from_env(); b.sync_time(); a = b.refresh_mode(sym)
    rep("PASS" if b.hedge else "FAIL", f"position mode: {a.get('posMode')}")
    rep("INFO", f"equity {float(a['accountEquity']):.2f} available {float(a['available']):.2f} lever long/short {a.get('isolatedLongLever')}/{a.get('isolatedShortLever')}")
    c = b.contract(sym); rep("PASS" if c.get("symbolStatus") == "normal" else "FAIL", f"contract status {c.get('symbolStatus')} tick={c['priceEndStep']}e-{c['pricePlace']} qstep=1e-{c['volumePlace']}")
    try:
        with open(os.path.join(LOGS, "state.json"), encoding="utf-8") as f: st = json.load(f)
    except Exception as e: st = None; rep("WARN", f"state.json unreadable: {e}")
    if st:
        age = time.time() - time.mktime(time.strptime(st["t"], "%Y-%m-%d %H:%M:%S"))
        rep("PASS" if age < 15 else "FAIL", f"engine state age {age:.0f}s, errors={st['errors']}, ws pub/prv={st['ws']['pub']}/{st['ws']['prv']}")
    pos = {q["holdSide"]: q for q in b.positions() if q["symbol"] == sym and float(q.get("total", 0)) > 0}
    for sd in sides:
        lots = ((st or {}).get("books") or {}).get(sd, {}).get("pos", {}).get("lots", []) if st else []
        ours = sum(l[0] for l in lots)
        ex = float(pos[sd]["total"]) if sd in pos else 0.0
        if ex and not ours: rep("WARN" if sp.get("adopt") else "FAIL", f"{sd}: exchange holds {ex} @ {pos[sd]['openPriceAvg']} and the book is empty -> needs strat.adopt=true (dry lots are not adopted; the live start takes the exchange position)")
        elif ex and ours and abs(ex - ours) > 1e-6: rep("FAIL", f"{sd}: exchange {ex} vs book {ours}: flatten or empty the book before switching")
        else: rep("PASS", f"{sd}: exchange {ex} book {ours}")
    for sd in set(pos) - set(sides): rep("WARN", f"{sd}: exchange holds {pos[sd]['total']} on a side the engine will not run (left alone)")
    pend = b.pending_orders(sym).get("entrustedList") or []
    foreign = [o for o in pend if not (o.get("clientOid") or "").startswith("cyc")]
    rep("FAIL" if foreign else "PASS", f"resting orders on {sym}: {len(pend)} total, {len(foreign)} not ours" + (f" -> cancel them first: {[(o.get('side'), o.get('tradeSide'), o.get('size'), o.get('price')) for o in foreign]}" if foreign else ""))
    plans = b.pending_plan_orders(sym).get("entrustedList") or []
    rep("INFO", f"plan orders on {sym}: {[(o.get('planType'), o.get('posSide'), o.get('triggerPrice')) for o in plans]} (psl of our side is adopted at live start)")
    for job in ("record", "cycle", "nightly", "sweep", "select"):
        try:
            pid = int(open(os.path.join(LOGS, f"{job}.pid")).read().strip())
            alive = str(pid) in subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
            rep("PASS" if alive else "FAIL", f"supervisor {job} pid {pid} {'alive' if alive else 'NOT running'}")
        except Exception as e: rep("FAIL", f"supervisor {job}: {e}")
    try:
        lines = open(os.path.join(LOGS, "record.log"), encoding="utf-8").read().splitlines()
        last = [l for l in lines if " REC " in l][-1]; rep("INFO", f"recorder last stats: {last[:120]}")
    except Exception: rep("WARN", "recorder has no REC line yet")
    r = subprocess.run([sys.executable, "-m", "unittest", "bot.test_signal", "bot.test_cycle", "bot.test_select", "bot.test_tools"], cwd=ROOT, capture_output=True, text=True)
    rep("PASS" if r.returncode == 0 else "FAIL", f"unit tests: {(r.stderr or r.stdout).strip().splitlines()[-1]}")
    fails = [m for lv, m in out if lv == "FAIL"]
    print("\nRESULT:", "FAIL" if fails else "PASS", f"({len(fails)} fail, {sum(1 for lv, _ in out if lv == 'WARN')} warn)")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
