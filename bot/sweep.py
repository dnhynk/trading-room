"""Fee-rebate sweep, run under the supervisor (python -m bot.supervise sweep): every POLL seconds it reads the spot USDT balance and,
when at least MIN_USDT is available, transfers it (floored to 4 decimals) to the USDT-M futures account, where the engine's
equity-scaled sizing compounds it. Only USDT is touched (the rebate lands in spot as USDT around 07:00 UTC). Every transfer appends a
SWEEP event to logs/events.jsonl; a failure appends SWEEP_FAIL to logs/events.jsonl and logs/alerts.jsonl and is retried next poll.
python -m bot.sweep --once runs a single pass."""
import json, math, os, sys, time
from bot.bitget import from_env
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
POLL, MIN_USDT = 600, 1.0

def ev(kind, **kw):
    line = json.dumps(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), sec=None, ev=kind, **kw), ensure_ascii=False)
    os.makedirs(LOGS, exist_ok=True)
    with open(os.path.join(LOGS, "events.jsonl"), "a", encoding="utf-8") as fh: fh.write(line + "\n")
    if kind == "SWEEP_FAIL":
        with open(os.path.join(LOGS, "alerts.jsonl"), "a", encoding="utf-8") as fh: fh.write(line + "\n")
    print(line, flush=True)

def sweep(b):
    """One pass: spot USDT available -> usdt_futures. Returns the amount moved (0 when below MIN_USDT)."""
    rows = b.get("/api/v2/spot/account/assets", coin="USDT")
    avail = float(rows[0]["available"]) if rows else 0.0
    if avail < MIN_USDT: return 0.0
    amt = math.floor(avail * 1e4) / 1e4
    r = b.post("/api/v2/spot/wallet/transfer", fromType="spot", toType="usdt_futures", amount=f"{amt:.4f}", coin="USDT",
               clientOid=f"swp{int(time.time() * 1000)}")
    ev("SWEEP", amount=amt, spot_avail=avail, transfer_id=(r or {}).get("transferId"))
    return amt

def main():
    b = from_env(); once = "--once" in sys.argv
    while True:
        try: sweep(b)
        except Exception as e: ev("SWEEP_FAIL", err=str(e)[:200])
        if once: return
        time.sleep(POLL)

if __name__ == "__main__":
    main()
