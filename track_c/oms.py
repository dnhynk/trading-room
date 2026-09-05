"""Single-campaign spot OMS. Network calls are serialized by its owner.

Save intent before submit; apply cumulative fills once; resolve/cancel protective
orders before selling. Uncertain submissions are queried, never resubmitted.
"""
from decimal import Decimal as D
import datetime as dt
import time
import uuid

from .coinone import CoinoneError, decimal
from .microstructure import liquidate
from .accounting import marked_equity, residual_mark

TERMINAL = {"FILLED", "CANCELED", "NOT_TRIGGERED_CANCELED", "CANCELED_NO_ORDER", "CANCELED_LIMIT_PRICE_EXCEED", "CANCELED_UNDER_PRODUCT_UNIT", "REJECTED"}
LIVE = {"LIVE", "PARTIALLY_FILLED", "PARTIALLY_CANCELED", "NOT_TRIGGERED", "NOT_TRIGGERED_PARTIALLY_CANCELED", "TRIGGERED"}
# Documented request rejection. 104 on a lookup, duplicate IDs, timeouts and
# server failures do not establish whether a submission was accepted.
REJECTED = {4, 8, 10, 11, 12, 20, 21, 22, 23, 24, 25, 27, 40, 50, 51, 52, 53, 54,
            101, 103, 105, 107, 108, 109, 111, 120, 121, 122, 123, 130, 131, 132, 133,
            151, 161, 162, 163, 164, 165, 166, 167, 300, 305, 306, 307, 308, 309, 310, 313, 314}


class OMS:
    def __init__(self, config, client, store, *, clock=time.time):
        self.config, self.client, self.store, self.clock = config, client, store, clock
        self.state = store.load() or dict(version=2, realized="0", day=None, day_start="0", day_realized="0",
                                         campaign=None, orders={}, halt=None, cooldown=0, cash_krw="0",
                                         capital_initialized=False, capital_at=0, initial_equity="0", external_flows="0")
        if self.state.get("version") == 1:
            if self.state["campaign"] or self.state["orders"] or D(self.state["realized"]) or D(self.state["day_realized"]):
                raise RuntimeError("legacy exposure or trading history requires explicit capital reconciliation")
            self.state.pop("capital_base", None)
            self.state.update(version=2, cash_krw="0", capital_initialized=False, capital_at=0,
                              initial_equity="0", external_flows="0", day_start="0")
            self.save("CAPITAL_MODEL_MIGRATION", model="account_equity")
        if self.state.get("version") != 2:
            raise RuntimeError("unknown Track C state version")
        self.state.setdefault('research_loss_day','0')
        self.state.setdefault('residuals',{})  # coin -> dust inventory kept for the next campaign
        self.roll_day()

    @property
    def campaign(self):
        return self.state["campaign"]

    @property
    def equity(self):
        return max(D(0), marked_equity(self.state))

    def sync_cash(self, total_krw):
        """At flat, bind to actual cash; cash changes are never invented trade PnL."""
        if self.campaign or self.active():
            return False  # reconcile fills first; reserved buy cash is still capital
        actual = decimal(total_krw)
        initial = not self.state["capital_initialized"]
        delta = D(0) if initial else actual-D(self.state["cash_krw"])
        if initial:
            self.state.update(initial_equity=str(actual), day_start=str(actual))
        self.state.update(cash_krw=str(actual), capital_initialized=True, capital_at=self.clock(),
                          external_flows=str(D(self.state["external_flows"])+delta))
        self.save("CAPITAL_INITIALIZED" if initial else "EXTERNAL_CAPITAL" if delta else "CAPITAL_SYNC",
                  balance=str(actual), external_delta=str(delta), realized=self.state["realized"])
        return True

    def roll_day(self):
        today = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).date().isoformat()
        if self.state["day"] != today:
            self.state.update(day=today, day_start=str(self.equity), day_realized="0", research_loss_day='0')
            if self.state["halt"] == "DAILY_LOSS":
                self.state["halt"] = None
            self.store.save(self.state, "DAY", day=today)

    def remaining_risk(self, mark=None):
        unrealized = D(0)
        equity = self.equity
        if self.campaign:
            c = self.campaign
            value = D(c["qty"])*D(str(mark) if mark is not None else c["mark"])
            unrealized = min(D(0), value-D(c["cost"]))
            equity = D(self.state["cash_krw"])+value
        return max(D(0), equity*D(self.config["daily_loss_fraction"])+D(self.state["day_realized"])+unrealized)

    def save(self, kind, **fields):
        self.store.save(self.state, kind, **fields)

    def halt(self, reason):
        if self.state["halt"] != reason:
            self.state["halt"] = reason
            self.save("HALT", reason=reason)

    def active(self, role=None):
        return [o for o in self.state["orders"].values() if o["status"] not in TERMINAL and (role is None or o["role"] == role)]

    def enter(self, coin, plan, feature, minimum):
        if self.config["mode"] != "live" or not self.config["funding_confirmed"] or not self.state["capital_initialized"]:
            return False
        if not 0 <= self.clock()-self.state["capital_at"] <= 5:
            return False
        if self.campaign or self.active() or self.state["halt"] or self.clock() < self.state["cooldown"] or plan.get("reason"):
            return False
        q, px, trigger, limit = (D(plan[k]) for k in ("qty", "entry", "stop", "stop_limit"))
        quantitative=plan.get('policy') in ('quantitative','rule')
        if quantitative:
            ttl,hold=(decimal(plan[k],positive=True) for k in ('entry_ttl_s','hold_limit_s'))
            if ttl>60 or hold>300 or not plan.get('model'):
                raise ValueError('invalid quantitative time/model contract')
        if any(D(plan[k]) for k in ("maker", "taker")):
            return False  # fee currency must be verified before nonzero-fee admission
        if not (q > 0 and px > trigger > limit > 0):
            return False
        if quantitative and q*limit<decimal(minimum):
            return False
        if q*px > D(self.state["cash_krw"])*D(self.config["cash_fraction"]) or q*(px-limit) > min(self.remaining_risk(), self.equity*D(self.config["risk_fraction"])):
            return False
        residual = self.state["residuals"].pop(coin, None) if plan.get('take_mode') == 'resting' else None
        self.state["campaign"] = dict(id=uuid.uuid4().hex, coin=coin, qty=residual["qty"] if residual else "0", cost=residual["cost"] if residual else "0",
                                      net="0", bought="0", sold="0", residual=residual,
                                      first_fill=None, created=self.clock(), exit_reason=None, minimum=str(minimum),
                                      stop=plan["stop"], stop_limit=plan["stop_limit"], mark=str(residual_mark(residual)) if residual else plan["entry"],
                                      mark_at=residual.get('mark_at', 0) if residual else self.clock(), plan=plan, signal=feature,
                                      entry_deadline=self.clock()+(float(plan['entry_ttl_s']) if quantitative else self.config["signal"]["v_hl"]/2))
        self.save("CAMPAIGN_INTENT", coin=coin, plan=plan, residual=residual)
        self.submit("entry", "BUY", "LIMIT", plan["qty"], price=plan["entry"])
        return True

    def submit(self, role, side, kind, qty, **fields):
        c = self.campaign
        if c is None or self.active(role):
            raise RuntimeError("duplicate role or absent campaign")
        if side == "SELL" and (D(qty) > D(c["qty"]) or self.active("entry") or (role == "exit" and (self.active("protect") or self.active("take")))):
            raise RuntimeError("sale before entry/protection settlement or above owned quantity")
        cid = "tc-"+role+"-"+uuid.uuid4().hex
        order = dict(cid=cid, coin=c["coin"], role=role, side=side, type=kind, qty=qty, status="INTENT",
                     filled="0", gross="0", fee="0", created=self.clock(), **fields)
        self.state["orders"][cid] = order
        self.save("ORDER_INTENT", order=order)
        started=time.perf_counter()
        try:
            result = self.client.submit(order)
            order["exchange_id"] = result.get("order_id")
            order["status"] = "SUBMITTED"
            order['submit_rtt_ms']=(time.perf_counter()-started)*1000
            self.save("ORDER_SUBMITTED", cid=cid, rtt_ms=order['submit_rtt_ms'],
                      exit_request_to_ack_ms=(self.clock()-c['exit_requested_at'])*1000 if role=='exit' and c.get('exit_requested_at') is not None else None)
        except CoinoneError as exc:
            order["status"] = "REJECTED" if exc.code in REJECTED else "UNKNOWN"
            self.save("ORDER_REJECTED" if order["status"] == "REJECTED" else "ORDER_UNCERTAIN", cid=cid, error=str(exc))
            if role == "protect" and order["status"] == "REJECTED":
                self.request_exit("protection_rejected")
            elif role == "take" and order["status"] == "REJECTED":
                # A post-only sale that would cross means the bid already exceeds the target.
                self.request_exit("take_rejected")
            elif role == "exit" and order["status"] == "REJECTED":
                self.halt("EXIT_REJECTED")
        return order

    def apply(self, order, row):
        c = self.campaign
        if c is None or row.get("quote_currency") != "KRW" or row.get("target_currency") != order["coin"] or row.get("side") != order["side"]:
            raise CoinoneError("order identity mismatch")
        status = row.get("status")
        if status not in TERMINAL | LIVE:
            raise CoinoneError("unknown order status")
        q, fee = decimal(row["executed_qty"]), decimal(row["fee"])
        gross = q*decimal(row.get("average_executed_price") or 0)
        if q and not gross:
            raise CoinoneError("filled order has no execution price")
        if status in TERMINAL and row.get("remain_qty") is not None and decimal(row["remain_qty"]) > 0:
            raise CoinoneError("terminal order still reports a remainder")
        if q > D(order["qty"]) or q < D(order["filled"]) or fee < D(order["fee"]) or gross < D(order["gross"]):
            raise CoinoneError("nonmonotonic cumulative execution")
        dq, dg, df = q-D(order["filled"]), gross-D(order["gross"]), fee-D(order["fee"])
        # Current live admission requires zero account fees. Nonzero actual fees
        # require checking their denomination before attributing cash or stock.
        if fee:
            raise CoinoneError("nonzero fee denomination needs reconciliation")
        cash = D(self.state["cash_krw"])+(dg if order["side"] == "SELL" else -dg)-df
        if cash < 0:
            raise CoinoneError("execution exceeds reconciled cash")
        if order["side"] == "BUY":
            c["qty"], c["cost"] = str(D(c["qty"])+dq), str(D(c["cost"])+dg)
            c["bought"] = str(D(c["bought"])+dq)
            pnl = -df
            if dq and c["first_fill"] is None:
                c["first_fill"] = self.clock()
        else:
            owned = D(c["qty"])
            if dq > owned:
                raise CoinoneError("execution exceeds Track C inventory")
            basis = D(c["cost"])*dq/owned if owned else D(0)
            c["qty"], c["cost"] = str(owned-dq), str(D(c["cost"])-basis)
            c["sold"] = str(D(c["sold"])+dq)
            pnl = dg-basis-df
            if dq and order["role"] == "protect":
                c["exit_reason"] = "exchange_stop"
            elif dq and order["role"] == "take" and not D(c["qty"]):
                c["exit_reason"] = c["exit_reason"] or "take_profit"
        for obj, key in ((self.state, "realized"), (self.state, "day_realized"), (c, "net")):
            obj[key] = str(D(obj[key])+pnl)
        if c.get('plan',{}).get('research') and pnl<0:
            self.state['research_loss_day']=str(D(self.state['research_loss_day'])-pnl)
        self.state["cash_krw"] = str(cash)
        updates=dict(filled=str(q), gross=str(gross), fee=str(fee), status=status, exchange_id=row.get("order_id"))
        changed=any(order.get(k)!=v for k,v in updates.items())
        order.update(updates)
        if dq or changed:
            self.save("FILL" if dq else "ORDER_STATUS", cid=order["cid"], role=order["role"], qty=str(dq), gross=str(dg), fee=str(df), pnl=str(pnl), status=status,
                      observed_ms=int(self.clock()*1000),
                      exit_request_to_fill_seen_ms=(self.clock()-c['exit_requested_at'])*1000 if dq and order['side']=='SELL' and c.get('exit_requested_at') is not None else None)

    poll_interval = 0.0  # seconds between REST re-reads of a resting order; 0 = every drive

    def reconcile(self, force=False):
        now = self.clock()
        for order in list(self.active()):
            if not force and self.poll_interval and 0 <= now-order.get("checked", -1e9) < self.poll_interval:
                continue
            try:
                row = self.client.detail(order["coin"], order["cid"])
                self.apply(order, row)
                order["checked"] = now
            except CoinoneError as exc:
                # A not-found answer after a timeout is not permission to create
                # another order. Its durable intent continues to own the slot.
                self.store.event("RECONCILE_PENDING", cid=order["cid"], error=str(exc))
                if self.clock()-order["created"] > 30:
                    self.halt("ORDER_RECONCILIATION")

    def cancel(self, order):
        started=time.perf_counter()
        try:
            self.client.cancel(order["coin"], order["cid"])
            row = self.client.detail(order["coin"], order["cid"])
            self.apply(order, row)
        except CoinoneError as exc:
            self.store.event("CANCEL_PENDING", cid=order["cid"], error=str(exc))
        # A cancel response alone never releases the quantity reservation.
        self.store.event('EXECUTION_TIMING',cid=order['cid'],phase='cancel_and_reconcile',
                         elapsed_ms=(time.perf_counter()-started)*1000,terminal=order['status'] in TERMINAL)

    def request_exit(self, reason):
        if self.campaign and (not self.campaign["exit_reason"] or (self.campaign['exit_reason']=='one_tick_profit' and reason!='one_tick_profit')):
            self.campaign["exit_reason"] = reason
            self.campaign['exit_requested_at'] = self.clock()
            self.save("EXIT_REQUEST", reason=reason)

    def carry_residual(self, *, no_fill=False):
        """Transfer owned inventory and basis; this is not liquidation or a win."""
        c = self.campaign
        if self.active():
            raise RuntimeError('residual transfer before order settlement')
        residual = dict(qty=c['qty'], cost=c['cost'], mark=c['mark'], mark_at=c.get('mark_at', 0), t=self.clock())
        if D(c['qty']):
            self.state['residuals'][c['coin']] = residual
        c['exit_reason'] = c['exit_reason'] or 'dust'
        self.save('NO_FILL' if no_fill else 'CLOSE', campaign=c, residual=residual if D(c['qty']) else None,
                  inventory_flat=not bool(D(c['qty'])))
        self.state.update(campaign=None, orders={}, cooldown=self.clock())
        self.save('FLAT', inventory_flat=not bool(D(residual['qty'])))

    def drive(self, *, bid=None, fresh=False, feature=None, opposite=False, stopping=False, quantitative_decision=None, force_reconcile=False):
        self.roll_day()
        self.reconcile(force=force_reconcile)
        c = self.campaign
        if not c:
            return
        if fresh and bid is not None and D(str(bid)) > 0 and D(c["mark"]) != D(str(bid)):
            c["mark"] = str(bid)
            c['mark_at'] = self.clock()
            self.save("MARK", bid=str(bid), equity=str(self.equity))
        if stopping:
            self.request_exit("operator_stop")
        if self.state["halt"]:
            self.request_exit("halt")
        if not self.remaining_risk(bid):
            self.halt("DAILY_LOSS")
            self.request_exit("daily_loss")
        quantitative=c.get('plan',{}).get('policy') in ('quantitative','rule')
        resting=c.get('plan',{}).get('take_mode')=='resting'
        hold_limit=float(c['plan']['hold_limit_s']) if quantitative else 4*self.config['signal']['v_hl']
        if c["first_fill"] is not None and self.clock()-c["first_fill"] >= hold_limit:
            self.request_exit("time")
        if fresh and bid is not None and D(str(bid)) <= D(c["stop"]):
            self.request_exit("stop" if c['plan'].get('policy') == 'rule' else "premise")
        if opposite and fresh and not quantitative:
            self.request_exit("opposite_stall")
        if quantitative and quantitative_decision and not quantitative_decision.get('hold',True) and c['first_fill'] is not None:
            self.request_exit(quantitative_decision.get('reason') or 'continuation_value')
        if quantitative and quantitative_decision and quantitative_decision.get('take_profit') and c['first_fill'] is not None:
            # Risk/continuation exits already set above take precedence.
            self.request_exit('one_tick_profit')
        if not fresh and c["first_fill"] is not None and not (c.get('recovery_protection') and self.active('protect')):
            self.request_exit("market_data_unavailable")
        if self.active("entry"):
            # Keep tiny partials until TTL rather than intentionally manufacturing
            # untradeable dust. Never enlarge the originally reserved order.
            sale_price = min(D(str(bid)), D(c['stop_limit'])) if resting and bid is not None else D(str(bid)) if bid is not None else None
            enough = sale_price is not None and D(c["qty"])*sale_price >= D(c["minimum"])*D("1.05")
            stale_signal = (quantitative_decision or {}).get('cancel_entry',False) if quantitative else feature and (feature.get("v", 0) < -1 or feature.get("bs10", 1) <= .5)
            if c["exit_reason"] or not fresh or (enough and (not quantitative or resting)) or self.clock() >= c["entry_deadline"] or stale_signal:
                for order in self.active("entry"):
                    self.cancel(order)
            if self.active("entry"):
                return
        qty = D(c["qty"])
        if not qty or (c["first_fill"] is None and c.get("residual")):
            # No fill: a merged residual goes back to the residual ledger untouched.
            for order in self.active():
                self.cancel(order)
            if not self.active():
                if qty:
                    self.carry_residual(no_fill=True)
                    return
                self.save("CLOSE" if c["first_fill"] is not None else "NO_FILL", campaign=c)
                self.state.update(campaign=None, orders={}, cooldown=self.clock()+(0 if quantitative else self.config["signal"]["cooldown"]))
                self.save("FLAT")
            return
        if bid is not None and qty*D(str(bid)) < D(c["minimum"]):
            if not resting:
                self.halt("UNTRADEABLE_PARTIAL")
                return  # retain inventory and basis; never pretend a dust holding was sold
            if not c["exit_reason"] and self.active("take"):
                return  # the resting sale may still complete the remainder
            # Below-minimum remainder cannot be sold as a new order: carry it into the
            # next campaign of this coin at its cost basis instead of halting.
            for order in self.active():
                self.cancel(order)
            if self.active():
                return
            self.carry_residual()
            return
        if c["exit_reason"]:
            for order in self.active("protect")+self.active("take"):
                self.cancel(order)
            if self.active("protect") or self.active("take") or self.active("entry") or self.active("exit"):
                return
            qty = D(c["qty"])  # a racing protective fill may have removed it
            if resting and bid is not None and qty*D(str(bid)) < D(c['minimum']):
                # The cancellation itself may turn a sellable holding into dust.
                self.carry_residual()
                return
            if qty:
                rejected = [o for o in self.state["orders"].values() if o["role"] == "exit" and o["status"] == "REJECTED"]
                if rejected and self.clock()-max(o["created"] for o in rejected) < 5:
                    return
                # Only a documented rejection permits another identifier, and
                # available stock must still cover our full owned remainder.
                balances = self.client.balances()
                asset = next((r for r in balances if r.get("currency") == c["coin"]), None)
                if asset is None or decimal(asset["available"]) < qty:
                    self.halt("INVENTORY_UNAVAILABLE")
                    return
                fields={}
                if c['exit_reason']=='one_tick_profit' and c['plan'].get('take_profit'):
                    target=D(c['plan']['take_profit'])
                    if not fresh or bid is None or D(str(bid))<target:
                        c['exit_reason']=None
                        self.save('PROFIT_OPPORTUNITY_EXPIRED')
                        self.submit('protect','SELL','STOP_LIMIT',str(qty),price=c['stop_limit'],trigger_price=c['stop'])
                        return
                    fields['limit_price']=str(target)
                self.submit("exit", "SELL", "MARKET", str(qty),**fields)
        elif resting:
            if self.active('protect'):
                # A failure-recovery stop keeps ownership until healthy reference
                # data is ready; then cancel/reconcile before reserving a take.
                if not (fresh and (quantitative_decision or {}).get('recovery_ready')):
                    return
                for order in self.active('protect'):
                    self.cancel(order)
                if not self.active('protect'):
                    c['recovery_protection']=False
                    self.save('RECOVERY_PROTECTION_RETIRED')
                # Re-enter drive after a possible racing stop fill, with fresh qty.
                return
            # Resting post-only sale at the target; the exchange holds no stop meanwhile
            # (no OCO), so stop/defend/time exits are software decisions above.
            if not self.active("take") and not self.active("exit"):
                self.submit("take", "SELL", "LIMIT", str(qty), price=c["plan"]["take_profit"])
        elif not self.active("protect") and not self.active("exit"):
            self.submit("protect", "SELL", "STOP_LIMIT", str(qty), price=c["stop_limit"], trigger_price=c["stop"])
