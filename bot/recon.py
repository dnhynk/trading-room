"""백테스트 캘리브레이션 — 같은 시간·같은 사이즈로 돌린 백테스트와 실매매 장부를 대조한다.

    python -m bot.recon [--day YYYYMMDD] [--min-hours 2] [--quiet]

백테스트는 이 레포의 거의 모든 판정(사다리 반사실, 종목 비교, 파라미터 실험)이 기대는 도구인데 그 도구가 실매매를
얼마나 재현하는지는 별개의 사실이다. 하루를 통째로 비교하면 안 된다 — 재기동·HALT·파라미터 변경·종목 전환이 낀
구간은 백테스트가 재현할 수 없으므로, 그런 경계 이벤트가 하나도 없는 **연속 시간대**만 골라 구간마다 대조한다.
사이즈는 그 구간 직전의 `SIZING`(자본 비례로 정해진 실제 값)을 방향 수만큼 되돌려 파일 단위로 넘긴다.

읽기 전용: `logs/events.jsonl`과 `data/ws/`만 읽는다. 출력은 구간별 (실현손익 live/bt/차, 담기·덜기 주문 수)와 합계.
누적된 차이가 계통적(양쪽 비교에서 상쇄됨)인지 무작위인지는 구간이 여러 날 쌓여야 갈린다 — 그래서 야간 리포트 항목이다."""
import calendar, glob, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from bot.backtest import run_files
from bot.ws import load_params

LOG = os.path.join(ROOT, "logs", "events.jsonl")
# 백테스트가 재현할 수 없는 것들: 프로세스 경계, 사람/사고에 의한 개입, 규칙 자체의 변경
BREAKS = {"START", "EXIT", "HALT", "RESUME", "EMERGENCY_CLOSE", "EMERGENCY_CANCEL_UNCONFIRMED", "SYMBOL_SWITCH",
          "STATE_DISCARDED", "PARAMS_INVALID", "PARAMS_DEFERRED", "ADOPT_STOP", "ADOPT_ORDER", "EXTERNAL_FILL"}


def _epoch(t):
    try: return time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S"))      # events 의 t 는 로컬 시각
    except Exception: return None


def load_events():
    out = []
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"ev"' not in line: continue
            try: d = json.loads(line)
            except Exception: continue
            d["_e"] = d.get("sec") or _epoch(d.get("t", ""))
            if d["_e"]: out.append(d)
    return out


def tape_hours(day):
    """그 UTC 날짜에 존재하는 테이프 시각 -> {시각: 파일}. 파일 하나가 UTC 한 시간을 담는다."""
    out = {}
    for f in glob.glob(os.path.join(ROOT, "data", "ws", f"pub-{day}-*.jsonl*")):
        b = os.path.basename(f)
        if b.endswith(".jsonl") and os.path.exists(f + ".gz"): continue   # gzip 중인 원본
        try: out[int(b[13:15])] = f
        except ValueError: pass
    return out


def segments(day, evs, min_hours):
    """경계 이벤트가 하나도 없는 연속 테이프 시간대. (시작epoch, 끝epoch, [파일...])"""
    hrs = tape_hours(day)
    if not hrs: return []
    y, mo, d = int(day[:4]), int(day[4:6]), int(day[6:8])
    brk = sorted(e["_e"] for e in evs if e.get("ev") in BREAKS)
    runs, cur = [], []
    for h in sorted(hrs):
        a = calendar.timegm((y, mo, d, h, 0, 0, 0, 0, 0)); z = a + 3600
        clean = not any(a <= b < z for b in brk)
        if clean and (not cur or h == cur[-1] + 1): cur.append(h)
        else:
            if cur: runs.append(cur)
            cur = [h] if clean else []
    if cur: runs.append(cur)
    out = []
    for r in runs:
        if len(r) < min_hours: continue
        a = calendar.timegm((y, mo, d, r[0], 0, 0, 0, 0, 0))
        out.append((a, a + 3600 * len(r), [hrs[h] for h in r], r[0], r[-1]))
    return out


def live(evs, a, z, sym):
    """구간의 실매매 장부. 주문 수는 고유 clientOid — 부분 체결이 한 주문을 여러 FILL 로 쪼개기 때문."""
    pnl = fee = 0.0; oid = {"buy": set(), "trim": set()}; stops = 0; sides = set()
    for e in evs:
        if not (a <= e["_e"] < z): continue
        if e.get("symbol") not in (None, sym): continue      # 엔진이 여럿이면 심볼로 가른다(옛 이벤트엔 symbol 이 없다)
        if e.get("ev") == "FILL":
            pnl += e.get("pnl", 0.0); fee += e.get("fee", 0.0)
            if e.get("role") in oid: oid[e["role"]].add(e.get("oid"))
            if e.get("side"): sides.add(e["side"])
        elif e.get("ev") == "STOP_HIT":
            pnl += e.get("pnl", 0.0); stops += 1
    return pnl, fee, len(oid["buy"]), len(oid["trim"]), stops, sides


def sizes(evs, z, sides):
    """구간 끝 이전 마지막 SIZING 을 방향별로 모아 파일 단위(방향 수를 곱한 값)로 되돌린다.
    구간이 실제로 거래한 방향만 쓴다 — 단일책 시절의 side 없는 SIZING 이 섞이면 유닛이 부풀려진다."""
    n_sides = max(len(sides), 1)
    last = {}
    for e in evs:
        if e.get("ev") == "SIZING" and e["_e"] < z and e.get("side") in sides: last[e["side"]] = e
    if not last: return {}
    def avg(k):
        v = [e[k] for e in last.values() if e.get(k) is not None]
        return sum(v) / len(v) * n_sides if v else None
    return {k: v for k, v in (("unit_qty", avg("unit_qty")), ("cap_usdt", avg("cap_usdt")),
                              ("daily_loss_limit", avg("daily_loss_limit")), ("max_notional", avg("max_notional"))) if v}


def main(day, min_hours=2, quiet=False):
    evs = load_events()
    p = load_params() or {}; sp = p.get("strat") or {}
    sym = sp.get("symbol", "TRUMPUSDT")
    segs = segments(day, evs, min_hours)
    print(f"# recon {day}  깨끗한 구간 {len(segs)}개 (경계 이벤트 없음: {', '.join(sorted(BREAKS))[:80]}...)")
    if not segs:
        print("  대조할 구간이 없다 — 재기동·HALT·파라미터 변경이 하루를 다 덮었거나 테이프가 없다."); return
    tot_l = tot_b = 0.0; hrs = 0
    for a, z, files, h0, h1 in segs:
        pnl, fee, nbuy, ntrim, stops, sides = live(evs, a, z, sym)
        sides = sides or set(sp.get("sides") or ["long"])
        ov = dict(unit_frac=0.0, cap_frac=0.0, daily_loss_frac=0.0, notional_frac=0.0, **sizes(evs, z, sides))
        try:
            m = run_files(files, sym, {}, ov, events=False, qstep=sp.get("qstep", 0.1) or 0.1, sides=sorted(sides) or None)
        except Exception as e:
            print(f"  UTC {h0:02d}-{h1:02d}  백테스트 실패 {type(e).__name__}: {e}"); continue
        tot_l += pnl; tot_b += m["pnl"]; hrs += len(files)
        print(f"  UTC {h0:02d}-{h1:02d} ({len(files)}h, {'/'.join(sorted(sides)) or '?'}, 유닛 {ov.get('unit_qty', 0):.1f})"
              f"  live {pnl:+7.3f}  bt {m['pnl']:+7.3f}  차 {m['pnl'] - pnl:+7.3f}"
              f"   담기 {nbuy}/{m['adds']}  덜기 {ntrim}/{m['cycles']}  스탑 {stops}/{m['stops']}")
    if hrs:
        print(f"  합계 {hrs}시간  live {tot_l:+.3f}  bt {tot_b:+.3f}  차 {tot_b - tot_l:+.3f} ({(tot_b - tot_l) / hrs:+.4f}/시간)")
        print("  읽기: 차이의 부호가 날마다 뒤집히면 무작위(종목 비교에서 상쇄되지 않는다), 한쪽으로 쌓이면 계통 편향이다.")


if __name__ == "__main__":
    a = sys.argv[1:]
    day = a[a.index("--day") + 1] if "--day" in a else time.strftime("%Y%m%d", time.gmtime(time.time() - 86400))
    mh = int(a[a.index("--min-hours") + 1]) if "--min-hours" in a else 2
    main(day, mh, "--quiet" in a)
