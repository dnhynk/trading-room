"""Direction capture of the live book: of the day's price movement, how much happened while the engine held a position?
  python -m track_a.capture [--day YYYYMMDD] [--sym TRUMPUSDT]
Position timeline from logs/events.jsonl (FILL pos_qty, STOP_HIT, START snapshots, ADOPT); prices from the day's recordings
(candle1m closes, one per minute). Per book side:
  up/dn held   fraction of the day's minute-by-minute up-moves (down-moves) that happened while a position was held
  legs up/dn   the same at the leg scale: legs >= sig.dip_min_atr x ATR on the closes, each weighted by its size, counting the
               minutes the book was in a position
  in_mkt       minutes held / minutes
A long book that captures up-legs more than down-legs is on the right side of the drift; one that holds through the down-legs
and sits out the up-legs (adds at decelerations, cut on the way up) is paying for its cycles with the direction's money.
A short book reads inverted (down held is its favourable side). 쌍검이면 책마다 한 줄이다 — 한 방향만 재면 절반만 보인다.
This is the measurement behind the "cycle = average adjustment, direction = the money" concept (2026-08-30) — evidence for a
cycle-scale FAVOR rule (core held with a trailing stop), never a rule by itself."""
from common.paths import runtime_root
import glob, gzip, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.signal import zigzag_pivots, wilder_atr
from common.ws import load_params, portfolio, strat_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(runtime_root(ROOT), "logs")

def candles_of(day, sym):
    """Closed 1m candles of the UTC day from the recordings (dict ts -> candle), oldest first."""
    c = {}
    for p in sorted(glob.glob(os.path.join(runtime_root(ROOT), "data", "ws", f"pub-{day}-*.jsonl*"))):
        if p.endswith(".jsonl") and os.path.exists(p + ".gz"): continue
        op = gzip.open if p.endswith(".gz") else open
        with op(p, "rt", encoding="utf-8") as f:
            for line in f:
                raw = line[line.find("\t") + 1:]
                if '"candle1m"' not in raw or f'"instId":"{sym}"' not in raw: continue
                try: j = json.loads(raw)
                except ValueError: continue
                for r in j.get("data") or []: c[int(r[0])] = dict(ts=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), v=float(r[5]))
    d0 = int(time.mktime(time.strptime(day, "%Y%m%d")) - time.timezone) // 86400          # the UTC day only: the first file's snapshot reaches back ~8h
    rows = [c[k] for k in sorted(c) if k // 86_400_000 == d0]
    return rows[:-1]     # the last row is the candle still open when the recording ended

def timeline(day, sym, side="long"):
    """[(epoch_s, qty)] of one (symbol, side) book from events.jsonl, in time order (the first entry is the qty at the start of
    the day). 엔진이 여럿이면 한 파일에 심볼이 섞이므로 심볼과 방향으로 가른다 — 안 가르면 다른 책의 체결이 이 책의 보유량이 된다."""
    out, qty, cur = [], 0.0, None
    try:
        with open(os.path.join(LOGS, "events.jsonl"), encoding="utf-8") as f: lines = f.readlines()
    except FileNotFoundError: return out
    d0 = time.strftime("%Y-%m-%d", time.strptime(day, "%Y%m%d"))
    for l in lines:
        try: e = json.loads(l)
        except ValueError: continue
        t = time.mktime(time.strptime(e["t"], "%Y-%m-%d %H:%M:%S"))
        if e.get("ev") == "START": cur = e.get("symbol") or cur     # 체결 이벤트에 symbol 이 붙기 전(2026-09-01) 장부의 귀속처: 그때는 엔진이 하나였고 START 는 늘 symbol 을 담았다
        if (e.get("symbol") or cur) != sym: continue
        if e.get("side", side) != side: continue          # 포트폴리오 START 만 side 가 없다(책 전체 스냅샷); 단일책 시절 START 는 자기 방향을 담았다
        if e.get("ev") == "START":
            b = (e.get("books") or {}).get(side) or {}
            qty = sum(l[0] for l in (b.get("lots") or e.get("lots") or []))
        elif e.get("ev") == "FILL" and e.get("pos_qty") is not None: qty = float(e["pos_qty"])
        elif e.get("ev") == "STOP_HIT": qty = max(qty - float(e.get("qty") or 0), 0.0)
        elif e.get("ev") == "ADOPT": qty = float(e.get("qty") or 0)
        elif e.get("ev") == "STATE_DISCARDED": qty = 0.0
        else: continue
        out.append((t, qty))
    # keep everything (earlier events set the qty at the day's start); the caller samples by time
    return out

def held_at(tl, t):
    q = 0.0
    for tt, qq in tl:
        if tt <= t: q = qq
        else: break
    return q > 0

def capture(rows, tl, k_atr):
    if len(rows) < 30: return None
    ts = [r["ts"] // 1000 + 60 for r in rows]                         # a candle's close time
    held = [held_at(tl, t) for t in ts]
    up_all = up_held = dn_all = dn_held = 0.0
    for i in range(1, len(rows)):
        d = (rows[i]["c"] - rows[i - 1]["c"]) / rows[i - 1]["c"] * 100
        if d > 0: up_all += d; up_held += d if held[i] else 0.0
        else: dn_all += -d; dn_held += -d if held[i] else 0.0
    cl = [r["c"] for r in rows]; atr = wilder_atr(rows) or 0.0; th = k_atr * atr / cl[-1] if atr else 0.0
    legs_up = legs_up_held = legs_dn = legs_dn_held = 0.0; n_up = n_dn = 0
    if th > 0:
        piv = zigzag_pivots(cl, th)
        for (i0, k0), (i1, k1) in zip(piv, piv[1:]):
            size = abs(cl[i1] - cl[i0]) / cl[i0] * 100; frac = sum(1 for j in range(i0 + 1, i1 + 1) if held[j]) / max(i1 - i0, 1)
            if k1 == "H": legs_up += size; legs_up_held += size * frac; n_up += 1
            else: legs_dn += size; legs_dn_held += size * frac; n_dn += 1
    return dict(minutes=len(rows), in_mkt=sum(held) / len(rows), up_held=up_held / up_all if up_all else 0.0, dn_held=dn_held / dn_all if dn_all else 0.0,
                up_all=up_all, dn_all=dn_all, legs_up=n_up, legs_dn=n_dn, legs_up_held=legs_up_held / legs_up if legs_up else 0.0, legs_dn_held=legs_dn_held / legs_dn if legs_dn else 0.0)

def main():
    args = sys.argv[1:]
    day = args[args.index("--day") + 1] if "--day" in args else time.strftime("%Y%m%d", time.gmtime())
    p = load_params() or {}; syms = [args[args.index("--sym") + 1]] if "--sym" in args else portfolio(p)   # 포트폴리오면 책마다 한 줄 — 심볼 하나만 재면 나머지 책은 측정되지 않는다
    k = float((p.get("sig") or {}).get("dip_min_atr", 3.0))
    for sym in syms:
        rows = candles_of(day, sym); sp = strat_for(p, sym)
        for side in (sp.get("sides") or [sp.get("side", "long")]):
            c = capture(rows, timeline(day, sym, side), k)
            if not c: print(f"capture {day} {sym} {side}: not enough candles ({len(rows)})"); continue
            print(f"capture {day} {sym} {side}: {c['minutes']} min, in_mkt {c['in_mkt']:.2f} | minute moves: up held {c['up_held']:.2f} of {c['up_all']:.1f}%, "
                  f"down held {c['dn_held']:.2f} of {c['dn_all']:.1f}% | legs >= {k:g} ATR: up {c['legs_up']} held {c['legs_up_held']:.2f}, down {c['legs_dn']} held {c['legs_dn_held']:.2f}")

if __name__ == "__main__":
    main()
