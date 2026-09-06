"""Event watcher: prints one line per event (stdout). Read-only. Levels come from logs/alerts.json."""
from common.paths import runtime_root
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.bitget import from_env

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERTS = os.path.join(runtime_root(ROOT), "logs", "alerts.json")
SYMBOL = "BTCUSDT"
MIN_AVAIL = 30.0       # USDT free margin that makes a BTC trade worth taking
POLL = 20              # seconds
HEARTBEAT = 3600       # seconds
ONCE = "--once" in sys.argv

def emit(s): print(s, flush=True)
def ema(v, n):
    k = 2 / (n + 1); e = v[0]
    for x in v[1:]: e = x * k + e * (1 - k)
    return e
def bar_line(b, sym=SYMBOL):
    """One line per closed 5m bar with the numbers I decide on."""
    c = b.candles(sym, "3m", 80); cl = c[:-1]; x = cl[-1]
    closes = [k["c"] for k in cl]; vols = [k["v"] for k in cl[-21:-1]]
    trs = [max(cl[i]["h"] - cl[i]["l"], abs(cl[i]["h"] - cl[i - 1]["c"]), abs(cl[i]["l"] - cl[i - 1]["c"])) for i in range(1, len(cl))]
    a = sum(trs[:14]) / 14
    for t in trs[14:]: a = (a * 13 + t) / 14
    hh = max(k["h"] for k in cl[-21:-1]); ll = min(k["l"] for k in cl[-21:-1])  # last hour (20 x 3m)
    ts = time.strftime("%H:%M", time.localtime(x["ts"] / 1000))
    g = lambda v: f"{v:.5g}" if sym != "BTCUSDT" else f"{v:.0f}"
    e9, e21 = ema(closes, 9), ema(closes, 21)
    vr = x["v"] / (sum(vols) / len(vols))
    prev = cl[-2]
    big = max(0.8 * a, 0.0015 * x["c"])
    interesting = (vr >= 1.5 or (x["h"] - x["l"]) >= big or x["h"] > hh + 0.3 * a or x["l"] < ll - 0.3 * a
                   or ((x["c"] - e21) * (prev["c"] - e21) < 0 and (x["h"] - x["l"]) >= 0.5 * a))
    line = (f"BAR3 {sym[:-4]} {ts} o={g(x['o'])} h={g(x['h'])} l={g(x['l'])} c={g(x['c'])} v={x['v']:.0f} vr={vr:.1f} "
            f"e9={g(e9)} e21={g(e21)} hi1h={g(hh)} lo1h={g(ll)} atr={a/x['c']*100:.2f}%")
    return (x["ts"], line, interesting)

SPEED_SYMS = {"TRUMPUSDT"}  # overridden each poll by alerts.json "_speed" list if present
_speed_last = {}
def speed_line(b, sym, pos):
    """1m dip/pop speed read around the position's average. Returns a line or None."""
    c = b.candles(sym, "1m", 30); cl = c[:-1]
    if len(cl) < 10: return None
    last, prev = cl[-1], cl[-2]
    vavg = sum(k["v"] for k in cl[-21:-1]) / 20
    vr = last["v"] / vavg if vavg else 0
    rng = last["h"] - last["l"]
    lw = (min(last["o"], last["c"]) - last["l"]) / rng if rng else 0
    uw = (last["h"] - max(last["o"], last["c"])) / rng if rng else 0
    roc3 = (last["c"] - cl[-4]["c"]) / cl[-4]["c"] * 100
    roc3p = (cl[-4]["c"] - cl[-7]["c"]) / cl[-7]["c"] * 100
    if pos is None:  # flat: read the first dip after the recent 1m peak (entry timing)
        avg = max(k["h"] for k in cl[-30:]); long = True
        pos = {"total": "0", "unrealizedPL": "0"}
    else:
        avg = float(pos["openPriceAvg"]); long = pos["holdSide"] == "long"
    dev = (last["c"] / avg - 1) * 100
    sig = None
    decel = abs(roc3p) >= 0.3 and abs(roc3) <= 0.35 * abs(roc3p)  # rate fell to <=35% of the prior 3-bar rate
    if (dev <= -0.4 if long else dev >= 0.4):
        if (vr >= 1.5 and (lw if long else uw) >= 0.33) or (decel and (roc3p < 0) == long):
            sig = "DIP_SLOWING" if long else "POP_SLOWING"
    if (dev >= 0.4 if long else dev <= -0.4):
        if (vr >= 1.5 and (uw if long else lw) >= 0.33) or (decel and (roc3p > 0) == long):
            sig = "POP_STALLING" if long else "DIP_STALLING"
    if not sig: return None
    key = (sym, sig)
    if time.time() - _speed_last.get(key, 0) < 90: return None
    _speed_last[key] = time.time()
    return (f"SPEED {sym[:-4]} {sig} px={last['c']:.5g} avg={avg:.5g} dev={dev:+.2f}% vr={vr:.1f} lw={lw:.2f} uw={uw:.2f} "
            f"roc3={roc3:+.2f}% prev3={roc3p:+.2f}% size={pos['total']} upl={float(pos['unrealizedPL']):.2f}")

def levels_all():
    try:
        with open(ALERTS, encoding="utf-8") as f:
            j = json.load(f)
            return {k: (v if k in ("_speed", "_bars") else [float(x) for x in v]) for k, v in j.items()}
    except Exception: return {SYMBOL: []}

b = from_env(); b.sync_time()
st = {"pos": None, "avail_ok": None, "px": {}, "hb": 0, "errs": 0, "bar": {}}
while True:
    try:
        acct = b.account(SYMBOL); avail = float(acct["available"]); eq = float(acct["accountEquity"])
        poss = {f"{p['symbol']}:{p['holdSide']}": p for p in b.positions() if float(p.get("total", 0)) > 0}
        LV = levels_all(); SPD = set(LV.pop("_speed", SPEED_SYMS)); BARS = LV.pop("_bars", None); SYMS = list(LV)
        BARS = SYMS if BARS is None else [x for x in BARS if x in SYMS]
        pxs = {sym: float(b.ticker(sym)["lastPr"]) for sym in SYMS}; px = pxs.get(SYMBOL, 0.0)
        if st["pos"] is None:
            emit(f"INIT px={pxs} eq={eq:.2f} avail={avail:.2f} pos={[(k,p['total'],p['openPriceAvg'],p['unrealizedPL']) for k,p in poss.items()]} levels={LV}")
        else:
            for k, p in poss.items():
                if k not in st["pos"]:
                    _speed_last.clear()
                    emit(f"POS_OPEN {k} size={p['total']} entry={p['openPriceAvg']} lev={p['leverage']} margin={float(p['marginSize']):.2f} avail={avail:.2f}")
            for k, p in poss.items():
                if k in st["pos"] and p["total"] != st["pos"][k]["total"]:
                    _speed_last.clear()
                    emit(f"POS_SIZE {k} {st['pos'][k]['total']}->{p['total']} entry={p['openPriceAvg']} upl={float(p['unrealizedPL']):.2f} avail={avail:.2f} btc={px}")
            for k, p in st["pos"].items():
                if k not in poss:
                    emit(f"POS_CLOSED {k} entry={p['openPriceAvg']} lastUPL={p['unrealizedPL']} eq_now={eq:.2f} avail={avail:.2f} btc={px}")
            ok = avail >= MIN_AVAIL
            if ok != st["avail_ok"]:
                emit(f"MARGIN_{'FREE' if ok else 'LOCKED'} avail={avail:.2f} eq={eq:.2f} btc={px}")
            st["avail_ok"] = ok
            held = {p["symbol"]: p for p in poss.values()}
            for sym in SPD:
                try:
                    ln = speed_line(b, sym, held.get(sym))
                    if ln: emit(ln)
                except Exception as e:
                    emit(f"ERR speed {type(e).__name__}: {str(e)[:120]}")
            for sym, lv in LV.items():
                p0 = st["px"].get(sym); p1 = pxs[sym]
                for L in lv:
                    if p0 is not None and ((p0 - L) * (p1 - L) < 0 or p1 == L):
                        emit(f"CROSS {sym[:-4]} {L:g} {'UP' if p1 > L else 'DOWN'} px={p1} eq={eq:.2f} avail={avail:.2f}")
            if int(time.time()) % 180 < 2 * POLL:  # just after a 3m boundary: report bars worth a look, batched
                pos_s = " ".join(f"{k}:{p['total']}@{float(p['openPriceAvg']):.5g} upl={float(p['unrealizedPL']):.2f}" for k, p in poss.items()) or "flat"
                lines = []
                for sym in BARS:
                    bts, line, hot = bar_line(b, sym)
                    if bts != st["bar"].get(sym):
                        st["bar"][sym] = bts
                        if hot or (bts // 1000) % 900 == 0:  # interesting, or the :00/:15/:30/:45 pulse
                            lines.append(f"{line}{' *' if hot else ''} | px={pxs[sym]}")
                if lines:
                    emit(chr(10).join(lines) + chr(10) + f"  eq={eq:.2f} avail={avail:.2f} {pos_s}")
            if time.time() - st["hb"] >= HEARTBEAT:
                emit(f"HB px={pxs} eq={eq:.2f} avail={avail:.2f} pos={[(k,p['total'],p['unrealizedPL']) for k,p in poss.items()]}")
                st["hb"] = time.time()
        if st["pos"] is None:
            st["hb"] = time.time(); st["avail_ok"] = avail >= MIN_AVAIL
            for sym in BARS: st["bar"][sym] = bar_line(b, sym)[0]
        st["pos"], st["px"], st["errs"] = poss, pxs, 0
    except Exception as e:
        st["errs"] += 1
        if st["errs"] <= 3 or st["errs"] % 10 == 0:
            emit(f"ERR n={st['errs']} {type(e).__name__}: {str(e)[:200]}")
        time.sleep(60 if st["errs"] > 3 else 20)
    if ONCE: break
    time.sleep(POLL)
