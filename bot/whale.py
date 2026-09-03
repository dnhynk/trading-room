"""Whale footprints and the pump lifecycle phase — the 세력대항마's reading layer (2026-09-03, stage 1: candles + ticker only;
stage 2 adds taker CVD / OI / funding series from the recordings and the fingerprint tables — NEXT 6.13).
  python -m bot.whale SYM[,SYM...] [--end "YYYY-MM-DD HH:MM"] [--hours 48] [--step 1]
Prints a walk-forward phase timeline: every row uses only candles closed before its time; the `next4h` column is what the price did
afterwards (the validation column, never an input). Pure functions `footprints` / `phase` are what bot/hunt.py consumes.

THE MODEL (CONCEPT 실험 모드): an operator runs a coin through phases and each phase leaves footprints we record —
  markup    fresh money (24h volume >= episode_ratio x the coin's own 7-day median), a run into new highs, structure up or unreadable;
            shakeouts inside it are where the long cycle buys (the sweep-and-reclaim: bot.sweeps).
  climax    two votes while still near the high: effort_fail (the episode's biggest-volume hour sat at the price peak AND the hours
            after it closed red), a lower high on 15m, fat upper wicks, funding hot. The long stops adding. (TRUMP 08-28 14-22 KST read
            climax 14-20 h before the 08-29 crash; the final marker high + dump inside one hour is not readable from closed bars.)
  markdown  the top is in (>= down_off % under the 48h high) and the 15m structure points down: the short cycle's phase.
  squeeze   markdown with funding negative (late shorts crowded): the bounce that flushes them is coming — the short adds pause.
  dead      24h volume under a quarter of the peak seen while held: leave the coin.
  quiet     no episode (ratio < quiet_ratio and no churn): not our game, the toll wins.
Every threshold here is a first value; the evidence table (stage 2) is what moves them."""
import datetime as dt, statistics, sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from bot.bitget import Bitget
from bot.signal import wilder_atr, structure_side, zigzag_pivots

WHALE = dict(episode_ratio=4.0,   # 24h volume / own 7-day median: an episode
             big_run=100.0,       # a run of this % into the 48h high is an episode whatever the ratio (AKE +142% on an already busy baseline, 3.8x)
             quiet_ratio=2.0,     # under this, with no churn, the coin is quiet
             run_min=25.0,        # % run into the 48h high from the low before it
             top_off=15.0,        # climax is read only this close (%) under the 48h high
             down_off=10.0,       # markdown needs the price at least this far (%) under the high (with the 15m structure down) ...
             far_off=20.0,        # ... or this far under it after a run, structure unreadable: a lagging structure reader (its zigzag threshold is 2 x ATR15, 10-20%
             #                      on a pump coin) must not leave a coin 30% under its top in "unknown" (AKE 2026-09-03 13:00-14:00, audit)
             far_close=10.0,      # ... AND this far under the highest CLOSE: a wick high inflates `off` on any pullback (STO 2026-04-02 00:00, 30% under a spike
             #                      wick but ABOVE every prior close, then +230% more — a shakeout, not a markdown; AKE 07:00+ was 17-31% under its top close)
             climax_votes=2,      # footprints that must agree for climax
             post_red=2,          # red closes among the 3 hours after the max-volume hour (effort > result)
             upwick=0.4,          # mean upper-wick share of the last 4 x 15m ranges
             exh_bars=4,          # quiet exhaustion: the last exh_bars x 15m up-bodies fade (each <= the previous) while price still climbs,
             vol_dry=0.5,         # and the last bar's volume < vol_dry x the mean of the exh_bars before it. A climax on its own only after a
             #                      big_run pump at the high (AKE 2026-09-03: bodies +7.4 -> +0.5%, last 15m 1% of the window's volume, no red bar yet)
             fund_hot=0.1,        # funding %/8h: longs crowded (climax vote)
             fund_cold=-0.1,      # funding %/8h: shorts crowded (squeeze)
             twoway_dead=8.0,     # 1h two-way path %/day under which a coin has no churn
             dead_ratio=0.25)     # 24h volume under this share of the peak seen while held: dead

def _pct(a, b): return (a / b - 1) * 100 if b else 0.0

def footprints(hours, bars15, days, ticker=None, held=None, p=WHALE):
    """hours / bars15 / days = CLOSED candles oldest -> newest (>= 48 hours, >= 20 x 15m, >= 3 days); ticker = {qv, fund} or None
    (offline); held = the state kept for a held coin ({peak}) or None. Returns the footprint dict `phase` reads."""
    px = hours[-1]["c"]
    h48 = hours[-48:]; i_hi = max(range(len(h48)), key=lambda k: h48[k]["h"]); high48 = h48[i_hi]["h"]
    before = hours[max(0, len(hours) - 48 + i_hi - 72):len(hours) - 48 + i_hi + 1] or h48[:i_hi + 1]
    run = _pct(high48, min(x["l"] for x in before)); off = -_pct(px, high48); age_h = len(h48) - 1 - i_hi
    off_close = -_pct(px, max(x["c"] for x in h48))                                          # under the highest close (a wick is not a level the market accepted)
    i_v = max(range(len(h48)), key=lambda k: h48[k]["qv"])
    after = h48[i_v + 1:i_v + 4]; post_red = sum(1 for x in after if x["c"] < x["o"])
    vmax_share = h48[i_v]["qv"] / max(sum(x["qv"] for x in h48[-24:]), 1e-9) if i_v >= len(h48) - 24 else 0.0
    last = bars15[-4:]
    upwick = statistics.mean([(x["h"] - max(x["o"], x["c"])) / (x["h"] - x["l"]) if x["h"] > x["l"] else 0.0 for x in last]) if last else 0.0
    # quiet exhaustion: the up-move fades bar by bar while price still climbs, and the last bar's volume dries up — a top that rolls
    # over silently (volume-decay), the counterpart to the loud effort_fail rollover
    exhaustion = False; k = int(p["exh_bars"])
    if len(bars15) >= 2 * k:
        recent = bars15[-k:]; prior = bars15[-2 * k:-k]
        up = lambda blk: max([(x["c"] - x["o"]) / x["o"] for x in blk] + [0.0])   # the strongest up-push in the block (0 if none)
        fading = up(recent) < up(prior) and recent[-1]["c"] > recent[0]["o"]       # the biggest push weakened while price still net-climbs (robust to one up-tick)
        vprior = statistics.mean([x["qv"] for x in prior]) or 1e-9
        exhaustion = fading and recent[-1]["qv"] < p["vol_dry"] * vprior            # ... and the latest bar's volume dried up
    atr15 = wilder_atr(bars15); hint15 = structure_side(bars15, atr15) if atr15 else None
    lower_high = None
    if atr15 and len(bars15) >= 20:
        closes = [b["c"] for b in bars15]; th = max(1.0, 2 * atr15 / closes[-1] * 100) / 100
        hs = [closes[i] for i, k in zigzag_pivots(closes, th) if k == "H"]
        if len(hs) >= 2: lower_high = hs[-1] < hs[-2] and closes[-1] < hs[-1]   # a price above the last pivot high has no lower high (a fresh leg up)
    h24 = hours[-24:]; o24 = h24[0]["o"]
    twoway24 = sum(abs(x["c"] - x["o"]) / o24 for x in h24) * 100 - abs(_pct(h24[-1]["c"], o24))
    qv = float(ticker["qv"]) if ticker and ticker.get("qv") else sum(x["qv"] for x in h24)
    prior = [d["qv"] for d in days[-8:-1]]; base = statistics.median(prior) if len(prior) >= 3 else 0.0
    new = len(prior) < 3                                         # a fresh listing has no baseline: its whole life is the episode (ratio 99)
    peak = max(float((held or {}).get("peak") or 0.0), qv)
    return dict(px=px, high48=high48, run=round(run, 1), off=round(off, 1), off_close=round(off_close, 1), age_h=age_h, vmax_at_high=abs(i_v - i_hi) <= 2, post_red=post_red,
                vmax_share=round(vmax_share, 2), upwick=round(upwick, 2), lower_high=lower_high, exhaustion=exhaustion, hint15=hint15, twoway24=round(twoway24, 1),
                ratio=99.0 if new else round(qv / base, 1), new=new, qv=qv, fund=(ticker or {}).get("fund"), dead=bool(held) and qv < p["dead_ratio"] * peak,
                atr15_pct=round(atr15 / px * 100, 2) if atr15 else None)

def phase(f, p=WHALE):
    """(phase, votes). Ordered: dead > quiet > markdown/squeeze (the top is in and the structure is down) > climax (votes near the
    high) > markup (episode, run, near the high, structure not down) > unknown."""
    if f["dead"]: return "dead", ["dead"]
    if f["ratio"] < p["quiet_ratio"] and f["twoway24"] < p["twoway_dead"]: return "quiet", [f"ratio{f['ratio']}", f"twoway{f['twoway24']}"]
    down = f["off"] >= p["down_off"] and f["hint15"] == "short"
    far = (f["off"] >= p["far_off"] and f.get("off_close", 0.0) >= p["far_close"] and f["hint15"] is None
           and f["run"] >= p["run_min"])                                                     # the top is in by distance alone: under the top AND under the highest close, structure unreadable, run behind it
    if down or far:
        why = [f"off{f['off']}", "hint_short" if down else "far_off"]
        if f["fund"] is not None and f["fund"] <= p["fund_cold"]: return "squeeze", why + [f"fund{f['fund']}"]
        return "markdown", why
    votes = []
    if f["vmax_at_high"] and f["post_red"] >= p["post_red"]: votes.append(f"effort_fail{f['post_red']}")   # the biggest hour sat at the peak and the
    if f["upwick"] >= p["upwick"]: votes.append(f"upwick{f['upwick']}")                                     # hours after it closed red: effort > result
    if f["lower_high"]: votes.append("lower_high")
    if f["fund"] is not None and f["fund"] >= p["fund_hot"]: votes.append(f"fund{f['fund']}")
    if f.get("exhaustion"): votes.append("exhaustion")
    # climax: two votes near the high, OR quiet exhaustion after a big pump at the high (a silent volume-decay top the vote count misses)
    if f["off"] < p["top_off"] and (len(votes) >= p["climax_votes"] or (f.get("exhaustion") and f["run"] >= p["big_run"])): return "climax", votes
    episode = f["ratio"] >= p["episode_ratio"] or f["run"] >= p["big_run"]
    if episode and f["run"] >= p["run_min"] and f["off"] < p["down_off"] and f["hint15"] in ("long", None): return "markup", votes
    return "unknown", votes

def _closed(rows, end_ms, span_ms): return [r for r in rows if r["ts"] + span_ms <= end_ms]

BINANCE = "https://fapi.binance.com/fapi/v1/klines"
def binance_candles(sym, interval, limit=200, end_ms=None):
    """Binance USDT-M futures klines as our candle dicts (oldest -> newest), or [] when the symbol is not there / unreachable. Used for
    the SHAPE of a coin whose Bitget listing is hours old (user 2026-09-03: "상장 짧으면 바이낸스 차트 참고해") — trading stays on Bitget."""
    import json as _json, urllib.parse, urllib.request
    q = dict(symbol=sym, interval=interval, limit=str(limit))
    if end_ms: q["endTime"] = str(int(end_ms))
    try:
        with urllib.request.urlopen(BINANCE + "?" + urllib.parse.urlencode(q), timeout=10) as r: rows = _json.loads(r.read().decode())
    except Exception: return []
    if not isinstance(rows, list): return []
    return [dict(ts=int(x[0]), o=float(x[1]), h=float(x[2]), l=float(x[3]), c=float(x[4]), v=float(x[5]), qv=float(x[7])) for x in rows]

def longer_history(sym, hours, bars15, days, min_hours=48, end_ms=None):
    """(hours, bars15, days, src): Binance's candles when Bitget's 1H history is shorter than min_hours and Binance has more of it."""
    if len(hours) >= min_hours: return hours, bars15, days, "bitget"
    bh = binance_candles(sym, "1h", 200, end_ms)
    if len(bh) <= len(hours) + 1: return hours, bars15, days, "bitget"
    bm = binance_candles(sym, "15m", 200, end_ms); bd = binance_candles(sym, "1d", 20, end_ms)
    cut = lambda rows, span: [r for r in rows if r["ts"] + span <= (end_ms or 4e12)]
    return cut(bh, 3_600_000)[:-1] if not end_ms else cut(bh, 3_600_000), (cut(bm, 900_000)[:-1] if not end_ms else cut(bm, 900_000)) or bars15, (cut(bd, 86_400_000)[:-1] if not end_ms else cut(bd, 86_400_000)) or days, "binance"

def load(sym, end_ms, hours=48, b=None, src=None):
    """Enough history to walk `hours` hourly steps ending at end_ms: 1H (48 + 72 + hours), 15m (200 + 4 x hours), 1D (20). src "binance"
    (or a Bitget listing shorter than 48 closed hours) reads Binance USDT-M futures instead — the shape, not the venue we trade."""
    b = b or Bitget("", "", "")
    def pages(gran, span_ms, need):
        out, cursor = {}, end_ms
        while len(out) < need:
            rows = b.history_candles(sym, gran, cursor, 200)
            if not rows: break
            out.update({r["ts"]: r for r in rows}); cursor = rows[0]["ts"]
            if len(rows) < 200: break
        return [out[k] for k in sorted(out)]
    d = dict(h=pages("1H", 3_600_000, 120 + hours), m15=pages("15m", 900_000, 200 + 4 * hours), d=pages("1D", 86_400_000, 20), src="bitget") if src != "binance" else dict(h=[], m15=[], d=[], src="bitget")
    if src == "binance" or len(d["h"]) < 48:
        bh = binance_candles(sym, "1h", min(1500, 120 + hours + 8), end_ms)
        if len(bh) > len(d["h"]):
            d = dict(h=bh, m15=binance_candles(sym, "15m", min(1500, 200 + 4 * hours + 8), end_ms), d=binance_candles(sym, "1d", 20, end_ms), src="binance")
    return d

def timeline(sym, end_ms, hours=48, step=1, data=None, held=None, p=WHALE):
    """[(t, phase, votes, footprints, next4h %)] walking forward from end - hours to end, each row from candles closed before t."""
    d = data or load(sym, end_ms, hours)
    out = []
    for k in range(hours, -1, -step):
        t = end_ms - k * 3_600_000
        h = _closed(d["h"], t, 3_600_000); m = _closed(d["m15"], t, 900_000); dd = _closed(d["d"], t, 86_400_000)
        if len(h) < 6 or len(m) < 20: continue                    # a listing a few hours old still reads (48h windows just shorten)
        f = footprints(h, m, dd, None, held, p); ph, votes = phase(f, p)
        fut = [x for x in d["h"] if t <= x["ts"] < t + 4 * 3_600_000]
        nxt = _pct(fut[-1]["c"], h[-1]["c"]) if fut else None
        out.append((t, ph, votes, f, nxt))
    return out

def main():
    args = sys.argv[1:]; syms = args[0].split(",") if args and not args[0].startswith("--") else []
    end, hours, step, src = None, 48, 1, None; i = 1 if syms else 0
    while i < len(args):
        if args[i] == "--end": end = args[i + 1]; i += 2
        elif args[i] == "--hours": hours = int(args[i + 1]); i += 2
        elif args[i] == "--step": step = int(args[i + 1]); i += 2
        elif args[i] == "--src": src = args[i + 1]; i += 2
        else: i += 1
    end_ms = int((time.mktime(time.strptime(end, "%Y-%m-%d %H:%M")) if end else time.time()) * 1000)
    for sym in syms:
        data = load(sym, end_ms, hours, src=src)
        print(f"# {sym}  phase timeline, {hours}h to {time.strftime('%Y-%m-%d %H:%M', time.localtime(end_ms / 1000))} KST, candles from {data['src']} (closed before each row; next4h = what followed)")
        print(f"{'time':12}{'phase':9}{'px':>10}{'ratio':>6}{'run':>6}{'off':>6}{'age':>4}{'vmax':>5}{'red':>4}{'upw':>5}{'lowhi':>6}{'hint':>6}{'2way':>6}{'next4h':>8}  votes")
        for t, ph, votes, f, nxt in timeline(sym, end_ms, hours, step, data=data):
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(t / 1000)):12}{ph:9}{f['px']:>10.5g}{f['ratio']:>6.1f}{f['run']:>6.0f}{f['off']:>6.1f}{f['age_h']:>4}"
                  f"{'Y' if f['vmax_at_high'] else '-':>5}{f['post_red']:>4}{f['upwick']:>5.2f}{str(f['lower_high'])[:5]:>6}{str(f['hint15']):>6}{f['twoway24']:>6.0f}"
                  f"{(f'{nxt:+.1f}%' if nxt is not None else '-'):>8}  {' '.join(votes)}")

if __name__ == "__main__":
    main()
