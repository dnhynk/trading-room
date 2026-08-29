"""Keeps one job alive.  python -m bot.supervise record|cycle
Runs the job as a child, appends its stdout+stderr to logs/<job>.log, restarts on exit (5s, doubling to 60s; reset after
a 5-minute healthy run). `cycle` is not (re)started while the STOP file exists. Writes its own pid to logs/<job>.pid."""
import os, subprocess, sys, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS = {"record": [sys.executable, "-u", "-m", "bot.ws", "record"], "cycle": [sys.executable, "-u", "-m", "bot.cycle"],
        "nightly": [sys.executable, "-u", "-m", "bot.nightly"], "sweep": [sys.executable, "-u", "-m", "bot.sweep"]}

def main():
    job = sys.argv[1]; cmd = JOBS[job]
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    with open(os.path.join(ROOT, "logs", f"{job}.pid"), "w") as f: f.write(str(os.getpid()))
    backoff = 5
    while True:
        if job == "cycle" and os.path.exists(os.path.join(ROOT, "STOP")):
            time.sleep(10); continue
        t0 = time.time()
        with open(os.path.join(ROOT, "logs", f"{job}.log"), "a", encoding="utf-8") as log:
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} SUPERVISOR start pid={p.pid} {' '.join(cmd)}\n"); log.flush()
            rc = p.wait()
            backoff = 5 if time.time() - t0 > 300 else min(backoff * 2, 60)
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} SUPERVISOR exit rc={rc} after {time.time() - t0:.0f}s; restart in {backoff}s\n")
        time.sleep(backoff)

if __name__ == "__main__":
    main()
