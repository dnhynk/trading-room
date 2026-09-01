"""Nightly evidence report, run under the supervisor (python -m bot.supervise nightly): waits for 00:10 UTC each day, then writes
logs/nightly-YYYYMMDD.txt with, for every book in params.books (the basket, not a default symbol — the report measured TRUMPUSDT by
default until 2026-09-02, a symbol no longer traded or recorded), (1) the replay signal summary for the previous UTC day split by
signal source, volume-decay, CVD divergence, value-area position and regime, the legs and sweeps tables, the day as long / short / dual
and the side-automation counterfactuals; once per basket: bot.capture, (2) bot.recon — what the backtest got wrong against the live ledger
that day, which every other backtest number in the file inherits, (3) the tuner report over the whole basket (report only, never
--apply), (4) the symbol scanner. python -m bot.nightly --now runs it once immediately for the previous day (or --day YYYYMMDD)."""
import glob, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.ws import load_params, portfolio
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")

def run_cmd(args, timeout=3600):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}   # 자식도 UTF-8 로 쓰게: 부모는 UTF-8 로 읽고 리포트 파일도 UTF-8 인데, 콘솔 기본값(cp949)은 한글 도구의 '—' 하나로 죽는다
    try:
        r = subprocess.run([sys.executable, "-m"] + args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", env=env)
        return (r.stdout or "") + (("\nSTDERR:\n" + r.stderr) if r.returncode else "")
    except Exception as e: return f"failed: {e}"

def report(day):
    allf = sorted(f for f in glob.glob(os.path.join(ROOT, "data", "ws", "pub-*.jsonl*")) if not (f.endswith(".jsonl") and os.path.exists(f + ".gz")))
    files = [f for f in allf if os.path.basename(f)[4:12] == day]
    warm = [f for f in allf if os.path.basename(f)[4:12] < day][-2:]      # the previous day's last two hours warm the windows; their signals are not reported
    syms = [s for s in portfolio(load_params() or {}) if s]
    out = [f"# nightly {day}  ({len(files)} recording files, {len(warm)} warm-up; books {', '.join(syms)})\n"]
    if files:
        for sym in syms:                                # every book gets its own tables: a basket's evidence is per symbol, never one default symbol's
            out.append(f"\n# {sym}\n")
            out.append("## signals (replay, forward 15m; split by source / volume decay / CVD divergence / value area / regime)\n")
            out.append(run_cmd(["bot.replay"] + warm + files + ["--sym", sym, "--quiet", "--day", day, "--by", "sell_decay,buy_decay,cvd_div,cvd_div_bear,vp_va,vp_dens,rg_er,U,D,side_hint_1h,side_hint_15m,daily_trend,dbl,brk"]))
            out.append("\n## legs (where the deceleration detectors fire vs the real extremes; speed-model evidence)\n")
            out.append(run_cmd(["bot.legs"] + warm + files + ["--sym", sym, "--quiet", "--day", day]))
            out.append("\n## sides (the day's tape as long / short / dual at live sizing; by_hint = realized pnl per book split by the 1H structure hint; capture = minute moves held / all per book)\n")
            for sides in ("long", "short", "long,short"):
                out.append(f"--sides {sides}: " + (run_cmd(["bot.backtest"] + files + ["--sym", sym, "--sides", sides]).strip().split("\n") or [""])[-1])
            for fol in ("15m", "brk"):                   # side automation counterfactuals: one side at a time, flipped at flat by the 15m structure / the last volume break
                out.append(f"--follow {fol}: " + (run_cmd(["bot.backtest"] + files + ["--sym", sym, "--follow", fol]).strip().split("\n") or [""])[-1])
            out.append("\n## sweeps (stop hunts: sweeps under/over the engine's pivots, reclaim rate, depth vs the stop buffer, what follows a reclaim)\n")
            out.append(run_cmd(["bot.sweeps"] + warm + files + ["--sym", sym, "--quiet", "--day", day]))
        out.append("\n# basket\n")
        out.append("## direction capture (live book from events.jsonl x the day's candles; up/dn held = share of minute moves and of legs that happened while holding)\n")
        out.append(run_cmd(["bot.capture", "--day", day]))
        out.append("\n## recon (백테스트 캘리브레이션: 경계 이벤트 없는 구간만 골라 같은 사이즈로 돌린 백테스트와 실매매 장부를 대조 — 아래 backtest 기반 수치는 전부 이 오차를 진다)\n")
        out.append(run_cmd(["bot.recon", "--day", day], timeout=3600))   # books 의 심볼마다 구간별 백테스트를 돌린다
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
