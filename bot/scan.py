"""Symbol scanner for 순환매: rank liquid USDT-M perpetuals by two-way swing yield and flag pump-and-dump shapes.
  python -m bot.scan [--top 15] [--hours 16] [--min-vol 50e6] [--theta 0.7] [--json]
Universe: contracts with symbolStatus normal, prefiltered by 24h quote volume >= --min-vol (USDT). Then the last --hours of 1m candles.
Per symbol (window = 1m closes):
  sw/h     zigzag reversals >= theta % per hour        (how often a cycle is offered)
  yld%/h   sum of |swing| per hour                     (gross cycle opportunity)
  med%     median swing size
  ER       |net| / path over the window                (drift dominance; one-way = high)
  atr%     Wilder-14 1m ATR / price
  tick%    price tick / price   (the 0.15% unit trim needs tick% well under 0.05)
  spread   (ask - bid) / mid in bp
Pump-and-dump flags (any -> listed separately, not ranked): |24h change| >= 25%, max 15-min move >= 8%, one hour holding >= 35% of the
window's volume while moving the price >= 4%, |funding| >= 0.1%, ER >= 0.35. score = yld%/h x (1 - ER); liquidity is a gate, not a
multiplier. A weekend window with one busy hour is not a pump; the price move condition is what separates the two.
Report only: prints the table; --json also writes logs/scan.json. Changing strat.symbol is the agent's decision, and only when flat."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import from_env, PRODUCT
from bot.signal import zigzag, wilder_atr, structure_side

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def arg(flag, default):
    return type(default)(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default

def metrics(c, hours, theta):
    cl = [x["c"] for x in c]; n = len(cl); hrs = n / 60
    path = sum(abs(a - b) for a, b in zip(cl, cl[1:])); net = cl[-1] - cl[0]
    sw = zigzag(cl, theta / 100); absw = sorted(abs(s) for s in sw)
    vols = [x["v"] for x in c]; tot = sum(vols) or 1e-9
    # pump signature: the hour that carries the most volume also moved the price a lot (volume alone spikes on news too)
    hours_v = [(sum(vols[i:i + 60]) / tot, abs(cl[min(i + 59, n - 1)] / cl[i] - 1) * 100) for i in range(0, n, 60)]
    top_hour, top_hour_move = max(hours_v)
    mx15 = max(abs(cl[i] / cl[i - 15] - 1) for i in range(15, n)) * 100 if n > 15 else 0.0
    atr = wilder_atr(c[-100:]) or 0.0
    return dict(sw_h=len(sw) / hrs, yld_h=sum(absw) / hrs, med=absw[len(absw) // 2] if absw else 0.0,
                er=abs(net) / path if path else 0.0, atr_pct=atr / cl[-1] * 100, top_hour=top_hour, top_hour_move=top_hour_move,
                mx15=mx15, net=net / cl[0] * 100, hrs=hrs)

def main():
    top, hours, min_vol, theta = arg("--top", 15), arg("--hours", 16), arg("--min-vol", 50e6), arg("--theta", 0.7)
    b = from_env(); b.sync_time()
    contracts = {c["symbol"]: c for c in b.get("/api/v2/mix/market/contracts", auth=False, productType=PRODUCT) if c.get("symbolStatus") == "normal"}
    tickers = b.get("/api/v2/mix/market/tickers", auth=False, productType=PRODUCT)
    cand = []
    for t in tickers:
        s = t["symbol"]; c = contracts.get(s)
        if not c or not s.endswith("USDT"): continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < min_vol: continue
        px = float(t["lastPr"]); bid, ask = float(t.get("bidPr") or px), float(t.get("askPr") or px)
        tick = float(c["priceEndStep"]) * 10 ** -int(c["pricePlace"])
        cand.append(dict(symbol=s, px=px, qv=qv, chg=float(t.get("change24h") or 0) * 100, fund=float(t.get("fundingRate") or 0) * 100,
                         oi=float(t.get("holdingAmount") or 0) * px, tick_pct=tick / px * 100, spread_bp=(ask - bid) / px * 1e4))
    cand.sort(key=lambda x: -x["qv"])
    print(f"{len(cand)} symbols with 24h volume >= {min_vol:.0f}; pulling {hours}h of 1m candles ...", flush=True)
    rows = []
    for x in cand:
        try:
            c = b.candles(x["symbol"], "1m", min(1000, hours * 60))[:-1]
        except Exception as e:
            print(f"  {x['symbol']}: candles failed {e}"); continue
        if len(c) < 120: continue
        m = metrics(c, hours, theta); x.update(m)
        try:
            c15 = b.candles(x["symbol"], "15m", 200)[:-1]; x["side"] = structure_side(c15, wilder_atr(c15)) or "-"
        except Exception: x["side"] = "?"
        flags = []
        if abs(x["chg"]) >= 25: flags.append(f"24h{x['chg']:+.0f}%")
        if m["mx15"] >= 8: flags.append(f"15m{m['mx15']:.0f}%")
        if m["top_hour"] >= 0.35 and m["top_hour_move"] >= 4: flags.append(f"pump1h{m['top_hour'] * 100:.0f}%/{m['top_hour_move']:.0f}%")
        if abs(x["fund"]) >= 0.1: flags.append(f"fund{x['fund']:+.2f}%")
        if m["er"] >= 0.35: flags.append(f"ER{m['er']:.2f}")
        if x["tick_pct"] > 0.05: flags.append(f"tick{x['tick_pct']:.2f}%")
        x["flags"] = flags; x["score"] = m["yld_h"] * (1 - m["er"]) if not flags else 0.0
        rows.append(x); time.sleep(0.12)
    ok = sorted([r for r in rows if not r["flags"]], key=lambda r: -r["score"])
    bad = sorted([r for r in rows if r["flags"]], key=lambda r: -r["qv"])
    hdr = f"{'symbol':<12}{'score':>6}{'sw/h':>6}{'yld%/h':>8}{'med%':>6}{'ER':>6}{'atr%':>6}{'net%':>7}{'vol24h':>8}{'OI':>7}{'tick%':>7}{'spr':>5}  side"
    print(hdr)
    for r in ok[:top]:
        print(f"{r['symbol']:<12}{r['score']:6.2f}{r['sw_h']:6.2f}{r['yld_h']:8.2f}{r['med']:6.2f}{r['er']:6.2f}{r['atr_pct']:6.2f}{r['net']:+7.1f}"
              f"{r['qv'] / 1e6:7.0f}M{r['oi'] / 1e6:6.0f}M{r['tick_pct']:7.3f}{r['spread_bp']:5.1f}  {r.get('side', '?')}")
    print(f"--- flagged (pump/one-way/tick), {len(bad)}:")
    for r in bad[:top]:
        print(f"{r['symbol']:<12}{'':6}{r['sw_h']:6.2f}{r['yld_h']:8.2f}{r['med']:6.2f}{r['er']:6.2f}{r['atr_pct']:6.2f}{r['net']:+7.1f}{r['qv'] / 1e6:7.0f}M  {' '.join(r['flags'])}")
    if "--json" in sys.argv:
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "logs", "scan.json"), "w", encoding="utf-8") as f:
            json.dump(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), hours=hours, theta=theta, ranked=ok, flagged=bad), f)

if __name__ == "__main__":
    main()
