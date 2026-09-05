"""One causal event handler for replay and paper. No exchange client or credentials.

The economic policy is the explicitly frozen reference Strategy. Infrastructure
is independent: one capital pool, latency, cancel acknowledgement, L15 taker
depth, settlement cash flows, and campaign-level accounting.
"""
from collections import Counter
import copy
import math

from bot.risk import floor_qty
from bot.signal import Features, Strategy, apply_fill, pos_stats, sim_book, sim_match, unit_under_cap


class Book:
    def __init__(self, symbol, side, contract, cfg):
        self.symbol, self.side = symbol, side
        self.s = 1 if side == "long" else -1
        self.key = f"{symbol}:{side}"
        self.contract = contract
        p = {**cfg["strategy"], "side": side, "tick": contract["tick"], "qstep": contract["qstep"], "hunt": 1}
        p["fee_rt_pct"] = 100 * (cfg["execution"]["maker"] + cfg["execution"]["taker"])
        self.strategy = Strategy(p, cfg["signal"])
        self.pos = dict(lots=[], avg=None, last=None, last_buy_px=None, last_trim_px=None, halt=None, pause=False)
        self.work = {"buy": None, "trim": None}
        self.stop = None
        self.frozen = False
        self.campaign = None
        self.stopping = False
        self.exit_reason = None
        self.risk = 0.0

    @property
    def qty(self):
        return pos_stats(self.pos)[0]


class Engine:
    def __init__(self, config, name="reference", observe=True, sink=None):
        if config.data["version"] == 2 and type(self) is Engine:
            raise ValueError("schema 2 requires ResearchEngine; use quant compare")
        self.config, self.name = config, name
        self.cfg, self.observe, self.sink = config.data, observe, sink
        self.execution = self.cfg["execution"]
        self.features = {s: Features(self.cfg["signal"]) for s in self.cfg["books"]}
        self.books = {f"{s}:{side}": Book(s, side, contract, self.cfg) for s, contract in self.cfg["books"].items() for side in contract["sides"]}
        self.markets = {}
        self.last_feature = {}
        self.cash = self.peak = self.cfg["equity"]
        self.max_dd = 0.0
        self.day = None
        self.day_start = self.cash
        self.day_realized = 0.0
        self.day_stops = 0
        self.halted = False
        self.now = 0
        self.seq = 0
        self.reservations = {}
        self.quality, self.counts = Counter(), Counter()
        self.campaigns, self.events, self.observations, self.equity_curve = [], [], [], []
        self.last_equity_minute = None
        self.trading = True
        self.start = None

    def emit(self, kind, **fields):
        row = dict(t=self.now, kind=kind, **fields)
        if self.sink:
            self.sink(row)
        self.events.append(row)
        self.counts[kind] += 1

    def rates(self, t):
        return self.execution["maker"], self.execution["taker"]

    def mtm(self):
        value = 0.0
        for b in self.books.values():
            if b.qty:
                m = self.markets[b.symbol]
                price = m["mark"] if m.get("mark") and self.now - m["mark_t"] <= self.execution["quote_max_age_ms"] else m["mid"]
                value += b.s * (price - b.pos["avg"]) * b.qty
        return value

    def fresh(self, symbol):
        m = self.markets.get(symbol)
        return bool(m and m.get("bids") and m.get("asks") and self.now - m["quote_t"] <= self.execution["quote_max_age_ms"])

    def clock(self, t):
        if t < self.now:
            raise ValueError("engine time regression")
        self.now = t
        if self.start is None:
            self.start = t
        day = t // 86400000
        if day != self.day:
            self.day, self.day_start = day, max(0.0, self.cash + self.mtm())
            self.day_realized, self.day_stops, self.halted = 0.0, 0, False
            for b in self.books.values():
                b.pos["halt"] = None
            self.emit("DAY", capital=self.day_start)

    def funding(self, row):
        self.clock(row["t"])
        for b in self.books.values():
            if b.symbol == row["symbol"] and b.qty:
                cash = -b.s * b.qty * row["mark"] * row["rate"]
                self.cash += cash
                self.day_realized += cash
                b.campaign["funding"] += cash
                b.campaign["net"] += cash
                self.emit("FUNDING", book=b.key, cash=cash)
        self.account()

    def account(self):
        equity = self.cash + self.mtm()
        self.peak = max(self.peak, equity)
        self.max_dd = max(self.max_dd, self.peak - equity)
        if equity - self.day_start <= -self.day_start * self.cfg["strategy"]["daily_loss_frac"] or self.day_stops >= self.cfg["strategy"]["max_stops_day"] or equity <= 0:
            if not self.halted:
                self.emit("HALT", reason="daily_risk")
            self.halted = True
        minute = self.now // 60000
        if minute != self.last_equity_minute:
            self.equity_curve.append(dict(t=self.now, equity=equity))
            self.last_equity_minute = minute

    def resize(self, b, f):
        if b.frozen or not f.get("atr"):
            return
        wallet = max(0.0, self.cash + self.mtm())
        p, reference = b.strategy.p, self.cfg["strategy"]
        cap = wallet * reference["cap_frac"]
        qty = unit_under_cap(wallet * reference["unit_frac"] / f["mid"], cap, f["atr"], reference["cap_min_atr"])
        qty = min(qty, wallet * reference["notional_frac"] / f["mid"])
        p.update(unit_qty=floor_qty(qty, b.contract["qstep"]), cap_usdt=cap,
                 max_notional=wallet * reference["notional_frac"], daily_loss_limit=self.day_start * reference["daily_loss_frac"])
        b.risk = cap

    def reserve(self, b, qty, px):
        if b.key in self.reservations:
            return True
        # The extracted pool is one campaign, including resting entry orders.
        cost = qty * px * (sum(self.rates(self.now)) + self.execution["slip_bps"] / 10000)
        remaining = self.day_start * self.cfg["strategy"]["daily_loss_frac"] + self.cash + self.mtm() - self.day_start
        if self.halted or not self.trading or self.reservations or b.risk + cost > remaining + 1e-12:
            self.counts["risk_refusals"] += 1
            return False
        self.reservations[b.key] = b.risk + cost
        b.frozen = True
        self.emit("RESERVE", book=b.key, nominal_risk=b.risk, cost_reserve=cost)
        return True

    def release(self, b):
        if not b.qty and not b.work["buy"] and not b.strategy.arm:
            self.reservations.pop(b.key, None)
            b.frozen = False

    def fill(self, b, role, qty, px, maker, order):
        qs = b.contract["qstep"]
        qty = floor_qty(min(qty, order["qty"] - order["filled"], b.qty if role == "trim" else math.inf), qs)
        if qty <= 0:
            return
        if role == "buy" and not b.qty:
            b.campaign = dict(book=b.key, symbol=b.symbol, side=b.side, t0=self.now, t1=None, wallet=self.cash + self.mtm(),
                              gross=0.0, fees=0.0, funding=0.0, net=0.0, turnover=0.0, stop=False,
                              config_id=self.config.id, entry_qty=0.0)
        fee = qty * px * order.get("fee_rates", self.rates(self.now))[0 if maker else 1]
        net = apply_fill(b.pos, b.s, role == "buy", qty, px, oid=order["oid"], fee=fee, lot=order.get("lot"))
        self.cash += net
        self.day_realized += net
        c = b.campaign
        c["fees"] += fee
        c["gross"] += net + fee
        c["net"] += net
        c["turnover"] += qty * px
        if role == "buy":
            c["entry_qty"] += qty
        b.strategy.on_fill(role, qty)
        if hasattr(b.strategy, "on_execution"):
            b.strategy.on_execution(self.now, role, px, b.qty)
        order["filled"] += qty
        if order["filled"] >= order["qty"] - qs / 2:
            b.work[role] = None
        self.emit("FILL", book=b.key, role=role, qty=qty, px=px, fee=fee, net=net, maker=maker, oid=order["oid"])
        if not b.qty:
            c["t1"], c["stop"] = self.now, b.stopping and b.exit_reason in {None, "disaster", "premise"}
            c["exit_reason"] = b.exit_reason or "reference_trim"
            self.campaigns.append(c)
            self.emit("CLOSE", book=b.key, net=c["net"], stop=c["stop"])
            b.campaign, b.stop = None, None
            if b.stopping and (b.exit_reason in {None, "disaster"} or c["net"] < 0):
                self.day_stops += 1
                b.pos["cooldown_until"] = self.now / 1000 + b.strategy.p["stop_cooldown_s"]
            b.stopping, b.exit_reason = False, None
            # Unacknowledged entry cancellation remains reserved until acknowledged.
            if b.work["buy"]:
                b.work["buy"]["cancel_at"] = min(b.work["buy"].get("cancel_at", math.inf), self.now + self.execution["latency_ms"])
        self.account()

    def new_order(self, b, role, want):
        px, qty = want[:2]
        kind = want[2] if len(want) > 2 else "maker"
        if kind not in {"maker", "taker"} or not math.isfinite(px + qty) or px <= 0:
            raise ValueError("invalid policy order")
        qty = floor_qty(qty, b.contract["qstep"])
        if role == "buy":
            qty = min(qty, floor_qty(max(0, b.strategy.p["unit_qty"] - b.qty), b.contract["qstep"]))
            qty = min(qty, floor_qty(max(0, b.strategy.p["max_notional"] / px - b.qty), b.contract["qstep"]))
        else:
            qty = min(qty, b.qty)
        if qty <= 0 or (role == "buy" and not self.reserve(b, qty, px)):
            b.strategy.arm = None if role == "buy" else b.strategy.arm
            return
        self.seq += 1
        b.work[role] = dict(oid=f"paper-{self.seq}", px=px, qty=qty, filled=0.0, kind=kind,
                            arrival=self.now + self.execution["latency_ms"], active=False,
                            t=self.now / 1000, queue=0.0, S=0.0, seen=0.0, lot=want[3] if len(want) > 3 else None,
                            fee_rates=self.rates(self.now))
        self.emit("ORDER", book=b.key, role=role, qty=qty, px=px, order_type=kind)

    def reconcile(self, b, desired):
        if b.qty:
            stop = desired.get("stop")
            if stop is not None:
                tick = next((unit for floor, unit in reversed(b.contract["price_ladder"]) if stop >= floor), b.contract["tick"]) if "price_ladder" in b.contract else b.contract["tick"]
                stop = round((math.ceil(stop / tick - 1e-10) if b.s > 0 else math.floor(stop / tick + 1e-10)) * tick, 12)
                if b.stop is None or b.s * (stop - b.stop) > 0:
                    b.stop = stop
            if desired.get("no_stop"):
                self.stop(b)
            if desired.get("exit_reason"):
                self.stop(b, desired["exit_reason"])
        else:
            b.stop = None
        if b.stopping:
            return
        for role in ("buy", "trim"):
            want, old = desired[role], b.work[role]
            if role == "buy" and (self.halted or not self.trading or not self.fresh(b.symbol)):
                want, b.strategy.arm = None, None
            same = old and want and abs(old["px"] - want[0]) < b.contract["tick"] / 2 and abs(old["qty"] - old["filled"] - want[1]) < b.contract["qstep"] / 2 and old["kind"] == (want[2] if len(want) > 2 else "maker")
            if old and not same:
                old.setdefault("cancel_at", self.now + self.execution["latency_ms"])
            if want and not old and self.fresh(b.symbol):
                self.new_order(b, role, want)
        self.release(b)

    def stop(self, b, reason="disaster"):
        if not b.stopping:
            b.stopping = True
            b.exit_reason = reason
            b.strategy.arm = None
            self.emit("STOP_TRIGGER" if reason == "disaster" else "EXIT_TRIGGER", book=b.key, trigger=b.stop, reason=reason)
        # Cancel acknowledgement precedes a market close. Resting orders can fill meanwhile.
        for order in b.work.values():
            if order and not order.get("stop_order"):
                order.setdefault("cancel_at", self.now + self.execution["latency_ms"])
        if b.qty and not any(b.work.values()):
            self.new_order(b, "trim", (self.markets[b.symbol]["mid"], b.qty, "taker"))
            if b.work["trim"]:
                b.work["trim"]["stop_order"] = True

    def market_fill(self, b, role, order):
        m = self.markets[b.symbol]
        buy = (role == "buy") == (b.s > 0)
        levels = m["asks" if buy else "bids"]
        remaining, quantity, notional = order["qty"] - order["filled"], 0.0, 0.0
        for level in levels:
            take = floor_qty(min(remaining, level[1]), b.contract["qstep"])
            remaining -= take
            quantity += take
            notional += take * level[0]
            level[1] -= take
            if remaining < b.contract["qstep"] / 2:
                break
        if quantity:
            price = notional / quantity * (1 + (1 if buy else -1) * self.execution["slip_bps"] / 10000)
            self.fill(b, role, quantity, price, False, order)
        if remaining >= b.contract["qstep"] / 2:
            self.quality["insufficient_depth"] += 1

    def execute(self, b, event):
        for role, order in list(b.work.items()):
            if not order:
                continue
            if order.get("cancel_at", math.inf) <= self.now:
                b.work[role] = None
                self.emit("CANCEL", book=b.key, role=role, oid=order["oid"])
                continue
            if order["arrival"] > self.now or not self.fresh(b.symbol):
                continue
            if order["kind"] == "taker":
                if event.channel == "books15":
                    self.market_fill(b, role, order)
                continue
            if not order["active"]:
                if event.channel != "books15":
                    continue
                m = self.markets[b.symbol]
                buy = (role == "buy") == (b.s > 0)
                opposite = m["asks"][0][0] if buy else m["bids"][0][0]
                if (order["px"] >= opposite if buy else order["px"] <= opposite):
                    b.work[role] = None
                    self.emit("POST_ONLY_CANCEL", book=b.key, role=role)
                    continue
                levels = m["bids" if buy else "asks"]
                queue = next((q for p, q in levels if p == order["px"]), 0.0)
                order.update(active=True, t=self.now / 1000, queue=queue, S=queue)
                continue  # The acknowledgement message cannot fill its own new order.
            args = [((b, role), order, (role == "buy") == (b.s > 0))]
            if event.channel == "trade":
                for trade in event.message["data"]:
                    matches = sim_match(args, float(trade["price"]), float(trade["size"]), trade["side"], b.contract["qstep"], t=self.now / 1000)
                    self.matches(b, role, order, matches)
                    if b.work[role] is not order:
                        break
            elif event.channel == "books15":
                m = self.markets[b.symbol]
                self.matches(b, role, order, sim_book(args, m["bids"], m["asks"], self.now / 1000, b.contract["qstep"]))

    def matches(self, b, role, order, matches):
        for _, _, quantity in matches:
            if quantity is None:
                b.work[role] = None
                self.emit("POST_ONLY_CANCEL", book=b.key, role=role)
            else:
                if role == "buy":
                    levels = self.markets[b.symbol]["bids" if b.s > 0 else "asks"]
                    quantity = min(quantity, floor_qty(sum(q for _, q in levels) * self.execution["max_participation"], b.contract["qstep"]))
                self.fill(b, role, quantity, order["px"], True, order)

    def process(self, event, feature_update=None):
        self.clock(event.t)
        sym = event.symbol
        m = self.markets.setdefault(sym, dict(bids=[], asks=[], quote_t=-math.inf, mark_t=-math.inf, mark=None, mid=0.0))
        if event.channel == "books15":
            row = event.message["data"][0]
            m.update(bids=[[float(p), float(q)] for p, q in row["bids"]], asks=[[float(p), float(q)] for p, q in row["asks"]], quote_t=self.now)
            m["mid"] = (m["bids"][0][0] + m["asks"][0][0]) / 2
        elif event.channel == "ticker":
            m.update(mark=float(event.message["data"][0]["markPrice"]), mark_t=self.now)
        related = [self.books[f"{sym}:{side}"] for side in self.cfg["books"][sym]["sides"]]
        for b in related:
            self.execute(b, event)
            if b.qty and b.stop and self.fresh(sym):
                mark = m["mark"] if self.now - m["mark_t"] <= self.execution["quote_max_age_ms"] else None
                if mark is None:
                    self.quality["stale_mark_while_positioned"] += 1
                    mark = m["bids"][0][0] if b.s > 0 else m["asks"][0][0]
                if b.s * (mark - b.stop) <= 0:
                    self.stop(b)
            if b.stopping:
                self.stop(b)
        # Availability clock, not exchange-time sorting: late information cannot enter the past.
        feat = self.features[sym]
        if feature_update is None:
            message = copy.deepcopy(event.message)
            message["ts"] = self.now
            if event.channel == "books15":
                message["data"][0]["ts"] = str(self.now)
            signals = feat.feed(message)
        else:
            signals = feature_update
        f = feat.f
        if f.get("t") is not None and f["t"] != self.last_feature.get(sym):
            self.last_feature[sym] = f["t"]
            fresh = self.fresh(sym) and self.now / 1000 - f["t"] <= 2
            if not fresh:
                self.quality["stale_feature_seconds"] += 1
            actionable = [s for s in signals if s.get("src") == "v" and not s.get("shadow") and ((s["sig"] == "DIP_SLOWING" and s.get("bs10", 0) > .5 and s.get("sell_decay")) or (s["sig"] == "POP_STALLING" and s.get("bs10", 1) < .5 and s.get("buy_decay")))]
            if self.observe and fresh and self.trading:
                self.observations.append(dict(t=self.now, symbol=sym, bid=m["bids"][0][0], ask=m["asks"][0][0],
                                              signals=["long" if s["sig"] == "DIP_SLOWING" else "short" for s in actionable]))
            self.account()
            if f.get("atr") and f.get("bid") and f.get("ask"):
                for b in related:
                    self.resize(b, f)
                    other = bool(self.reservations and b.key not in self.reservations)
                    b.pos["pause"] = self.halted or not self.trading or not fresh or other or b.strategy.p["unit_qty"] <= 0 or b.stopping
                    b.pos["unit_mult"] = b.strategy.p["against_daily_mult"] if feat.daily_trend and feat.daily_trend != ("up" if b.s > 0 else "down") else 1.0
                    working = {r: (w["px"], w["qty"] - w["filled"]) for r, w in b.work.items() if w}
                    desired = b.strategy.step(f, signals if fresh else [], b.pos, working)
                    self.reconcile(b, desired)
        # A newly filled position gets its stop before the next market event.
        for b in related:
            if b.qty and b.stop is None and f.get("atr") and f.get("bid"):
                desired = b.strategy.step(f, [], b.pos, {})
                self.reconcile(b, desired)
            self.release(b)
        self.account()
        return signals

    def result(self):
        unfinished = []
        for b in self.books.values():
            if b.campaign:
                row = dict(b.campaign)
                m = self.markets[b.symbol]
                touch = m["bids"][0][0] if b.s > 0 else m["asks"][0][0]
                row.update(qty=b.qty, avg=b.pos["avg"], open_pnl=b.s * (m["mid"] - b.pos["avg"]) * b.qty,
                           liquidation_estimate=b.s * (touch - b.pos["avg"]) * b.qty - touch * b.qty * (self.rates(self.now)[1] + self.execution["slip_bps"] / 10000),
                           quote_age_ms=self.now - m["quote_t"])
                unfinished.append(row)
        net = self.cash - self.cfg["equity"]
        return dict(name=self.name, config_id=self.config.id, start=self.start, end=self.now,
                    initial_equity=self.cfg["equity"], cash=self.cash, realized_net=net,
                    marked_equity=self.cash + self.mtm(), liquidated_estimate_net=net + sum(c["liquidation_estimate"] for c in unfinished),
                    max_drawdown=self.max_dd, campaigns=self.campaigns, unfinished=unfinished,
                    reservations=dict(self.reservations), quality=dict(self.quality), counts=dict(self.counts), equity_curve=self.equity_curve)
