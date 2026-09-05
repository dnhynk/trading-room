"""선정기 반사실 — logs/hunt-history.jsonl 의 모든 스캔에서 "그때 그 코인에 들어갔다면?"을 걸어가며 세는 읽기 전용 도구.
    python -m bot.campaigns [--stop live|N] [--hours 12] [--by off|ratio|run|atr|tw|age|spread|depth]

각 스캔의 각 행에 **현재 live 설정으로 진입 자격을 다시 판정**하고(`flags_of`), 통과한 자리마다 캠페인을 열어 스캔마다
`exit_flags` 를 건다. 진입가 대비 스탑 거리만큼 역행이 먼저면 stop, 퇴출 조건이 먼저면 exit. 스탑 거리는 기본이 **live 기하**다
(`stop_pct`: 유닛당 돈거리 cap_frac/unit_frac 과 cap_min_atr × ATR1m 중 큰 것 — 행마다 다르다); `--stop N` 은 고정 N%. p = stop / (stop + exit) 이고
이 p 는 **스캔 종가 경로의 장벽 도달 비율**이며 엔진 손절률·기하 성장률·켈리 계산에 쓰지 않는다(2026-09-05 정정).
감속 진입·메이커 체결·봉 사이 고저가·wind_down 보유·exit 지연을 재생하지 않는다. `fuel` = 보유 중 지나간 총 경로 % — 사이클이 태우는 재료라
캠페인의 이익 잠재력을 근사한다(퇴출이 너무 빠르면 여기가 먼저 줄어든다).

두 가지를 조심해서 읽어라 (2026-09-04 감사에서 둘 다 당했다, NEXT 18):
  1. `HUNT` 는 코드 기본값이고 live 는 `{**HUNT, **params["hunt"]}` 다 — 기본값으로 재면 다른 표가 나온다. 이 모듈은 live 를 쓴다.
  2. **인접 스캔은 같은 캠페인이다.** 한 에피소드가 수십 개의 진입 자리를 만들므로 n 은 독립 사건 수보다 훨씬 크다.
     `--by` 표의 칸마다 찍히는 `코인/에피소드` 수가 실제 표본이다 — 그게 한 자리면 그 칸은 잡음이다."""
import json, os, statistics, sys, datetime as dt
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.hunt import flags_of, exit_flags, HUNT
from bot.signal import STRAT
from bot.ws import load_params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEEP = lambda r: r.get("phase") not in (None, "shallow", "unread")
BUCKETS = {"off": [(0, 3, "0-3%"), (3, 6, "3-6%"), (6, 12, "6-12%"), (12, 25, "12-25%"), (25, 100, ">=25%")],
           "ratio": [(0, 4, "<4x"), (4, 10, "4-10x"), (10, 30, "10-30x"), (30, 98, ">=30x"), (98, 100, "new 99")],
           "run": [(0, 40, "<40%"), (40, 80, "40-80%"), (80, 150, "80-150%"), (150, 9e9, ">=150%")],
           "atr": [(0.3, 0.5, "0.30-0.50"), (0.5, 0.8, "0.50-0.80"), (0.8, 1.3, "0.80-1.30"), (1.3, 9, ">=1.30")],
           "tw": [(15, 30, "15-30"), (30, 50, "30-50"), (50, 70, "50-70"), (70, 999, ">=70")],
           "age": [(0, 24, "<1d"), (24, 72, "1-3d"), (72, 168, "3-7d"), (168, 9e9, ">=7d")],
           "spread": [(0, 5, "<5bp"), (5, 10, "5-10bp"), (10, 20, "10-20bp"), (20, 999, ">=20bp")],
           # depth = 진입가가 직전 6스캔(약 1시간) 고점 아래로 몇 % 인가. 사용자 가설(2026-09-04): "평평한 구간의 아주 작은 눌림에
           # 담아서 급락에 노출됐다" — 지금 표본으로는 순열 검정을 못 이겼다(NEXT 19). 에피소드가 쌓이면 이 열로 다시 묻는다.
           "depth": [(0, 1, "<1%"), (1, 2, "1-2%"), (2, 3.5, "2-3.5%"), (3.5, 6, "3.5-6%"), (6, 999, ">=6%")]}
KEY = {"off": "off", "ratio": "ratio", "run": "run", "atr": "atr_pct", "tw": "twoway24", "age": "age_h", "spread": "spread_bp",
       "depth": "_depth"}

def scans(path=None):
    out = []
    for line in open(path or os.path.join(ROOT, "logs", "hunt-history.jsonl"), encoding="utf-8"):
        d = json.loads(line)
        out.append((dt.datetime.strptime(d["t"], "%Y-%m-%d %H:%M:%S"), {r["symbol"]: r for r in d["rows"]}))
    out.sort(key=lambda x: x[0]); return out

def fuel(SC, sym, i, j):
    """보유 중 지나간 총 경로 %/캠페인 — 사이클의 재료"""
    px = [SC[k][1][sym]["px"] for k in range(i, j + 1) if sym in SC[k][1] and SC[k][1][sym].get("px")]
    return sum(abs(px[k + 1] / px[k] - 1) for k in range(len(px) - 1)) * 100 if len(px) > 1 else 0.0

def stop_pct(r, prof, fixed=None):
    """진입가 대비 스탑 거리(%). 기본은 **live 기하**(감사 2026-09-04): 유닛당 돈거리 `cap_frac/unit_frac` 과 ATR 바닥 `cap_min_atr × ATR1m`
    중 큰 것 — `cap_per_unit` 1 이면 사다리 깊이와 무관하고, 0 이어도 첫 유닛의 거리는 같다. `--stop N` 은 그대로 N. 고정 10% 로 재면
    ATR1m 1.3% 코인의 실제 스탑(30 ATR = 40%)을 4배 가깝게 두고 p 를 부풀린다 — `--by atr` 표의 ≥1.30 칸이 그렇게 21% 로 읽혔다."""
    if fixed is not None: return fixed
    money = prof["cap_frac"] / prof["unit_frac"] * 100 if prof.get("unit_frac") and prof.get("cap_frac") else 0.0
    floor = (prof.get("cap_min_atr") or 0.0) * (r.get("atr_pct") or 0.0)
    return max(money, floor) or 10.0

def campaign(SC, sym, i, side, cfg, stop, hours):
    """그 자리의 결말: (kind, 진입가 대비 %, 지속 h, fuel %, 끝낸 이유) 또는 None(지평 안에 안 끝남)"""
    r0 = SC[i][1][sym]; px0 = r0["px"]; s = 1 if side == "long" else -1
    held = dict(side=side, peak=r0.get("qv_shape") or r0["qv"], climax=r0.get("high48"), tw_peak=r0.get("twoway24") or 0.0)
    for j in range(i + 1, len(SC)):
        age = (SC[j][0] - SC[i][0]).total_seconds() / 3600
        if age > hours: return None
        r = SC[j][1].get(sym)
        if not r or not r.get("px"): continue
        mv = s * (r["px"] / px0 - 1) * 100
        if mv <= -stop: return ("stop", mv, age, fuel(SC, sym, i, j), "stop")
        if not DEEP(r): continue
        held["tw_peak"] = max(held["tw_peak"], r.get("twoway24") or 0.0)
        held["peak"] = max(held["peak"], r.get("qv_shape") or r["qv"])
        f = exit_flags(r, held, cfg)
        if f: return ("exit", mv, age, fuel(SC, sym, i, j), f[0])
    return None

def main():
    a = sys.argv[1:]
    get = lambda k, d: float(a[a.index(k) + 1]) if k in a else d
    fixed = None if "--stop" not in a or a[a.index("--stop") + 1] == "live" else float(a[a.index("--stop") + 1])
    hours = get("--hours", 12.0)
    by = a[a.index("--by") + 1] if "--by" in a else None
    cfg = {**HUNT, **((load_params() or {}).get("hunt") or {})}; prof = {**STRAT, **(cfg.get("strat") or {})}
    money = prof["cap_frac"] / prof["unit_frac"] * 100 if prof.get("unit_frac") and prof.get("cap_frac") else 0.0
    SC = scans()
    print(f"scans {len(SC)}  {SC[0][0]} .. {SC[-1][0]}  | live: min_vol {cfg['min_vol'] / 1e6:.0f}M  atr [{cfg['min_atr']}, {cfg['max_atr']}]  "
          f"exit_atr_min {cfg['exit_atr_min']}  exit_twoway {cfg['exit_twoway']}  min_twoway {cfg['min_twoway']}")
    k_atr = prof.get("cap_min_atr") or 0
    print(f"  stop: {f'-{fixed:g}% fixed' if fixed is not None else f'live geometry per entry row = max(cap_frac/unit_frac {money:.1f}%, cap_min_atr {k_atr:g} x ATR1m)'}")
    print("  RESEARCH ONLY: scan-close barriers, not engine campaigns or an executable return; unresolved horizons are censored.")
    rows = []; censored = 0
    for i in range(len(SC)):
        for sym, r in SC[i][1].items():
            if not DEEP(r) or not r.get("side") or flags_of(r, cfg): continue
            stop = stop_pct(r, prof, fixed)
            o = campaign(SC, sym, i, r["side"], cfg, stop, hours)
            if not o: censored += 1; continue
            prior = [SC[k][1][sym]["px"] for k in range(max(0, i - 6), i + 1) if sym in SC[k][1] and SC[k][1][sym].get("px")]
            d = (max(prior) / r["px"] - 1) * 100 if len(prior) > 2 else None       # 진입이 직전 ~1시간 고점 아래로 몇 % (long 기준)
            rows.append(dict(sym=sym, kind=o[0], mv=o[1], h=o[2], fuel=o[3], why=o[4], _depth=d, _stop=stop, **r))
    if not rows: print("no finished campaign in the horizon"); return
    st = sum(1 for r in rows if r["kind"] == "stop")
    print(f"  unresolved / censored entries: {censored}; the following ratio is conditional on an observed ending")
    print(f"\n진입 자리 중 결말이 관측된 것 {len(rows)} (stop 중앙 -{statistics.median(r['_stop'] for r in rows):.1f}%, 지평 {hours:.0f}h)  코인 {len(set(r['sym'] for r in rows))}종")
    print(f"  p = {st / len(rows) * 100:.1f}%  ({st} stop / {len(rows) - st} exit)   지속 중앙 {statistics.median(r['h'] for r in rows):.1f}h   "
          f"fuel 중앙 {statistics.median(r['fuel'] for r in rows):.1f}%   손익 중앙 {statistics.median(r['mv'] for r in rows):+.2f}%")
    print("  끝낸 이유:", dict(Counter("".join(c for c in (r["why"] or "") if not c.isdigit() and c not in ".-+/%") for r in rows).most_common()))
    if not by: return
    key = KEY.get(by)
    print(f"\n== {by} ==\n{'':>14}{'n':>5}{'코인':>5}{'p':>8}{'fuel':>8}{'손익':>9}")
    for lo, hi, nm in ([(x, y, z) for x, y, z in BUCKETS[by]] if key else []):
        sub = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
        if not sub: continue
        n_sym = len(set(r["sym"] for r in sub))          # 이게 진짜 표본이다 — 1이면 그 칸은 한 에피소드다
        print(f"{nm:>14}{len(sub):>5}{n_sym:>5}{sum(1 for r in sub if r['kind'] == 'stop') / len(sub) * 100:>7.0f}%"
              f"{statistics.median(r['fuel'] for r in sub):>7.1f}%{statistics.median(r['mv'] for r in sub):>+8.2f}%")

if __name__ == "__main__":
    main()
