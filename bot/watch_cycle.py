"""Quiet watcher for the agent's Monitor: prints new logs/alerts.jsonl lines and FILL / RESUME / PARAMS / DAY_CLOSE / ADOPT / STOP_HIT
events as they happen, plus one summary line per side from every engine's logs/state-<SYMBOL>.json every HB seconds. Nothing else.
감시견 줄은 logs/cycle*.log 를 전부 따라간다 — 목록을 손으로 적어두면 cycle:SYMBOL 로 띄운 엔진의 재기동이 조용히 빠진다."""
import glob, json, os, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 파이프로 나가면 stdout 이 콘솔 코드페이지(cp949)라 알림 한 줄의 '—' 하나로 Monitor 가 UnicodeEncodeError 로 죽고, 한글은 죽지 않아도 읽는 쪽에서 깨진다(2026-09-04 HUNT_BLOCKED 줄)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
HB = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
SHOW = {"FILL", "RESUME", "PARAMS", "DAY_CLOSE", "ADOPT", "STOP_HIT", "SIZING", "TAKER", "SWEEP", "DAILY_TREND", "SELECT", "RECORD_SET"}

def tail(path, pos):
    """errors="replace": 이 파일들을 우리만 쓰는 게 아니다 — logs/cycle*.log 는 감시견이 자식 stdout 을 파일에 그대로 붙인 것이라
    자식의 인코딩(깨끗한 셸에서 띄우면 cp949)으로 들어오고, 시작 위치는 바이트 크기라 멀티바이트 문자 중간에 떨어질 수 있다.
    둘 다 UnicodeDecodeError 로 Monitor 를 죽인다 — 한 줄이 깨져 보이는 것이 감시가 멈추는 것보다 낫다."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(pos); lines = f.readlines(); return lines, f.tell()
    except FileNotFoundError: return [], pos

def closed(sym, side=None, realized=None):
    """청산 직후 한 줄: 그 종목의 방향별 누적 실현손익(UTC 자정 리셋). 종목이 여럿이라 어느 책이 벌었는지 체결 줄만으론 안 보인다."""
    from bot.ws import load_states
    bs = ((load_states().get(sym) or {}).get("books") or {})
    r = {sd: (b.get("realized") or 0.0) for sd, b in bs.items()}
    if side and realized is not None: r[side] = realized     # 방금 온 체결이 state 저장(5초)보다 빠르다
    return f"{sym} today " + " / ".join(f"{sd} {v:+.2f}" for sd, v in sorted(r.items())) + f" = {sum(r.values()):+.2f}"

def selector_age():
    """선정기(hunt)가 마지막으로 스캔한 지 얼마나 됐나, 또는 None(hunt 모드가 아님). 크래시는 감시견이 5초 뒤 되살리지만
    **행(hang)은 아무 알림도 내지 않는다** — 알림이 안 오는 것은 알림이 아니다. 그 사이 국면 퇴출을 쓸 주체가 없고
    남는 보호는 거래소 돈 한도뿐이다(감사 NEXT 17c)."""
    from bot.ws import load_params
    p = load_params() or {}
    if not (p.get("hunt") or {}).get("on"): return None
    every = float((p.get("hunt") or {}).get("every_min") or 10)
    try: age = time.time() - os.path.getmtime(os.path.join(LOGS, "hunt.json"))
    except OSError: return (None, every)
    return (age, every)

def summary():
    from bot.ws import load_states
    sts = load_states()                                  # 엔진마다 state-<SYMBOL>.json — 포트폴리오면 여럿이다
    if not sts: return "STATE unreadable: no state-*.json"
    out = "\n".join(one(s) for s in sorted(sts.values(), key=lambda x: x.get("symbol", "")))
    sel = selector_age()
    if sel: out += f"\nHB hunt last scan {'never' if sel[0] is None else f'{int(sel[0])}s'} ago (every {sel[1]:.0f}m)"
    return out

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
print(summary(), flush=True); last_hb = time.time(); stale_alerted = False
while True:
    lines, pa = tail(os.path.join(LOGS, "alerts.jsonl"), pa)
    for l in lines: print("ALERT " + l.strip()[:300], flush=True)
    lines, pe = tail(os.path.join(LOGS, "events.jsonl"), pe)
    for l in lines:
        try: j = json.loads(l)
        except ValueError: continue
        if j.get("ev") in SHOW: print(l.strip()[:300], flush=True)
        if j.get("symbol") and (j.get("pos_qty") == 0 or (j.get("ev") == "STOP_HIT" and not j.get("partial"))):
            print("CLOSE " + closed(j["symbol"], j.get("side"), j.get("realized")), flush=True)
    for f in glob.glob(os.path.join(LOGS, "cycle*.log")):   # 새 엔진이 뜨면 그 로그도 자동으로 따라붙는다
        lines, sup[f] = tail(f, sup.get(f, 0))
        for l in lines:
            if "SUPERVISOR" in l: print(f"SUP [{os.path.basename(f)[:-4]}] " + l.strip()[:200], flush=True)
    lines, pn = tail(os.path.join(LOGS, "nightly.log"), pn)
    for l in lines:
        if " REPORT " in l: print("NIGHTLY " + l.strip()[:200], flush=True)
    sel = selector_age()                                    # 선정기가 멈추면 아무 이벤트도 안 나므로 침묵으로만 드러난다 — 침묵을 알림으로 바꾼다
    if sel and (sel[0] is None or sel[0] > sel[1] * 60 * 3):
        if not stale_alerted:
            print(f"HUNT_STALE hunt has not scanned for {'ever' if sel[0] is None else f'{int(sel[0] / 60)}m'} "
                  f"(every {sel[1]:.0f}m): no phase exit can be written, the exchange stop is the only protection left", flush=True)
            stale_alerted = True
    elif stale_alerted:
        print(f"HUNT_STALE cleared: last scan {int(sel[0])}s ago", flush=True); stale_alerted = False
    if time.time() - last_hb >= HB: print(summary(), flush=True); last_hb = time.time()
    time.sleep(5)
