"""Phase evidence — does the lifecycle read (bot/whale.py) actually pay, and what do the recordings' order flow add? (2026-09-03, the
세력대항마 stage-2 table; read-only, a nightly section.)
  python -m bot.phases SYM[,SYM] [--day YYYYMMDD] [--from "YYYY-MM-DD HH:MM"] [--hours 96] [--files F...]
Three joins, all keyed by the coin's own lifecycle phase at each hour (walk-forward, candles closed before the hour; Binance for a
young Bitget listing):
  (1) forward — phase -> the price's own next-1h / next-4h move (is markdown really followed by down, markup by up): the model check.
  (2) ledger  — (phase, side) -> the LIVE cycles the engine opened in that phase: n, win %, mean net %/cycle, sum net (does long-in-
                markup / short-in-markdown pay, and does trading the wrong side of a phase lose): what moves the WHALE thresholds.
  (3) flow    — phase -> order-flow footprints from the recordings (candles cannot show these): taker CVD slope %/h (absorption vs
                markup), open-interest change %/h (a shakeout harvests longs -> OI falls on a green bar), funding. Evidence only, not
                fed to `phase` until this table splits (NEXT 6.13; the script does not fit itself to one tape — CONCEPT).
Nothing here trades or writes params; it reads logs/events.jsonl and the recordings."""
import json, os, statistics, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.cycles import build as build_cycles
from bot.whale import timeline, load, WHALE
from bot.replay import lines as replay_lines

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHASES = ("markup", "climax", "markdown", "squeeze", "dead", "quiet", "unknown")

def _ms(t): return int(time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S")) * 1000)

def phase_at(tl, t_ms):
    """The phase of the walk-forward row whose hour brackets t_ms (the last row at or before it)."""
    ph = None
    for row in tl:
        if row[0] <= t_ms: ph = row[1]
        else: break
    return ph

def forward_table(tl):
    """phase -> (hours, mean next1h %, mean next4h %). The model check: the price's own move after each phase read."""
    acc = {}
    for t, ph, votes, f, nxt in tl:
        acc.setdefault(ph, []).append((f.get("next1h"), nxt))
    out = {}
    for ph, rows in acc.items():
        n1 = [a for a, _ in rows if a is not None]; n4 = [b for _, b in rows if b is not None]
        out[ph] = (len(rows), round(statistics.mean(n1), 2) if n1 else None, round(statistics.mean(n4), 2) if n4 else None)
    return out

def ledger_table(done, tl):
    """(phase, side) -> (n, win%, mean net%/cycle, sum net). Each live cycle is placed by the phase of the hour it opened in."""
    acc = {}
    for c in done:
        ph = phase_at(tl, _ms(c["t0"]))
        if ph is None: continue
        acc.setdefault((ph, c["side"]), []).append(c)
    out = {}
    for k, cs in acc.items():
        nets = [c["net"] / (c["qty"] * c["entry"]) * 100 for c in cs if c["qty"] and c["entry"]]
        out[k] = (len(cs), round(sum(1 for c in cs if c["net"] > 0) / len(cs), 2), round(statistics.mean(nets), 3) if nets else None, round(sum(c["net"] for c in cs), 2))
    return out

def flow_by_hour(files, sym):
    """{utc_hour_ms: {cvd, oi0, oi1, fund, trades}} from the recordings: taker CVD (buy - sell size), open interest at the hour's ends,
    funding. One streaming pass filtered by symbol; empty when no recordings are given."""
    hours = {}
    for path in files:
        for _recv, raw in replay_lines(path):
            if f'"instId":"{sym}"' not in raw and f'"{sym}"' not in raw: continue
            try: d = json.loads(raw)
            except Exception: continue
            ch = (d.get("arg") or {}).get("channel"); rows = d.get("data") or []
            if ch == "trade":
                for r in rows:
                    try: ts = int(r["ts"]); sz = float(r["size"]); h = ts - ts % 3_600_000
                    except Exception: continue
                    b = hours.setdefault(h, dict(cvd=0.0, oi0=None, oi1=None, fund=None, trades=0))
                    b["cvd"] += sz if r.get("side") == "buy" else -sz; b["trades"] += 1
            elif ch == "ticker":
                for r in rows:
                    try: ts = int(r["ts"]); h = ts - ts % 3_600_000; oi = float(r.get("holdingAmount") or 0)
                    except Exception: continue
                    b = hours.setdefault(h, dict(cvd=0.0, oi0=None, oi1=None, fund=None, trades=0))
                    if b["oi0"] is None: b["oi0"] = oi
                    b["oi1"] = oi; b["fund"] = float(r.get("fundingRate") or 0) * 100
    return hours

def flow_table(tl, flow):
    """phase -> (hours with flow, mean CVD/h in base units, mean OI change %/h, mean funding %)."""
    acc = {}
    for t, ph, votes, f, nxt in tl:
        b = flow.get(t - t % 3_600_000)
        if not b or not b["trades"]: continue
        oi_chg = (b["oi1"] / b["oi0"] - 1) * 100 if b["oi0"] else None
        acc.setdefault(ph, []).append((b["cvd"], oi_chg, b["fund"]))
    out = {}
    for ph, rows in acc.items():
        oi = [o for _, o, _ in rows if o is not None]; fu = [x for _, _, x in rows if x is not None]
        out[ph] = (len(rows), round(statistics.mean([c for c, _, _ in rows]), 0), round(statistics.mean(oi), 2) if oi else None, round(statistics.mean(fu), 4) if fu else None)
    return out

def report(sym, end_ms, hours, since, files):
    tl = timeline(sym, end_ms, hours, data=load(sym, end_ms, hours))
    for i, (t, ph, votes, f, nxt) in enumerate(tl):        # a next-1h column beside whale's next-4h (rows are one hour apart, step=1)
        f["next1h"] = round((tl[i + 1][3]["px"] / f["px"] - 1) * 100, 2) if i + 1 < len(tl) else None
    done = [c for c in build_cycles(since=since, sym=sym)[0] if c["symbol"] == sym]
    fwd = forward_table(tl); led = ledger_table(done, tl); flow = flow_table(tl, flow_by_hour(files, sym)) if files else {}
    out = [f"### {sym}  ({len([r for r in tl])} hourly reads, {len(done)} live cycles; phase at each hour, Binance for a young listing)"]
    out.append(f"  {'phase':9}{'hours':>6}{'next1h':>8}{'next4h':>8} | forward move after the read")
    for ph in PHASES:
        if ph in fwd: n, a, b = fwd[ph]; out.append(f"  {ph:9}{n:>6}{(f'{a:+.2f}' if a is not None else '-'):>8}{(f'{b:+.2f}' if b is not None else '-'):>8}")
    out.append(f"  {'phase':9}{'side':7}{'n':>4}{'win':>6}{'net%/cyc':>9}{'sumnet':>8} | the live cycles opened in each phase")
    for ph in PHASES:
        for side in ("long", "short"):
            if (ph, side) in led:
                n, w, m, s = led[(ph, side)]; out.append(f"  {ph:9}{side:7}{n:>4}{w:>6.2f}{(f'{m:+.3f}' if m is not None else '-'):>9}{s:>+8.2f}")
    if flow:
        out.append(f"  {'phase':9}{'hours':>6}{'cvd/h':>10}{'oi%/h':>7}{'fund':>8} | order-flow footprints (recordings; evidence only)")
        for ph in PHASES:
            if ph in flow: n, c, o, fu = flow[ph]; out.append(f"  {ph:9}{n:>6}{c:>10.0f}{(f'{o:+.2f}' if o is not None else '-'):>7}{(f'{fu:+.4f}' if fu is not None else '-'):>8}")
    return "\n".join(out)

def main():
    args = sys.argv[1:]; syms = args[0].split(",") if args and not args[0].startswith("--") else []
    day = end = since = None; hours = 96; files = []; i = 1 if syms else 0
    while i < len(args):
        if args[i] == "--day": day = args[i + 1]; i += 2
        elif args[i] == "--from": since = args[i + 1]; i += 2
        elif args[i] == "--hours": hours = int(args[i + 1]); i += 2
        elif args[i] == "--end": end = args[i + 1]; i += 2
        elif args[i] == "--files": i += 1;
        else:
            if args[i].endswith(".jsonl") or args[i].endswith(".gz"): files.append(args[i])
            i += 1
    if day and not end: end = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.mktime(time.strptime(day + " 235959", "%Y%m%d %H%M%S"))))
    end_ms = int((time.mktime(time.strptime(end, "%Y-%m-%d %H:%M")) if end else time.time()) * 1000)
    since = since or "2026-08-29 18:18"
    for sym in syms:
        print(report(sym, end_ms, hours, since, files) + "\n")

if __name__ == "__main__":
    main()
