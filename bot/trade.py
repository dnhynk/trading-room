"""My hands. Usage:
  python -m bot.trade status
  python -m bot.trade open long|short SIZE [--sl P] [--tp P]     # market entry carrying the SL/TP as presets (protected from the fill), then position SL/TP
  python -m bot.trade add  long|short SIZE [--sl P] [--tp P]     # add to existing position (position SL/TP follow the size; --sl/--tp move them)
  python -m bot.trade tpsl long|short [--sl P] [--tp P]          # move position SL/TP in place (modify), placing only what is missing; nothing is cancelled first
  python -m bot.trade close long|short [SIZE]                    # market reduce (whole position if SIZE omitted)
  python -m bot.trade limit long|short SIZE PRICE [--close]      # post-only maker order (entry, or --close to reduce)
  python -m bot.trade tp long|short PRICE                        # replace TP with post-only limit close of whole position
  python -m bot.trade cancel all|ORDER_ID                        # cancel pending limit orders
Every action appends one JSON line to logs/trades.jsonl."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import from_env, BitgetError
from bot.ws import load_params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "trades.jsonl")
S = None   # set in main(): --sym SYMBOL, else params.json strat.symbol (the engine's contract), else BTCUSDT

def log(**kw):
    kw["t"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f: f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    print(json.dumps(kw, ensure_ascii=False))

def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default

def status(b):
    a = b.account(S); t = b.ticker(S)
    print(f"btc={t['lastPr']} mark={t['markPrice']} | equity={float(a['accountEquity']):.2f} avail={float(a['available']):.2f} upl={a['unrealizedPL']} posMode={a['posMode']}")
    for p in b.positions():
        print(f"POS {p['symbol']} {p['holdSide']} size={p['total']} entry={p['openPriceAvg']} lev={p['leverage']} margin={float(p['marginSize']):.2f} upl={p['unrealizedPL']} liq={p['liquidationPrice']} be={p['breakEvenPrice']}")
    pl = b.pending_plan_orders(S).get("entrustedList") or []
    for o in pl:
        print(f"PLAN {o.get('planType')} trigger={o.get('triggerPrice')} side={o.get('side')} size={o.get('size')} id={o.get('orderId')}")
    po = b.pending_orders(S).get("entrustedList") or []
    for o in po: print(f"ORDER {o.get('side')} {o.get('tradeSide')} {o.get('size')} @{o.get('price')} {o.get('status')} id={o.get('orderId')}")

def side_plans(b, hold):
    """Pending plan orders of one side only (hedge accounts hold both sides; never touch the other one)."""
    return [o for o in b.pending_plan_orders(S).get("entrustedList") or [] if o.get("posSide", o.get("holdSide", hold)) == hold]

def set_tpsl(b, hold, sl=None, tp=None):
    """Position SL/TP with no unprotected window: an existing plan is moved in place (modify), a missing one is placed; a failure leaves
    the old plan standing."""
    plans = {o.get("planType"): o for o in side_plans(b, hold)}
    for plan, alias, px in (("pos_loss", "psl", sl), ("pos_profit", "ptp", tp)):
        if px is None: continue
        cur = plans.get(plan) or plans.get(alias)
        if cur:
            r = b.modify_pos_tpsl(S, cur["orderId"], px, hold); log(action="modify_" + plan, side=hold, trigger=px, id=cur["orderId"], resp=r)
        else:
            r = b.place_pos_tpsl(S, hold, sl=px if plan == "pos_loss" else None, tp=px if plan == "pos_profit" else None); log(action="place_" + plan, side=hold, trigger=px, resp=r)

def drop_presets(b, hold):
    """Cancel the entry order's preset SL/TP plans once the position-level ones stand (they cover only that order's size)."""
    for o in side_plans(b, hold):
        if o.get("planType") in ("loss_plan", "profit_plan", "sl", "tp"):
            b.cancel_plan(S, o["orderId"], plan_type=o.get("planType")); log(action="cancel_preset", planType=o.get("planType"), trigger=o.get("triggerPrice"), id=o["orderId"])

def main():
    global S
    S = arg("--sym") or ((load_params() or {}).get("strat") or {}).get("symbol") or "BTCUSDT"
    if "--sym" in sys.argv: i = sys.argv.index("--sym"); del sys.argv[i:i + 2]
    b = from_env(); b.sync_time(); b.refresh_mode(S)
    cmd = sys.argv[1]; print(f"symbol={S}")
    if cmd == "status": return status(b)
    side = sys.argv[2]; hold = side; order_side = "buy" if side == "long" else "sell"
    if cmd == "cancel": side = hold = order_side = None
    if cmd in ("open", "add"):
        size = sys.argv[3]; sl, tp = arg("--sl"), arg("--tp")
        preset = cmd == "open" and (sl or tp)                   # the entry carries its stop: protected from the first fill, no window for a network error to widen
        r = b.market_order(S, order_side, size, trade_side="open", sl=sl if preset else None, tp=tp if preset else None)
        log(action=cmd, side=side, size=size, orderId=r.get("orderId"), preset_sl=sl if preset else None)
        time.sleep(1.5)
        if sl or tp:
            try: set_tpsl(b, hold, sl, tp)                     # whole-position plans (follow later adds) ...
            except Exception as e:
                log(action="tpsl_FAILED", err=str(e), note="preset stop stays" if preset else "existing plans stay"); raise
            if preset: drop_presets(b, hold)                   # ... and only then the presets go
        status(b)
    elif cmd == "tpsl":
        set_tpsl(b, hold, arg("--sl"), arg("--tp"))
        status(b)
    elif cmd == "limit":
        size, price = sys.argv[3], sys.argv[4]; ts = "close" if "--close" in sys.argv else "open"
        r = b.limit_order(S, order_side, price, size, trade_side=ts); log(action="limit_"+ts, side=side, size=size, price=price, orderId=r.get("orderId"))
        time.sleep(1); status(b)
    elif cmd == "tp":
        price = sys.argv[3]
        for o in b.pending_plan_orders(S).get("entrustedList") or []:
            if o.get("planType") == "pos_profit" and o.get("posSide", hold) == hold:
                b.cancel_plan(S, o["orderId"], plan_type=o.get("planType", "profit_loss")); log(action="cancel_plan", planType="pos_profit", trigger=o.get("triggerPrice"), id=o["orderId"])
        for o in b.pending_orders(S).get("entrustedList") or []:
            if (o.get("tradeSide") == "close" or o.get("reduceOnly") == "YES") and o.get("posSide", hold) == hold:
                b.cancel_order(S, o["orderId"]); log(action="cancel_order", id=o["orderId"], price=o.get("price"))
        pos = next((p for p in b.positions() if p["symbol"] == S and p["holdSide"] == hold and float(p.get("total", 0)) > 0), None)
        if not pos: print("no position on that side"); return
        r = b.limit_order(S, order_side, price, pos["total"], trade_side="close"); log(action="tp_limit", side=side, size=pos["total"], price=price, orderId=r.get("orderId"))
        time.sleep(1); status(b)
    elif cmd == "cancel":
        tgt = sys.argv[2]
        for o in b.pending_orders(S).get("entrustedList") or []:
            if tgt == "all" or o["orderId"] == tgt:
                b.cancel_order(S, o["orderId"]); log(action="cancel_order", id=o["orderId"], price=o.get("price"), size=o.get("size"))
        status(b)
    elif cmd == "close":
        if len(sys.argv) > 3:
            r = b.market_order(S, order_side, sys.argv[3], trade_side="close"); log(action="close_partial", side=side, size=sys.argv[3], orderId=r.get("orderId"))
        else:
            r = b.close_position(S, hold); log(action="close_all", side=side, resp=r)
        time.sleep(1.5); status(b)
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
