"""Nightly evidence report, run under the supervisor (python -m bot.supervise nightly): waits for 00:10 UTC each day, then writes
logs/nightly-YYYYMMDD.txt with, for every book in params.books (the basket, not a default symbol — the report measured TRUMPUSDT by
default until 2026-09-02, a symbol no longer traded or recorded), (1) the replay signal summary for the previous UTC day split by
signal source, volume-decay, CVD divergence, value-area position and regime, the legs and sweeps tables, the day as long / short / dual
and the side-automation counterfactuals; once per basket: bot.capture, (2) bot.recon — what the backtest got wrong against the live ledger
that day, which every other backtest number in the file inherits, (3) the tuner report over the whole basket (report only, never
--apply), (4) the symbol scanner. python -m bot.nightly --now runs it once immediately for the previous day (or --day YYYYMMDD)."""
import calendar, glob, json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.ws import load_params, load_states, portfolio
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")

def run_cmd(args, timeout=3600):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}   # 자식도 UTF-8 로 쓰게: 부모는 UTF-8 로 읽고 리포트 파일도 UTF-8 인데, 콘솔 기본값(cp949)은 한글 도구의 '—' 하나로 죽는다
    try:
        r = subprocess.run([sys.executable, "-m"] + args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", env=env)
        return (r.stdout or "") + (("\nSTDERR:\n" + r.stderr) if r.returncode else "")
    except Exception as e: return f"failed: {e}"

def _metrics(line):
    """The metrics dict a backtest run printed on its last line (json + ' (Ns)'), or None when the run failed."""
    try: return json.loads(line.rsplit(" (", 1)[0])
    except Exception: return None

def _vs(base, line):
    """'vs live: total +1.23, stops 1->0, dd 8.9->7.2 | ' — a counterfactual's total / stops / max_dd against the live dual run (NEXT 3: the
    risk columns next to the pnl, never the pnl alone); empty when either run has no metrics."""
    m = _metrics(line)
    if not base or not m: return ""
    return f"vs live: total {m['total'] - base['total']:+.2f}, stops {base['stops']}->{m['stops']}, dd {base['max_dd']:.1f}->{m['max_dd']:.1f} | "

def symbols(day, p=None, states=None):
    """(symbols, {symbol: sides}) the report covers: the books held now plus every engine whose state file was written since the report
    day began (00:00 UTC). A track-B book rotates within hours, and a report that read `books` alone measured CPUSDT for 20260903 while
    AKE / EGLD / MUBARAK had traded that day. A gone book's sides come from its state file (a hunt book is one side); a symbol never
    held reads both."""
    p = (load_params() or {}) if p is None else p; states = load_states() if states is None else states
    t0 = calendar.timegm(time.strptime(day, "%Y%m%d"))
    syms = [s for s in portfolio(p) if s]
    for s, st in states.items():
        try:
            if s not in syms and time.mktime(time.strptime(st["t"], "%Y-%m-%d %H:%M:%S")) >= t0: syms.append(s)
        except Exception: pass
    books = p.get("books") or {}
    return syms, {s: list((books.get(s) or {}).get("sides") or (states.get(s) or {}).get("sides") or ["long", "short"]) for s in syms}

def report(day):
    allf = sorted(f for f in glob.glob(os.path.join(ROOT, "data", "ws", "pub-*.jsonl*")) if not (f.endswith(".jsonl") and os.path.exists(f + ".gz")))
    files = [f for f in allf if os.path.basename(f)[4:12] == day]
    warm = [f for f in allf if os.path.basename(f)[4:12] < day][-2:]      # the previous day's last two hours warm the windows; their signals are not reported
    p = load_params() or {}; syms, bsides_of = symbols(day, p)
    out = [f"# nightly {day}  ({len(files)} recording files, {len(warm)} warm-up; symbols {', '.join(syms)})\n"]
    if files:
        for sym in syms:                                # every book gets its own tables: a basket's evidence is per symbol, never one default symbol's
            # a hunt book that left before 00:10 is not in `books`, and the backtest (strat_for) would size it as a basket slot on the common strat:
            # give it the track's profile and share, so its lines are the engine that actually traded it
            gone = ([x for k, v in {**((p.get("hunt") or {}).get("strat") or {}), "wallet_frac": 1.0}.items() for x in ("--strat", f"{k}={v}")]
                    if (p.get("hunt") or {}).get("on") and sym not in (p.get("books") or {}) else [])
            bt = ["bot.backtest"] + files + ["--sym", sym] + gone
            out.append(f"\n# {sym}\n")
            out.append("## signals (replay, forward 15m; split by source / volume decay / CVD divergence / value area / regime)\n")
            out.append(run_cmd(["bot.replay"] + warm + files + ["--sym", sym, "--quiet", "--day", day, "--by", "sell_decay,buy_decay,cvd_div,cvd_div_bear,vp_va,vp_dens,rg_er,leg_ow,U,D,side_hint_1h,side_hint_15m,daily_trend,dbl,brk"]))
            out.append("\n## legs (where the deceleration detectors fire vs the real extremes; speed-model evidence)\n")
            out.append(run_cmd(["bot.legs"] + warm + files + ["--sym", sym, "--quiet", "--day", day]))
            out.append("\n## sides (the day's tape as long / short / dual at live sizing; by_hint = realized pnl per book split by the 1H structure hint; capture = minute moves held / all per book)\n")
            base = None
            for sides in ("long", "short", "long,short"):
                line = (run_cmd(bt + ["--sides", sides]).strip().split("\n") or [""])[-1]
                out.append(f"--sides {sides}: " + line)
                if sides == "long,short": base = _metrics(line)      # the live dual run the counterfactuals below are read against
            for v in ("0", "1"):   # the current-leg read as a size scale (NEXT 1), both ways whatever params say: the live experiment's readout is on minus off, per symbol per day
                out.append(f"--sig rg_leg_on={v} (dual): " + (run_cmd(bt + ["--sides", "long,short", "--sig", f"rg_leg_on={v}"]).strip().split("\n") or [""])[-1])
            # the other live experiments of 2026-09-02 (NEXT 3, 5): the counterfactual of each, per symbol per day — live params vs the value it replaced.
            # de-risk runs both forms (NEXT 3, 2026-09-03): on0 = the lone-core cut that was live until 09-02 10:19, on1 = the under-units cut of the
            # 09-02 audit; each line leads with total / stops / max_dd against the live dual run so the revert rule reads the risk columns, not the pnl alone
            for label, ov in (("derisk on0 (pct 3, lone-core cut)", ["--strat", "derisk_pct=3.0", "--strat", "derisk_under_units=0"]),
                              ("derisk on1 (pct 3, under-units cut)", ["--strat", "derisk_pct=3.0", "--strat", "derisk_under_units=1"]),
                              ("retrace_frac 0.33", ["--strat", "retrace_frac=0.33"]),
                              # the cost-anchored trim gate vs a market-referenced one (NEXT 5, 2026-09-03: lost on 6 of 8 tapes, paid on the drift book only —
                              # the cross-section decides whether either form ever comes back): below-cost stall sales after a half-excursion bounce; the bounce-size gate alone
                              ("trim market frac 0.5 (below-cost bounce sale)", ["--strat", "trim_market_frac=0.5"]),
                              ("trim market atr 1.5 only (bounce-size gate, cost ignored)", ["--strat", "trim_market_atr=1.5", "--strat", "trim_market_only=1"])):
                line = (run_cmd(bt + ["--sides", "long,short"] + ov).strip().split("\n") or [""])[-1]
                out.append(f"counterfactual {label} (dual): {_vs(base, line)}" + line)
            # entry quality (2026-09-04, track B — RULES 담기 확인 절): the live hunt book opens a campaign only on a stall the velocity rule read with the flow
            # turned and the volume fading, and adds half units on unconfirmed stalls. Two readouts on the book's own side(s): the gate off (every stall opens,
            # adds whole — the pre-09-04 book) and the gate without the decay condition. The revert rule in NEXT's live 실험 표 reads these lines.
            bsides = ",".join(bsides_of[sym])
            own = base if bsides == "long,short" else _metrics((run_cmd(bt + ["--sides", bsides]).strip().split("\n") or [""])[-1])
            for label, ov in (("entry quality off (every stall opens, adds whole)", ["--strat", "entry_v=0", "--strat", "entry_flow=0", "--strat", "entry_decay=0", "--strat", "add_mult=0"]),
                              ("entry quality without decay", ["--strat", "entry_decay=0"]),
                              # the random baseline (NEXT 19): the gate off but 85% of entries vetoed at random, three seeds - a gate that only
                              # trades less lands inside these; a gate that reads something lands under them on stops / drawdown
                              *((f"random veto 85% seed {k}", ["--strat", "entry_v=0", "--strat", "entry_flow=0", "--strat", "entry_decay=0", "--strat", "add_mult=0", "--strat", "entry_random=0.85", "--strat", f"entry_seed={k}"]) for k in (1, 2, 3))):
                line = (run_cmd(bt + ["--sides", bsides] + ov).strip().split("\n") or [""])[-1]
                out.append(f"counterfactual {label} ({bsides}): {_vs(own, line)}" + line)
            for fol in ("15m", "brk"):                   # side automation counterfactuals: one side at a time, flipped at flat by the 15m structure / the last volume break
                out.append(f"--follow {fol}: " + (run_cmd(bt + ["--follow", fol]).strip().split("\n") or [""])[-1])
            out.append("\n## sweeps (stop hunts: sweeps under/over the engine's pivots, reclaim rate, depth vs the stop buffer, what follows a reclaim)\n")
            out.append(run_cmd(["bot.sweeps"] + warm + files + ["--sym", sym, "--quiet", "--day", day]))
            out.append("\n## phases (does the lifecycle read pay: forward move / live cycles per phase / order-flow footprints; 세력대항마 stage 2, NEXT 6.13)\n")
            out.append(run_cmd(["bot.phases", sym, "--day", day] + files, timeout=1800))
        out.append("\n# basket\n")
        out.append("## direction capture (live book from events.jsonl x the day's candles; up/dn held = share of minute moves and of legs that happened while holding)\n")
        out.append(run_cmd(["bot.capture", "--day", day]))
        out.append("\n## recon (백테스트 캘리브레이션: 경계 이벤트 없는 구간만 골라 같은 사이즈로 돌린 백테스트와 실매매 장부를 대조 — 아래 backtest 기반 수치는 전부 이 오차를 진다)\n")
        out.append(run_cmd(["bot.recon", "--day", day], timeout=3600))   # books 의 심볼마다 구간별 백테스트를 돌린다
        out.append("\n## slippage (live fills vs the mid at arrival, by symbol / maker-taker / role: the direct cost observation NEXT 6.5 wants instead of imp%)\n")
        out.append(run_cmd(["bot.slip", "--day", day]))
        out.append("\n## estimator vs live (scan-history p_up / trials per hour against the day's live cycles per symbol; NEXT 6.1-2, 8)\n")
        out.append(run_cmd(["bot.pair", "--day", day]))
        if (p.get("hunt") or {}).get("on"):
            out.append("\n## track B campaign evidence (entry capital, profiles, tails, costs; research only)\n")
            out.append(run_cmd(["bot.research_b", "--day", day], timeout=300))
        else:
            out.append("\n## tuner (report only; every candidate is judged on the whole basket's tapes)\n")
            out.append(run_cmd(["bot.tune", "--days", "7", "--workers", "4"], timeout=7200))
    else: out.append("no recordings for this day\n")
    out.append("\n## scanner\n")
    out.append(run_cmd(["bot.scan", "--top", "12"], timeout=900))
    os.makedirs(LOGS, exist_ok=True)
    path = os.path.join(LOGS, f"nightly-{day}.txt")
    with open(path, "w", encoding="utf-8") as f: f.write("\n".join(out))
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} REPORT {path}", flush=True)

def main():
    if "--now" in sys.argv or "--day" in sys.argv:
        day = sys.argv[sys.argv.index("--day") + 1] if "--day" in sys.argv else time.strftime("%Y%m%d", time.gmtime(time.time() - 86400))
        report(day); return
    while True:
        now = time.time(); t = time.gmtime(now)
        nxt = time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.gmtime(now + 86400)) + " 00:10:00", "%Y-%m-%d %H:%M:%S")) - time.timezone
        if t.tm_hour == 0 and t.tm_min < 10: nxt -= 86400        # today's run is still ahead
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} next report in {int(nxt - now)}s", flush=True)
        time.sleep(max(60, nxt - now))
        report(time.strftime("%Y%m%d", time.gmtime(time.time() - 3600)))

if __name__ == "__main__":
    main()
