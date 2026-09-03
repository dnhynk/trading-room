"""Read-only pre-switch checks for going live.  python -m bot.preflight
Prints PASS / WARN / FAIL per item and exits 1 on any FAIL. Nothing is changed.
계정 단위 항목은 한 번, 나머지는 params["books"] 의 심볼마다 반복한다(포트폴리오면 여럿). 감시견은 logs/*.pid 로 찾으므로
cycle:SYMBOL 로 띄운 엔진도 잡힌다 — 목록을 손으로 적어두면 새 엔진이 점검에서 조용히 빠진다."""
import glob, json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import from_env
from bot.signal import STRAT, SIG
from bot.ws import load_params, portfolio, strat_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
out = []
def rep(level, msg): out.append((level, msg)); print(f"{level:<4} {msg}", flush=True)

def main():
    p = load_params() or {}; syms = portfolio(p); all_pos = None
    from bot.cycle import valid_params
    from bot.ws import load_states
    all_st = load_states()
    b = from_env(); b.sync_time(); a = b.refresh_mode(syms[0])
    rep("PASS" if b.hedge else "FAIL", f"position mode: {a.get('posMode')}")
    rep("INFO", f"equity {float(a['accountEquity']):.2f} available {float(a['available']):.2f} lever long/short {a.get('isolatedLongLever')}/{a.get('isolatedShortLever')}")
    if len(syms) > 1:
        # select 은 wallet_frac 을 1/len(books) 가 아니라 1/select.n 으로 쓴다 — 슬롯이 비어 있는 동안 합이 1보다 작은 것은
        # 설계된 과도기다(자본을 덜 쓸 뿐 위험하지 않다). 위험한 것은 초과뿐이라 그것만 FAIL 이다.
        n = int((p.get("select") or {}).get("n") or len(syms))
        wf = {s: strat_for(p, s).get("wallet_frac", 1.0) for s in syms}
        tot = sum(wf.values())
        rep("FAIL" if tot > 1.0 + 1e-9 else "PASS", f"portfolio {len(syms)}/{n} symbols, wallet_frac {wf} sums to {tot:.3f}"
            + (" -> the wallet is OVER-committed" if tot > 1.0 + 1e-9 else
               f" (empty slots: {n - len(syms)}, so {1 - tot:.0%} of the wallet is idle by design)" if tot < 1.0 - 1e-9 else ""))
    all_pos = b.positions()
    for sym in syms:                                    # 심볼마다: 파라미터·계약·상태·거래소 대조·대기 주문
        sp = strat_for(p, sym); sides = list(sp.get("sides") or [sp["side"]])
        rep("INFO", f"[{sym}] params: sides={sides} mode={sp.get('mode')} adopt={sp.get('adopt')} unit_frac={sp.get('unit_frac')} cap_frac={sp.get('cap_frac')} wallet_frac={sp.get('wallet_frac')}")
        bad = valid_params(sp, p.get("sig") or {})
        rep("FAIL" if bad else "PASS", f"[{sym}] params validation: {bad or 'ok'}")
        c = b.contract(sym); rep("PASS" if c.get("symbolStatus") == "normal" else "FAIL", f"[{sym}] contract {c.get('symbolStatus')} tick={c['priceEndStep']}e-{c['pricePlace']} qstep=1e-{c['volumePlace']}")
        acc = b.account(sym); mm = acc.get("marginMode"); want_mm = sp.get("margin_mode"); want_lv = float(sp.get("lever") or 0)
        lv = float((acc.get("crossedMarginLeverage") if mm == "crossed" else acc.get("isolatedLongLever")) or 0)
        rep("FAIL" if want_mm and mm != want_mm else "PASS", f"[{sym}] margin mode {mm} (params {want_mm}); leverage {lv:g} (params {want_lv:g})"
            + (" -> ISOLATED: the liquidation guard, not the money cap, would be the stop (audit 7); the engine switches it when the book is flat" if want_mm and mm != want_mm else "")
            + (" -> leverage differs: the engine sets it when flat" if want_lv and abs(lv - want_lv) > 1e-9 else ""))
        st = all_st.get(sym)
        if st is None: rep("FAIL", f"[{sym}] no state (state-{sym}.json); engines seen: {sorted(all_st) or 'none'}")
        else:
            age = time.time() - time.mktime(time.strptime(st["t"], "%Y-%m-%d %H:%M:%S"))
            rep("PASS" if age < 15 else "FAIL", f"[{sym}] state age {age:.0f}s, errors={st['errors']}, ws pub/prv={st['ws']['pub']}/{st['ws']['prv']}")
        pos = {q["holdSide"]: q for q in all_pos if q["symbol"] == sym and float(q.get("total", 0)) > 0}
        for sd in sides:
            lots = ((st or {}).get("books") or {}).get(sd, {}).get("pos", {}).get("lots", []) if st else []
            ours = sum(l[0] for l in lots)
            ex = float(pos[sd]["total"]) if sd in pos else 0.0
            if ex and not ours: rep("WARN" if sp.get("adopt") else "FAIL", f"[{sym}] {sd}: exchange holds {ex} @ {pos[sd]['openPriceAvg']} and the book is empty -> needs strat.adopt=true (dry lots are not adopted; the live start takes the exchange position)")
            elif ex and ours and abs(ex - ours) > 1e-6: rep("FAIL", f"[{sym}] {sd}: exchange {ex} vs book {ours}: flatten or empty the book before switching")
            else: rep("PASS", f"[{sym}] {sd}: exchange {ex} book {ours}")
        for sd in set(pos) - set(sides): rep("WARN", f"[{sym}] {sd}: exchange holds {pos[sd]['total']} on a side the engine will not run (left alone)")
        pend = b.pending_orders(sym).get("entrustedList") or []
        foreign = [o for o in pend if not (o.get("clientOid") or "").startswith("cyc")]
        rep("FAIL" if foreign else "PASS", f"[{sym}] resting orders: {len(pend)} total, {len(foreign)} not ours" + (f" -> cancel them first: {[(o.get('side'), o.get('tradeSide'), o.get('size'), o.get('price')) for o in foreign]}" if foreign else ""))
        plans = b.pending_plan_orders(sym).get("entrustedList") or []
        rep("INFO", f"[{sym}] plan orders: {[(o.get('planType'), o.get('posSide'), o.get('triggerPrice')) for o in plans]} (psl of our side is adopted at live start)")
    for q in all_pos:                                   # 포트폴리오 밖 계약에 남은 물량 = 엔진의 OLD_POSITION halt 와 같은 조건
        if q["symbol"] not in syms and float(q.get("total", 0)) > 0:
            rep("WARN", f"[{q['symbol']}] {q['holdSide']}: {q['total']} on a contract no engine runs (the engine halts on this: OLD_POSITION)")
    hunt_on = bool((p.get("hunt") or {}).get("on"))       # hunt mode: bot.hunt owns books and bot.select must NOT run (two writers)
    jobs = ["record", "nightly", "sweep", "hunt" if hunt_on else "select"] + sorted(os.path.basename(f)[:-4] for f in glob.glob(os.path.join(LOGS, "cycle*.pid")))
    def alive_pid(job):
        pid = int(open(os.path.join(LOGS, f"{job}.pid")).read().strip())
        return pid, str(pid) in subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, errors="replace").stdout
    for job in jobs:
        try:
            pid, alive = alive_pid(job)
            rep("PASS" if alive else "FAIL", f"supervisor {job} pid {pid} {'alive' if alive else 'NOT running'}")
        except Exception as e: rep("FAIL", f"supervisor {job}: {e}")
    if hunt_on:
        try: pid, alive = alive_pid("select")
        except Exception: alive = False
        rep("FAIL" if alive else "PASS", f"hunt mode: bot.select {'is RUNNING — two writers of params.books' if alive else 'stopped'}")
    try:
        lines = open(os.path.join(LOGS, "record.log"), encoding="utf-8").read().splitlines()
        last = [l for l in lines if " REC " in l][-1]; rep("INFO", f"recorder last stats: {last[:120]}")
    except Exception: rep("WARN", "recorder has no REC line yet")
    # track B too (test_hunt / test_whale / test_phases): without them the selector and the evidence tables can break while preflight says PASS
    mods = ["bot.test_signal", "bot.test_cycle", "bot.test_select", "bot.test_tools", "bot.test_hunt", "bot.test_whale", "bot.test_phases"]
    r = subprocess.run([sys.executable, "-m", "unittest"] + mods, cwd=ROOT, capture_output=True, text=True)
    rep("PASS" if r.returncode == 0 else "FAIL", f"unit tests: {(r.stderr or r.stdout).strip().splitlines()[-1]}")
    fails = [m for lv, m in out if lv == "FAIL"]
    print("\nRESULT:", "FAIL" if fails else "PASS", f"({len(fails)} fail, {sum(1 for lv, _ in out if lv == 'WARN')} warn)")
    sys.exit(1 if fails else 0)

if __name__ == "__main__":
    main()
