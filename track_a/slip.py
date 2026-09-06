"""Execution cost of the live ledger: every FILL joined by clientOid to its PLACE (maker) or TAKER (market) event, which carry the mid at
arrival (the price the decision saw). Per symbol x scope x role: n, cost = signed (fill - arrival mid) in bp of the arrival mid (positive =
paid more than the arrival mid; a maker at the touch is negative by half the spread), drift = signed (mid at the fill - arrival mid),
the market's move against us while the order waited (adverse selection for makers, impact for takers), and the cost in USDT. The
direct observation NEXT 6 asks for in place of imp% = sigma sqrt(unit/ADV) (a regression needs thousands of fills). Records without a
mid (before the 2026-09-02 build) are skipped and counted.
  python -m track_a.slip [--day YYYYMMDD] [--from "YYYY-MM-DD HH:MM"] [--sym SYMBOL]
Read-only: logs/events.jsonl."""
from common.paths import runtime_root
import json, os, sys, time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(runtime_root(ROOT), "logs", "events.jsonl")

def _utc_day(t):
    return time.strftime("%Y%m%d", time.gmtime(time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S"))))

def rows(day=None, since=None, sym=None):
    """[(symbol, side, role, scope, qty, px, arrival, mid_fill, fee)] for fills with an arrival mid, and the count without one."""
    arrival, out, missing, last_sym = {}, [], 0, None
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"ev": "PLACE"' not in line and '"ev": "TAKER"' not in line and '"ev": "FILL"' not in line and '"ev": "START"' not in line: continue
            try: d = json.loads(line)
            except Exception: continue                        # engines append to one file: a torn line is skipped, never guessed
            ev = d["ev"]
            if ev == "START": last_sym = d.get("symbol") or last_sym; continue
            s = d.get("symbol") or last_sym
            if ev in ("PLACE", "TAKER"):
                if d.get("mid") is not None: arrival[d["oid"]] = (float(d["mid"]), d.get("role", "trim"))
                continue
            if d.get("scope") not in ("maker", "taker"): continue
            if since and d["t"] < since: continue
            if day and _utc_day(d["t"]) != day: continue
            if sym and s != sym: continue
            a = arrival.get(d.get("oid"))
            if a is None or d.get("mid") is None: missing += 1; continue
            out.append((s, d.get("side"), d.get("role"), d["scope"], float(d["qty"]), float(d["px"]), a[0], float(d["mid"]), float(d.get("fee") or 0)))
    return out, missing

def summarize(rs):
    """Per (symbol, scope, role): n, median / mean cost bp, median drift bp, cost USDT. Buying = a long book's add or a short book's trim."""
    g = defaultdict(list)
    for s, side, role, scope, qty, px, arr, mid, fee in rs:
        buying = (role == "buy") == (side == "long"); sgn = 1 if buying else -1
        cost = sgn * (px - arr) / arr * 1e4; drift = sgn * (mid - arr) / arr * 1e4
        g[(s, scope, role)].append((cost, drift, cost / 1e4 * qty * arr, fee))
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else float("nan")
    lines = [f"  {'symbol':<10}{'scope':<7}{'role':<5}{'n':>5}{'cost bp med':>12}{'mean':>8}{'drift bp med':>13}{'cost $':>9}{'fee $':>8}"]
    for (s, scope, role), xs in sorted(g.items()):
        lines.append(f"  {s:<10}{scope:<7}{role:<5}{len(xs):>5}{med([x[0] for x in xs]):>12.2f}{sum(x[0] for x in xs) / len(xs):>8.2f}"
                     f"{med([x[1] for x in xs]):>13.2f}{sum(x[2] for x in xs):>9.3f}{sum(x[3] for x in xs):>8.3f}")
    return "\n".join(lines)

def main(argv):
    a = argv; day = a[a.index("--day") + 1] if "--day" in a else None
    since = a[a.index("--from") + 1] if "--from" in a else None; sym = a[a.index("--sym") + 1] if "--sym" in a else None
    rs, missing = rows(day, since, sym)
    print(f"# slippage vs arrival mid — fills with a recorded arrival {len(rs)}, without {missing} (mid is recorded since the 2026-09-02 build)")
    if rs: print(summarize(rs))

if __name__ == "__main__":
    main(sys.argv[1:])
