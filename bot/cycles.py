"""사이클(로트 생애) 재구성 — `logs/events.jsonl`의 체결만 읽는다. 엔진·거래소에 접근하지 않는 읽기 전용 분석 도구다.

한 로트 = 담기 주문 하나(clientOid). 부분 체결은 같은 로트로 합치고, 덜기는 LIFO로 로트를 소진한다
(엔진의 `pos["lots"]`와 같은 규칙). 로트가 다 팔리면 한 사이클로 확정한다 — RULES의 "사이클 = 로트가 다 팔린 횟수".

    python -m bot.cycles [--from "2026-08-29 18:18"] [--side long|short] [--symbol SYM] [--top 20] [--csv PATH]

출력: 사이클별 (방향·진입시각·보유시간·수량·진입가·청산가·총손익·수수료·순손익·청산사유·진입 당시 로트 깊이)와
요약(개수·순손익 합·승률·중앙 보유시간·수수료 총액), 그리고 상위 N% 사이클이 순손익에서 차지하는 비중.
마지막 줄에 엔진이 기록한 누적 실현손익과의 대조(검산)를 찍는다."""
import json, os, sys, datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "events.jsonl")
LIVE = "2026-08-29 18:18"          # live 전환 시각: 그 전의 dry 장부는 STATE_DISCARDED로 버려졌다
EPS = 1e-9


def _secs(a, b):
    f = "%Y-%m-%d %H:%M:%S"
    return (dt.datetime.strptime(b, f) - dt.datetime.strptime(a, f)).total_seconds()


def _close(lots, done, side, s, qty, px, fee, t, why, from_core=False):
    """덜기/손절 체결을 LIFO로 로트에 배분한다. 다 팔린 로트는 사이클로 확정. from_core = 엔진이 코어 로트에 붙인 derisk 컷
    (FILL.lot == "core"): 첫 로트부터 차감하고 남으면 LIFO."""
    left, rate, orphan = qty, (fee / qty if qty else 0.0), 0.0
    while left > EPS:
        if not lots:
            orphan += left
            break
        lot = lots[0] if from_core else lots[-1]
        take = min(left, lot["qty"])
        lot["out_val"] += take * px
        lot["fee_out"] += rate * take
        lot["qty"] -= take
        lot["t1"], lot["why"] = t, why
        left -= take
        if lot["qty"] <= EPS:
            entry = lot["cost0"] / lot["qty0"]
            exitp = lot["out_val"] / lot["qty0"]
            gross = (exitp - entry) * lot["qty0"] * s
            fees = lot["fee_in"] + lot["fee_out"]
            done.append(dict(symbol=lot["sym"], side=side, t0=lot["t0"], t1=lot["t1"], hold=_secs(lot["t0"], lot["t1"]),
                             qty=lot["qty0"], entry=entry, exit=exitp, gross=gross, fee=fees,
                             net=gross - fees, why=lot["why"], depth=lot["depth"]))
            lots.remove(lot)
        from_core = False                      # 코어 로트가 다 팔리면 나머지는 LIFO
    return orphan


def build(since=LIVE, only=None, sym=None, until=None):
    books, done, orphan, eng, cur = {}, [], 0.0, {}, None
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"ev": "FILL"' not in line and '"ev": "STOP_HIT"' not in line and '"ev": "START"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ev, t = d.get("ev"), d.get("t", "")
            if ev == "START":                      # 체결 이벤트에 symbol 이 붙기 전(2026-09-01 17:30 이전) 장부의 귀속처.
                cur = d.get("symbol") or cur       # START 는 전 기간 symbol 을 담고, 그때는 엔진이 하나였다.
                continue
            if ev not in ("FILL", "STOP_HIT") or t < since or (until and t > until):   # until: a report for a past day must not read the cycles that came after it
                continue
            side = d.get("side") or "long"
            if only and side != only:
                continue
            esym = d.get("symbol") or cur          # 태그가 없으면 직전 START 의 심볼. 없는 것을 모든 심볼에 흘리면
            if sym and esym != sym:                # ZECUSDT 로 필터해도 TRUMPUSDT 장부가 통째로 딸려온다(2026-09-01)
                continue
            if "realized" in d:
                eng[f"{esym}/{side}"] = d["realized"]          # 엔진이 찍은 누적 실현손익(당일 기준) — 검산용
            lots = books.setdefault((esym, side), [])          # 로트 책은 (심볼, 방향)마다 — 안 가르면 한 심볼의 덜기가 다른 심볼의 로트를 LIFO 로 먹는다
            s = 1 if side == "long" else -1
            if ev == "FILL" and d.get("role") == "buy":
                oid = d.get("oid")
                lot = next((l for l in lots if l["oid"] == oid), None)
                if lot is None:
                    lot = dict(oid=oid, sym=esym, qty=0.0, qty0=0.0, cost0=0.0, fee_in=0.0,
                               out_val=0.0, fee_out=0.0, t0=t, t1=t, why="", depth=len(lots) + 1)
                    lots.append(lot)
                lot["qty"] += d["qty"]; lot["qty0"] += d["qty"]
                lot["cost0"] += d["qty"] * d["px"]; lot["fee_in"] += d.get("fee", 0.0)
            elif ev == "FILL" and d.get("role") == "trim":
                orphan += _close(lots, done, side, s, d["qty"], d["px"], d.get("fee", 0.0), t, "trim", from_core=d.get("lot") == "core")
            elif ev == "STOP_HIT":
                # STOP_HIT에는 fee 필드가 없다 — 엔진이 쓴 pnl은 수수료를 뺀 값이므로 (평단 기준 총손익 − pnl)로 역산한다
                fee = (d["px"] - d["avg"]) * d["qty"] * s - d["pnl"] if d.get("avg") else 0.0
                orphan += _close(lots, done, side, s, d["qty"], d["px"], max(fee, 0.0), t, "stop")
    return done, books, orphan, eng


def geometry_of(done):
    """엔진 기하: 완결 사이클의 평균 gross 이익%·손실%·왕복 수수료%(명목가 대비)와 승률. scan.EDGE 의 값이다. 사이클이 없으면 None."""
    g = [(c["gross"] / (c["qty"] * c["entry"]) * 100, c["fee"] / (c["qty"] * c["entry"]) * 100, c["net"]) for c in done if c["qty"] and c["entry"]]
    if not g: return None
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    return dict(n=len(g), win_pct=avg([a for a, _, n in g if n > 0]), loss_pct=abs(avg([a for a, _, n in g if n <= 0])),
                fee_pct=avg([b for _, b, _ in g]), p_win=sum(1 for _, _, n in g if n > 0) / len(g))


def geometry(since=LIVE, min_n=200):
    """실매매 장부에서 잰 현재 기하(win_pct/loss_pct/fee_pct) — scan 이 EDGE 대신 쓴다. 사이클이 min_n 미만이면 None(상수 유지)."""
    try: e = geometry_of(build(since)[0])
    except FileNotFoundError: return None
    return {k: e[k] for k in ("win_pct", "loss_pct", "fee_pct")} | dict(n=e["n"]) if e and e["n"] >= min_n else None


def report(done, books, orphan, eng, top=20, csv=None):
    if csv:
        with open(csv, "w", encoding="utf-8") as fh:
            fh.write("symbol,side,t0,t1,hold_s,qty,entry,exit,gross,fee,net,why,depth\n")
            for c in done:
                fh.write(f"{c.get('symbol')},{c['side']},{c['t0']},{c['t1']},{c['hold']:.0f},{c['qty']:.1f},"
                         f"{c['entry']:.4f},{c['exit']:.4f},{c['gross']:.4f},{c['fee']:.4f},"
                         f"{c['net']:.4f},{c['why']},{c['depth']}\n")
        print(f"csv -> {csv}")
    for c in done[-top:]:
        print(f"  {c['side']:<5} {c['t0'][5:]} +{c['hold']:>5.0f}s  q{c['qty']:>6.1f} "
              f"{c['entry']:.4f}->{c['exit']:.4f}  gross {c['gross']:+7.3f} fee {c['fee']:5.3f} "
              f"net {c['net']:+7.3f}  {c['why']:<4} d{c['depth']}")
    print()
    for side in sorted({c["side"] for c in done}) + ["ALL"]:
        cs = done if side == "ALL" else [c for c in done if c["side"] == side]
        if not cs:
            continue
        net = sorted(c["net"] for c in cs)
        tot, fee = sum(net), sum(c["fee"] for c in cs)
        wins = [n for n in net if n > 0]
        hold = sorted(c["hold"] for c in cs)
        print(f"{side:<5} cycles {len(cs):3d}  net {tot:+8.3f}  fees {fee:6.3f}  "
              f"win {len(wins) / len(cs) * 100:4.0f}%  median net {net[len(net) // 2]:+6.3f}  "
              f"median hold {hold[len(hold) // 2] / 60:5.1f}m  stops {sum(1 for c in cs if c['why'] == 'stop')}")
        if side == "ALL":
            desc = sorted((c["net"] for c in cs), reverse=True)
            pos, neg = sum(n for n in net if n > 0), sum(n for n in net if n < 0)
            for frac in (0.1, 0.25, 0.5):
                k = max(1, int(len(desc) * frac))
                print(f"        상위 {frac * 100:3.0f}% ({k:3d}개) 합 {sum(desc[:k]):+8.3f}"
                      f"  = 이익 합의 {sum(desc[:k]) / pos * 100 if pos else 0:5.1f}%")
            print(f"        이익 사이클 {sum(1 for n in net if n > 0):3d}개 {pos:+8.3f} / "
                  f"손실 사이클 {sum(1 for n in net if n < 0):3d}개 {neg:+8.3f}"
                  f"  (수수료 {fee:.3f} 포함)")
    e = geometry_of(done)
    if e:      # 엔진 기하 — bot/scan.py EDGE 의 출처(select 이 스캔 때마다 여기서 다시 잰다). 명목가 대비 %라 종목이 섞여도 더할 수 있다
        W, L, F, pw = e["win_pct"], e["loss_pct"], e["fee_pct"], e["p_win"]
        print(f"\n엔진 기하 (bot/scan.py EDGE, n={e['n']}): win_pct {W:.3f}  loss_pct {L:.3f}  fee_pct {F:.4f}"
              f"  | 손익분기 승률 {(L + F) / (W + L):.3f} vs 실측 {pw:.3f}  → 사이클당 {pw * W - (1 - pw) * L - F:+.4f}%")
    left = {f"{k[0]}/{k[1]}" if isinstance(k, tuple) else k: round(sum(l['qty'] for l in v), 1) for k, v in books.items() if v}
    print(f"\n미완결 로트 {left or '없음'} | 로트에 못 붙인 청산 수량 {orphan:.1f}")
    print(f"검산: 사이클 순손익 합 {sum(c['net'] for c in done):+.3f} "
          f"(엔진 누적 실현손익은 UTC 일자마다 0으로 리셋되므로 직접 비교 불가; "
          f"마지막 값 {eng})")


if __name__ == "__main__":
    args, since, only, top, csv, sym = sys.argv[1:], LIVE, None, 20, None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--from": since = args[i + 1]; i += 2
        elif a == "--side": only = args[i + 1]; i += 2
        elif a == "--symbol": sym = args[i + 1]; i += 2
        elif a == "--top": top = int(args[i + 1]); i += 2
        elif a == "--csv": csv = args[i + 1]; i += 2
        else: print(__doc__); sys.exit(0)
    done, books, orphan, eng = build(since, only, sym)
    report(done, books, orphan, eng, top, csv)
