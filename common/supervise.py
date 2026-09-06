"""Keeps one job alive.  python -m common.supervise record|cycle|cycle:SYMBOL|nightly|sweep|select
Runs the job as a child, appends its stdout+stderr to logs/<job>.log, restarts on exit (5s, doubling to 60s; reset after
a 5-minute healthy run). `cycle` is not (re)started while the STOP file exists, nor when its pinned symbol is not in
params["books"] (that engine refuses to start; restarting it would only loop). Writes its own pid to logs/<job>.pid.
`cycle` with no symbol is the basket supervisor: while params.json has `books` it keeps exactly one engine per book symbol
(`python -m common.cycle SYMBOL`, logs/cycle-SYMBOL.log) and reconciles on every params change — a symbol track_a/select.py added gets an
engine, and a symbol it removed is not restarted (that engine exits by itself, and select only removes a book that is flat). Without
`books` it runs the single unpinned engine exactly as before. `cycle:SYMBOL` still pins one engine by hand. Every child is put in a
Windows job object that dies with this supervisor, so stopping a supervisor never leaves its child running unsupervised."""
from common.paths import runtime_root
import os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.ws import load_params, portfolio, outside_books

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS = {"record": [sys.executable, "-u", "-m", "common.ws", "record"], "cycle": [sys.executable, "-u", "-m", "common.cycle"],
        "nightly": [sys.executable, "-u", "-m", "common.nightly"], "sweep": [sys.executable, "-u", "-m", "common.sweep"],
        "select": [sys.executable, "-u", "-m", "track_a.select"],
        "hunt": [sys.executable, "-u", "-m", "track_b.hunt"]}          # the short-hunting side pipeline (report-only unless params hunt.on = 1)

def stopped(): return os.path.exists(os.path.join(ROOT, "STOP"))

def gone(sym):
    """books 밖 심볼에 못박힌 엔진은 스스로 나간다(cycle.py) — 다시 올리면 5초마다 시작·종료를 되풀이하며 알림만 쌓인다."""
    return bool(sym) and outside_books(load_params(), sym)

JOB = None                                               # this supervisor's job object (Windows), created at the first spawn

def job_object():
    """Windows: a job whose processes are killed when the last handle to it closes — i.e. when this supervisor exits or is killed.
    Without it Stop-Process on a supervisor orphans its child: on 2026-09-03 the 08-29 recorder outlived its supervisor and a second
    recorder wrote the same tapes for 4.5 hours (every payload twice, lines torn at the 64 KB buffer boundaries). None where unsupported."""
    if os.name != "nt": return None
    import ctypes
    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]
    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    k = ctypes.windll.kernel32
    k.CreateJobObjectW.restype = ctypes.c_void_p; k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    h = k.CreateJobObjectW(None, None); info = Extended(); info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not h or not k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)): return None   # 9 = JobObjectExtendedLimitInformation
    return h

def assign(job, p):
    """Put child `p` into `job`; False when it could not be (then it would outlive the supervisor — the start line says so)."""
    if os.name != "nt": return True
    import ctypes
    return bool(job) and bool(ctypes.windll.kernel32.AssignProcessToJobObject(job, int(p._handle)))

def spawn(cmd, name):
    global JOB
    if JOB is None and os.name == "nt": JOB = job_object()
    with open(os.path.join(runtime_root(ROOT), "logs", f"{name}.log"), "a", encoding="utf-8") as log:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}   # the child appends to the SAME file these two writers use as utf-8, but it picks its own
        #                                                     encoding for the inherited handle — from a clean shell that is cp949, so one Korean traceback
        #                                                     line (common/cycle.py has Korean comments) made logs/<job>.log undecodable and killed the Monitor
        p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT); owned = assign(JOB, p)
        log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} SUPERVISOR start pid={p.pid} {' '.join(cmd)}{'' if owned else ' NOT IN JOB (outlives the supervisor)'}\n")
    return p

def note(name, msg):
    with open(os.path.join(runtime_root(ROOT), "logs", f"{name}.log"), "a", encoding="utf-8") as log:
        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} SUPERVISOR {msg}\n")

def basket():
    """One engine per params["books"] key (or the single unpinned engine while there is no books)."""
    kids, idle = {}, None                                    # symbol (None = unpinned) -> dict(p, t0, backoff, next)
    while True:
        p = load_params()
        if p is None: time.sleep(2); continue                # unreadable params: keep the children we have, never guess a basket
        if p.get("books"): want = [s for s in portfolio(p) if s]
        elif (p.get("hunt") or {}).get("on"):
            want = []                                        # hunt owns `books` and has not opened one yet (or a hand edit emptied it): the unpinned engine would fall back
            if idle != "hunt": note("cycle", "not starting the unpinned engine: hunt.on with an empty books"); idle = "hunt"   # to strat.symbol on the WHOLE wallet and the
        else: want = [None]                                  # common (track A) sides — the opposite of this track's one coin, one side contract (RULES 트랙 B)
        if want: idle = None
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
    from common.lifecycle import require_active
    require_active(base, ROOT)
    cmd = JOBS[base] + ([sym] if sym else [])
    job = job.replace(":", "-")                         # 로그·pid 파일명: Windows 는 콜론을 못 쓴다
    os.makedirs(os.path.join(runtime_root(ROOT), "logs"), exist_ok=True)
    with open(os.path.join(runtime_root(ROOT), "logs", f"{job}.pid"), "w") as f: f.write(str(os.getpid()))
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
