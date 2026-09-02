"""Keeps one job alive.  python -m bot.supervise record|cycle|cycle:SYMBOL|nightly|sweep|select
Runs the job as a child, appends its stdout+stderr to logs/<job>.log, restarts on exit (5s, doubling to 60s; reset after
a 5-minute healthy run). `cycle` is not (re)started while the STOP file exists, nor when its pinned symbol is not in
params["books"] (that engine refuses to start; restarting it would only loop). Writes its own pid to logs/<job>.pid.
`cycle` with no symbol is the basket supervisor: while params.json has `books` it keeps exactly one engine per book symbol
(`python -m bot.cycle SYMBOL`, logs/cycle-SYMBOL.log) and reconciles on every params change — a symbol bot/select.py added gets an
engine, and a symbol it removed is not restarted (that engine exits by itself, and select only removes a book that is flat). Without
`books` it runs the single unpinned engine exactly as before. `cycle:SYMBOL` still pins one engine by hand."""
import os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.ws import load_params, portfolio, outside_books

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS = {"record": [sys.executable, "-u", "-m", "bot.ws", "record"], "cycle": [sys.executable, "-u", "-m", "bot.cycle"],
        "nightly": [sys.executable, "-u", "-m", "bot.nightly"], "sweep": [sys.executable, "-u", "-m", "bot.sweep"],
        "select": [sys.executable, "-u", "-m", "bot.select"],
        "hunt": [sys.executable, "-u", "-m", "bot.hunt"]}          # the short-hunting side pipeline (report-only unless params hunt.on = 1)

def stopped(): return os.path.exists(os.path.join(ROOT, "STOP"))

def gone(sym):
    """books 밖 심볼에 못박힌 엔진은 스스로 나간다(cycle.py) — 다시 올리면 5초마다 시작·종료를 되풀이하며 알림만 쌓인다."""
    return bool(sym) and outside_books(load_params(), sym)

def spawn(cmd, name):
    with open(os.path.join(ROOT, "logs", f"{name}.log"), "a", encoding="utf-8") as log:
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} SUPERVISOR start pid={p.pid} {' '.join(cmd)}\n")
    return p

def note(name, msg):
    with open(os.path.join(ROOT, "logs", f"{name}.log"), "a", encoding="utf-8") as log:
        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} SUPERVISOR {msg}\n")

def basket():
    """One engine per params["books"] key (or the single unpinned engine while there is no books)."""
    kids = {}                                                # symbol (None = unpinned) -> dict(p, t0, backoff, next)
    while True:
        p = load_params()
        if p is None: time.sleep(2); continue                # unreadable params: keep the children we have, never guess a basket
        want = [s for s in portfolio(p) if s] if p.get("books") else [None]
        for sym, k in list(kids.items()):
            if k["p"] is not None and k["p"].poll() is not None:
                rc, dur = k["p"].returncode, time.time() - k["t0"]
                k["backoff"] = 5 if dur > 300 else min(k["backoff"] * 2, 60)
                k["p"], k["next"] = None, time.time() + k["backoff"]
                note(name_of(sym), f"exit rc={rc} after {dur:.0f}s; {'restart in %ds' % k['backoff'] if sym in want else 'book gone: not restarting'}")
            if k["p"] is None and sym not in want: del kids[sym]
        for sym in want:
            k = kids.setdefault(sym, dict(p=None, t0=0.0, backoff=5, next=0.0))
            if k["p"] is None and time.time() >= k["next"] and not stopped():
                k["p"], k["t0"] = spawn(JOBS["cycle"] + ([sym] if sym else []), name_of(sym)), time.time()
        time.sleep(2)

def name_of(sym): return f"cycle-{sym}" if sym else "cycle"

def main():
    job = sys.argv[1]                                   # cycle:SYMBOL = 그 심볼에 못박힌 엔진(포트폴리오)
    base, _, sym = job.partition(":")
    cmd = JOBS[base] + ([sym] if sym else [])
    job = job.replace(":", "-")                         # 로그·pid 파일명: Windows 는 콜론을 못 쓴다
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    with open(os.path.join(ROOT, "logs", f"{job}.pid"), "w") as f: f.write(str(os.getpid()))
    if base == "cycle" and not sym: return basket()      # 바구니 감시견: books 의 심볼마다 엔진 하나
    backoff, idle = 5, None
    while True:
        why = ("STOP file" if stopped() else f"{sym} is not in params.books") if base == "cycle" and (stopped() or gone(sym)) else None
        if why:
            if idle != why: note(job, f"not starting: {why}"); idle = why
            time.sleep(10); continue
        idle = None
        t0 = time.time()
        p = spawn(cmd, job); rc = p.wait()
        backoff = 5 if time.time() - t0 > 300 else min(backoff * 2, 60)
        note(job, f"exit rc={rc} after {time.time() - t0:.0f}s; restart in {backoff}s")
        time.sleep(backoff)

if __name__ == "__main__":
    main()
