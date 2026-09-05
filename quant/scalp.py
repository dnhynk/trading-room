"""A frozen short-horizon hypothesis, isolated from the production B policy."""
from collections import Counter, deque
import math

from bot.risk import floor_qty
from bot.signal import Strategy, pos_stats
from .engine import Engine
from .markouts import vwap
from .venues import round_price, tick_at


class ResearchStrategy(Strategy):
    def __init__(self, p, sig, research, ladder=None):
        super().__init__(p, sig)
        self.research = research
        self.price_ladder = ladder or [[0, p["tick"]]]
        self.short_exit = research["policy"] in {"scalp", "scalp_exit"}
        if research["policy"] == "scalp":
            self.p["buy_ttl_s"] = research["entry_ttl_s"]
        self.now_ms = 0
        self.trades = deque()
        self.phase_side = None
        self.quote_fresh = True
        self.first_fill_ms = None
        self.premise = None
        self.bad_t, self.bad_n = None, 0
        self.refusals = Counter()

    def allowed(self, f):
        side, r = self.p["side"], self.research
        end = (f["t"] + 1) * 1000
        n = sum(count for t, count in self.trades if end - 10000 <= t < end)
        if n < r["min_trades_10s"]:
            return "sparse_trades"
        if r["regime"] == "structure" and (f.get("side_hint_15m") != side or f.get("side_hint_1h") != side):
            return "structure_unconfirmed"
        if r["regime"].startswith("recorded") and self.phase_side != side:
            return "recorded_phase_unavailable_or_against"
        return None

    def step(self, f, sigs, pos, working=None):
        qty, _ = pos_stats(pos)
        why = self.allowed(f)
        gated = dict(pos, pause=bool(pos.get("pause") or why))
        if why and sigs:
            self.refusals[why] += 1
        previous = self.arm
        desired = super().step(f, sigs, gated, working)
        if self.short_exit and self.arm and previous is None:
            if self.research["policy"] == "scalp":
                self.arm = (self.now_ms / 1000 + self.p["buy_ttl_s"], *self.arm[1:])
            s = 1 if self.p["side"] == "long" else -1
            extreme = f.get("dip_low" if s > 0 else "pop_high")
            self.premise = round_price(self.price_ladder, extreme - s * self.research["invalidation_ticks"] * tick_at(self.price_ladder, extreme), up=s < 0) if extreme else None
            if self.premise is None:
                self.arm, desired["buy"] = None, None
                self.refusals["missing_signal_extreme"] += 1
        if not self.short_exit or not qty:
            return desired
        # The reference money stop is preserved. All discretionary exits below
        # replace reference trims; none requires recovering the entry fee/cost.
        desired["trim"] = None
        s = 1 if self.p["side"] == "long" else -1
        reason = None
        if self.first_fill_ms is not None and self.now_ms >= self.first_fill_ms + 1000 * self.research["max_hold_s"]:
            reason = "time"
        current = self.quote_fresh and 0 <= self.now_ms / 1000 - f["t"] <= 2
        broken = current and self.premise is not None and s * ((f["bid"] if s > 0 else f["ask"]) - self.premise) < 0
        adverse = why != "sparse_trades" and f.get("bs10") is not None and s * (f["bs10"] - .5) < 0
        if f["t"] != self.bad_t:
            consecutive = self.bad_t is not None and f["t"] == self.bad_t + 1
            self.bad_n = (self.bad_n + 1 if consecutive else 1) if broken and adverse else 0
            self.bad_t = f["t"]
        if self.bad_n >= self.research["invalidation_confirm_s"]:
            reason = "premise"
        opposite = "POP_STALLING" if s > 0 else "DIP_SLOWING"
        if any(x.get("sig") == opposite and x.get("src") == "v" and not x.get("shadow") for x in sigs):
            reason = reason or "opposite_stall"
        if reason:
            desired.update(buy=None, exit_reason=reason)
            self.arm = None
        return desired

    def on_execution(self, t, role, price, remaining):
        if role == "buy" and self.first_fill_ms is None:
            self.first_fill_ms = t
        if not remaining:
            self.first_fill_ms, self.bad_t, self.bad_n = None, None, 0
            # Keep the frozen signal premise until outstanding cancels settle.


class ResearchEngine(Engine):
    def __init__(self, config, **kwargs):
        if config.data["version"] != 2:
            raise ValueError("research engine requires schema 2")
        super().__init__(config, **kwargs)
        self.research = self.cfg["research"]
        self.spot = self.research["market"] == "spot"
        self.trade_times = {s: deque() for s in self.features}
        self.phase_schedule = None
        for b in self.books.values():
            b.strategy = ResearchStrategy(b.strategy.p, self.cfg["signal"], self.research, b.contract["price_ladder"])
            b.strategy.trades = self.trade_times[b.symbol]

    def rates(self, t):
        return next((m, k) for start, m, k in reversed(self.research["fee_schedule"]) if t >= start)

    def mtm(self):
        # Every virtual position is born from a reserved order. Empty pools have
        # no inventory; this avoids scanning all idle books for every WS packet.
        return super().mtm() if self.reservations else 0.0

    def fresh(self, symbol):
        if not super().fresh(symbol):
            return False
        m = self.markets[symbol]
        return self.now - m.get("exchange_quote_t", m["quote_t"]) <= self.execution["quote_max_age_ms"]

    def available_cash(self):
        inventory_cost = sum(b.qty * (b.pos["avg"] or 0) for b in self.books.values()) if self.spot else 0
        committed = sum((w["qty"] - w["filled"]) * w["px"] * (1 + w.get("fee_rates", self.rates(self.now))[0])
                        for b in self.books.values() if (w := b.work["buy"])) if self.spot else 0
        return self.cash - inventory_cost - committed

    def new_order(self, b, role, want):
        px, qty = want[:2]
        px = round_price(b.contract["price_ladder"], px, up=(role == "trim") == (b.s > 0))
        if role == "buy" and self.spot:
            qty = min(qty, floor_qty(max(0, self.available_cash()) / (px * (1 + self.rates(self.now)[0])), b.contract["qstep"]))
        qty = floor_qty(min(qty, b.qty) if role == "trim" else qty, b.contract["qstep"])
        if role == "buy":
            qty = min(qty, floor_qty(max(0, b.strategy.p["unit_qty"] - b.qty), b.contract["qstep"]),
                      floor_qty(max(0, b.strategy.p["max_notional"] / px - b.qty), b.contract["qstep"]))
        check_px = px
        if len(want) > 2 and want[2] == "taker" and self.markets.get(b.symbol, {}).get("bids"):
            check_px = self.markets[b.symbol]["bids" if b.s > 0 else "asks"][0][0]
        if qty * check_px < b.contract["min_order"] - 1e-8:
            self.counts["minimum_order_refusals"] += 1
            if role == "buy":
                b.strategy.arm = None
                self.release(b)
            elif not b.pos.get("dust_reported"):
                self.emit("DUST", book=b.key, qty=b.qty, value=qty * check_px)
                b.pos["dust_reported"] = True
            return
        super().new_order(b, role, (px, qty, *want[2:]))
        if role == "buy" and b.work[role]:
            self.emit("ENTRY_PREMISE", book=b.key, oid=b.work[role]["oid"], premise=b.strategy.premise,
                      expires_ms=int(b.strategy.arm[0] * 1000) if b.strategy.arm else None)

    def fill(self, b, role, qty, px, maker, order):
        race = role == "buy" and not b.qty and order.get("cancel_at") is not None and b.strategy.arm is None
        if self.spot and role == "buy":
            # Existing order reservation is ours; do not subtract it twice.
            inventory = sum(x.qty * (x.pos["avg"] or 0) for x in self.books.values())
            rate = order.get("fee_rates", self.rates(self.now))[0 if maker else 1]
            qty = min(qty, floor_qty(max(0, self.cash - inventory) / (px * (1 + rate)), b.contract["qstep"]))
        super().fill(b, role, qty, px, maker, order)
        if race and b.qty:
            self.stop(b, "entry_cancel_race")

    def advance(self, target):
        """Advance order/holding timers even if this symbol sends no new message.

        No synthetic quote, trade, signal or fill is created. An exit waits for
        a real fresh book after its request latency. Gaps do not stop the clock.
        """
        while True:
            due = []
            for key in self.reservations:
                b = self.books[key]
                if b.strategy.arm:
                    due.append(int(b.strategy.arm[0] * 1000))
                if b.strategy.short_exit and b.qty and not b.stopping and b.strategy.first_fill_ms is not None:
                    due.append(b.strategy.first_fill_ms + self.research["max_hold_s"] * 1000)
                due.extend(w["cancel_at"] for w in b.work.values() if w and "cancel_at" in w)
            if not due or min(due) > target:
                break
            self.clock(max(self.now, min(due)))
            for key in list(self.reservations):
                b = self.books[key]
                for role, w in list(b.work.items()):
                    if w and w.get("cancel_at", math.inf) <= self.now:
                        b.work[role] = None
                        self.emit("CANCEL", book=b.key, role=role, oid=w["oid"])
                if b.strategy.arm and b.strategy.arm[0] * 1000 <= self.now:
                    b.strategy.arm = None
                    if b.work["buy"]:
                        b.work["buy"].setdefault("cancel_at", self.now + self.execution["latency_ms"])
                    self.emit("ENTRY_EXPIRED", book=b.key)
                if b.strategy.short_exit and b.qty and not b.stopping and b.strategy.first_fill_ms is not None and self.now >= b.strategy.first_fill_ms + self.research["max_hold_s"] * 1000:
                    self.stop(b, "time")
                if b.stopping:
                    self.stop(b)
                self.release(b)
        self.clock(target)

    def funding(self, row):
        if self.spot:
            raise ValueError("spot accounts cannot receive perpetual funding")
        self.advance(row["t"])
        super().funding(row)

    def execute(self, b, event):
        if not self.spot:
            return super().execute(b, event)
        # Domestic strict FIFO hypothesis: cancellations alone never fill us.
        for role, order in list(b.work.items()):
            if not order:
                continue
            if order.get("cancel_at", math.inf) <= self.now:
                b.work[role] = None
                self.emit("CANCEL", book=b.key, role=role, oid=order["oid"])
                continue
            if self.now < order["arrival"] or not self.fresh(b.symbol):
                continue
            buy = role == "buy"
            m = self.markets[b.symbol]
            if order["kind"] == "taker":
                if event.channel == "books15":
                    self.market_fill(b, role, order)
                continue
            if not order["active"]:
                if event.channel != "books15":
                    continue
                opposite = m["asks" if buy else "bids"][0][0]
                if (order["px"] >= opposite if buy else order["px"] <= opposite):
                    b.work[role] = None
                    self.emit("POST_ONLY_CANCEL", book=b.key, role=role)
                    continue
                queue = next((q for p, q in m["bids" if buy else "asks"] if p == order["px"]), 0)
                order.update(active=True, queue=queue)
                continue
            if event.channel != "trade":
                continue
            for trade in event.message["data"]:
                price, size = float(trade["price"]), float(trade["size"])
                if trade["side"] != ("sell" if buy else "buy") or (price > order["px"] if buy else price < order["px"]):
                    continue
                through = price < order["px"] if buy else price > order["px"]
                ahead = 0 if through else order["queue"]
                order["queue"] = max(0, ahead - size)
                volume = min(max(0, size - ahead), size * self.execution["max_participation"])
                self.fill(b, role, volume, order["px"], True, order)
                if b.work[role] is not order:
                    break

    def process(self, event, feature_update=None):
        self.advance(event.t)
        sym = event.symbol
        if event.channel == "trade":
            times = self.trade_times[sym]
            times.append((event.t, len(event.message["data"])))
            while times and times[0][0] < event.t - 12000:
                times.popleft()
        if event.channel == "books15":
            m = self.markets.setdefault(sym, dict(bids=[], asks=[], quote_t=-math.inf, mark_t=-math.inf, mark=None, mid=0))
            m["exchange_quote_t"] = int(event.message["data"][0].get("ts") or event.t)
            mid = (float(event.message["data"][0]["bids"][0][0]) + float(event.message["data"][0]["asks"][0][0])) / 2
            for side in self.cfg["books"][sym]["sides"]:
                b = self.books[f"{sym}:{side}"]
                b.contract["tick"] = b.strategy.p["tick"] = tick_at(b.contract["price_ladder"], mid)
        for side in self.cfg["books"][sym]["sides"]:
            policy = self.books[f"{sym}:{side}"].strategy
            policy.now_ms = event.t
            policy.quote_fresh = (event.t - int(event.message["data"][0].get("ts") or event.t) <= self.execution["quote_max_age_ms"]) if event.channel == "books15" else self.fresh(sym)
            policy.p["fee_rt_pct"] = 100 * sum(self.rates(event.t))
            if self.phase_schedule:
                policy.phase_side = self.phase_schedule.side(sym, event.t, self.research["regime"] == "recorded_det", self.research["phase_max_age_s"])
        signals = super().process(event, feature_update)
        # Spot has no separate mark-price channel; quote stops are intentional.
        if self.spot:
            self.quality.pop("stale_mark_while_positioned", None)
        return signals

    def result(self):
        out = super().result()
        complete = True
        for row in out["unfinished"]:
            b = self.books[row["book"]]
            book = self.markets[b.symbol]
            exit_px = vwap(book["bids" if b.s > 0 else "asks"], b.qty)
            if exit_px is None or not self.fresh(b.symbol) or exit_px * b.qty < b.contract["min_order"]:
                row["liquidation_estimate"] = None
                row["liquidation_priced"] = False
                complete = False
            else:
                exit_px *= 1 - b.s * self.execution["slip_bps"] / 10000
                row["liquidation_estimate"] = b.s * (exit_px - b.pos["avg"]) * b.qty - exit_px * b.qty * self.rates(self.now)[1]
                row["liquidation_priced"] = True
        out["liquidation_depth_complete"] = complete
        out["liquidated_estimate_net"] = out["realized_net"] + sum(row["liquidation_estimate"] for row in out["unfinished"]) if complete else None
        out.update(research=self.research, available_cash=self.available_cash(), funding_applicable=not self.spot,
                   policy_refusals={key: dict(b.strategy.refusals) for key, b in self.books.items()})
        return out
