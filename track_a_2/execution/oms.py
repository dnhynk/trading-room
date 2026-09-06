"""Durable multi-book spot OMS for Track A-2.

Intent is committed before transmission. Cumulative exchange fills are applied
once. A cancel acknowledgement never releases cash or inventory until order
detail is reconciled. Unknown submissions retain ownership of their slot.
"""
from decimal import Decimal as D
import datetime as dt
import time
import uuid

from track_c.execution.coinone import CoinoneError, EntryExpired, decimal


TERMINAL = {
    "FILLED",
    "CANCELED",
    "NOT_TRIGGERED_CANCELED",
    "CANCELED_NO_ORDER",
    "CANCELED_LIMIT_PRICE_EXCEED",
    "CANCELED_UNDER_PRODUCT_UNIT",
    "REJECTED",
}
LIVE = {
    "LIVE",
    "PARTIALLY_FILLED",
    "PARTIALLY_CANCELED",
    "NOT_TRIGGERED",
    "NOT_TRIGGERED_PARTIALLY_CANCELED",
    "TRIGGERED",
}
REJECTED = {
    4, 8, 10, 11, 12, 20, 21, 22, 23, 24, 25, 27, 40, 50, 51, 52, 53, 54,
    101, 103, 105, 107, 108, 109, 111, 120, 121, 122, 123, 130, 131, 132, 133,
    151, 161, 162, 163, 164, 165, 166, 167, 300, 305, 306, 307, 308, 309, 310,
    313, 314, 315, 316, 317,
}


def new_book():
    return dict(
        lots=[],
        avg=None,
        last=None,
        last_buy_px=None,
        last_trim_px=None,
        cooldown_until=0,
        selected=False,
        wind_down=False,
        strategy_params=None,
        desired_stop=None,
        stop_limit=None,
        exit_reason=None,
        mark=None,
        mark_at=0,
        rejection_until=0,
        campaign_open=False,
        campaign_realized="0",
        campaign_budget=None,
        campaign_full_budget=None,
        campaign_entry_cid=None,
        campaign_started_at=None,
        campaign_stop_counted=False,
    )


class OMS:
    def __init__(self, config, client, store, *, clock=time.time):
        self.config, self.client, self.store, self.clock = config, client, store, clock
        self.state = store.load() or dict(
            version=1,
            day=None,
            day_start="0",
            day_external_flows="0",
            day_realized="0",
            day_stops=0,
            realized="0",
            cash_krw="0",
            initial_equity="0",
            external_flows="0",
            capital_initialized=False,
            account_at=0,
            balances={},
            books={},
            orders={},
            selected=[],
            selection_reasons={},
            halt=None,
            mismatches={},
        )
        if self.state.get("version") != 1:
            raise RuntimeError("unsupported Track A-2 ledger version")
        required = {"books", "orders", "cash_krw", "capital_initialized", "halt"}
        if not required <= set(self.state):
            raise RuntimeError("incomplete Track A-2 ledger")
        for order in self.active():
            if order["coin"] not in self.state["books"]:
                raise RuntimeError("orphan Track A-2 order")
        for order in self.state["orders"].values():
            if order.get("status") in TERMINAL and order.get("terminal_at") is None:
                # Older ledgers did not timestamp terminal settlement. Give
                # them one conservative correction window after this upgrade.
                order["terminal_at"] = self.clock()
        defaults = new_book()
        self.state.setdefault("day_stops", 0)
        self.state.setdefault("day_external_flows", "0")
        for book in self.state["books"].values():
            for key, value in defaults.items():
                book.setdefault(key, [] if key == "lots" else value)
        missing_risk = [
            coin for coin, book in self.state["books"].items()
            if book.get("lots") and book.get("campaign_budget") is None
        ]
        if missing_risk and not self.state["halt"]:
            self.state["halt"] = "CAMPAIGN_RISK_STATE_MISSING"
            self.save("HALT", reason=self.state["halt"], coins=sorted(missing_risk))
        self.roll_day()

    def save(self, kind, **fields):
        self.store.save(self.state, kind, **fields)

    def book(self, coin):
        if coin not in self.state["books"]:
            self.state["books"][coin] = new_book()
        return self.state["books"][coin]

    def quantity(self, coin):
        return sum((D(lot[0]) for lot in self.book(coin)["lots"]), D(0))

    def cost(self, coin):
        qty = self.quantity(coin)
        average = self.book(coin).get("avg")
        if qty and average is None:
            raise RuntimeError("Track A-2 inventory has no average cost")
        return qty * D(average) if qty else D(0)

    def position(self, coin, *, paused=False):
        book = self.book(coin)
        return dict(
            lots=[[float(qty), float(price), oid] for qty, price, oid in book["lots"]],
            avg=float(book["avg"]) if book["avg"] is not None else None,
            last=book["last"],
            last_buy_px=float(book["last_buy_px"]) if book["last_buy_px"] is not None else None,
            last_trim_px=float(book["last_trim_px"]) if book["last_trim_px"] is not None else None,
            halt=self.state["halt"],
            pause=bool(paused or book["wind_down"]),
            cooldown_until=book["cooldown_until"],
            avail=float(self.free_cash()),
            lever=None,
        )

    def active(self, coin=None, role=None):
        return [
            order for order in self.state["orders"].values()
            if order["status"] not in TERMINAL
            and (coin is None or order["coin"] == coin)
            and (role is None or order["role"] == role)
        ]

    def remaining(self, order):
        return max(D(0), D(order["qty"]) - D(order["filled"]))

    def reserved_cash(self):
        return sum(
            (self.remaining(order) * D(order["price"]) * (D(1) + D(order["fee_rate"])) for order in self.active(role="buy")),
            D(0),
        )

    def free_cash(self):
        account = D(self.state["balances"].get("KRW", {}).get("available", "0"))
        ledger = max(D(0), D(self.state["cash_krw"]) - self.reserved_cash())
        return min(account, ledger) if self.state["account_at"] else D(0)

    def mark(self, coin, price):
        price = decimal(price, positive=True)
        book = self.book(coin)
        book["mark"], book["mark_at"] = str(price), self.clock()

    def equity(self, marks=None):
        marks = marks or {}
        value = D(self.state["cash_krw"])
        for coin, book in self.state["books"].items():
            qty = self.quantity(coin)
            if not qty:
                continue
            mark = marks.get(coin) or book.get("mark")
            if mark is None:
                mark = book["avg"]
            value += qty * D(str(mark))
        return max(D(0), value)

    def portfolio_notional(self, marks=None, *, pending=True):
        marks = marks or {}
        total = D(0)
        for coin, book in self.state["books"].items():
            qty = self.quantity(coin)
            mark = marks.get(coin) or book.get("mark") or book.get("avg")
            if qty and mark is not None:
                total += qty * D(str(mark))
        if pending:
            total += sum((self.remaining(order) * D(order["price"]) for order in self.active(role="buy")), D(0))
        return total

    def unrealized(self, marks=None):
        marks = marks or {}
        total = D(0)
        for coin, book in self.state["books"].items():
            qty = self.quantity(coin)
            mark = marks.get(coin) or book.get("mark")
            if qty and mark is not None:
                total += qty * D(str(mark)) - self.cost(coin)
        return total

    def roll_day(self, marks=None):
        today = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).date().isoformat()
        if self.state["day"] != today:
            self.state.update(
                day=today,
                day_start=str(self.equity(marks)),
                day_external_flows="0",
                day_realized="0",
                day_stops=0,
            )
            self.save("DAY", day=today)

    def day_pnl(self, marks=None):
        return (
            self.equity(marks)
            - D(self.state["day_start"])
            - D(self.state.get("day_external_flows", "0"))
        )

    def daily_blocked(self, marks=None):
        self.roll_day(marks)
        capital = max(
            D(0),
            D(self.state["day_start"]) + D(self.state.get("day_external_flows", "0")),
        )
        return not capital or self.day_pnl(marks) <= -(
            capital * decimal(self.config["daily_loss_fraction"])
        )

    def halt(self, reason, **fields):
        if self.state["halt"] != reason:
            self.state["halt"] = reason
            self.save("HALT", reason=reason, **fields)

    def set_selection(self, plan, reasons):
        selected, wind = set(plan["selected"]), set(plan["wind_down"])
        changed = self.state["selected"] != plan["selected"] or self.state["selection_reasons"] != reasons
        self.state["selected"] = list(plan["selected"])
        self.state["selection_reasons"] = dict(reasons)
        for coin in set(self.state["books"]) | selected | wind:
            book = self.book(coin)
            book["selected"] = coin in selected
            book["wind_down"] = coin in wind
        if changed:
            self.save("SELECTION", selected=plan["selected"], wind_down=plan["wind_down"], reasons=reasons)

    def set_strategy_params(self, coin, params):
        book = self.book(coin)
        if self.quantity(coin) and book["strategy_params"] and book["strategy_params"] != params:
            raise RuntimeError("positioned Track A-2 book cannot be resized")
        if book["strategy_params"] != params:
            book["strategy_params"] = params
            self.save("SIZING", coin=coin, params=params)

    def _refresh_book(self, coin):
        book = self.book(coin)
        qty = self.quantity(coin)
        if not qty:
            book["lots"] = []
            book["last"] = None
            book["last_buy_px"] = None
            book["last_trim_px"] = None
            book["desired_stop"] = None
            book["stop_limit"] = None
            book["exit_reason"] = None

    def _add_lot(self, coin, qty, price, oid):
        book = self.book(coin)
        book_qty = self.quantity(coin)
        old_average = D(book["avg"]) if book.get("avg") is not None else D(0)
        if not book["campaign_open"]:
            params = book.get("strategy_params") or {}
            full_budget = D(str(params.get("campaign_loss_budget_krw") or 0))
            if full_budget <= 0:
                full_budget = self.equity() * decimal(self.config["book_risk_fraction"])
            unit = D(str(params.get("unit_qty") or qty))
            scale = min(D(1), qty / max(unit, qty))
            book.update(
                campaign_realized="0",
                campaign_budget=str(full_budget * scale),
                campaign_full_budget=str(full_budget),
                campaign_entry_cid=oid,
                campaign_started_at=self.clock(),
                campaign_stop_counted=False,
            )
        elif book.get("campaign_entry_cid") == oid:
            # The first resting order can fill more than once while its
            # remainder is being canceled. Risk scales only with the amount
            # that actually arrived, never with an unfilled intention.
            full_budget = D(book.get("campaign_full_budget") or book["campaign_budget"])
            params = book.get("strategy_params") or {}
            unit = D(str(params.get("unit_qty") or qty))
            increment = full_budget * min(D(1), qty / max(unit, qty))
            book["campaign_budget"] = str(min(full_budget, D(book["campaign_budget"]) + increment))
        book["campaign_open"] = True
        for lot in book["lots"]:
            if lot[2] == oid:
                lot_qty, old_price = D(lot[0]), D(lot[1])
                lot[0] = str(lot_qty + qty)
                lot[1] = str((lot_qty * old_price + qty * price) / (lot_qty + qty))
                break
        else:
            book["lots"].append([str(qty), str(price), oid])
        book["avg"] = str((book_qty * old_average + qty * price) / (book_qty + qty))
        book["last"], book["last_buy_px"] = "buy", str(price)
        self._refresh_book(coin)

    def _adjust_buy_gross(self, order, qty, gross_delta):
        """Allocate a late cumulative buy-value correction without losing cash.

        The part belonging to inventory still held increases its cost basis; a
        part whose order lot has already been sold corrects realized P&L.
        """
        if not gross_delta:
            return D(0)
        if not qty:
            raise CoinoneError("buy value correction has no filled quantity")
        coin = order["coin"]
        book = self.book(coin)
        remaining_order = D(0)
        unit_delta = gross_delta / qty
        for lot in book["lots"]:
            if lot[2] == order["cid"]:
                remaining_order = D(lot[0])
                lot[1] = str(D(lot[1]) + unit_delta)
                break
        inventory_delta = unit_delta * remaining_order
        book_qty = self.quantity(coin)
        if inventory_delta and book_qty:
            book["avg"] = str((self.cost(coin) + inventory_delta) / book_qty)
        return -(gross_delta - inventory_delta)

    def _record_pnl(self, coin, pnl):
        self.state["realized"] = str(D(self.state["realized"]) + pnl)
        self.state["day_realized"] = str(D(self.state["day_realized"]) + pnl)
        book = self.book(coin)
        if book["campaign_open"]:
            book["campaign_realized"] = str(D(book["campaign_realized"]) + pnl)

    @staticmethod
    def _risk_exit(order, reason):
        if order["role"] in ("protect", "exit"):
            return True
        return order["role"] == "trim" and order.get("purpose") == "risk" and str(
            reason or ""
        ).startswith(
            ("strategy_risk", "strategy_stop", "protection_", "protect_")
        )

    def campaign_stop_floor(self, coin, exit_fee_rate=0):
        book = self.book(coin)
        qty = self.quantity(coin)
        budget = book.get("campaign_budget")
        if not qty or budget is None or book.get("avg") is None:
            return None
        fee = decimal(exit_fee_rate)
        if fee >= 1:
            raise ValueError("invalid exit fee rate")
        numerator = (
            qty * D(book["avg"])
            - D(budget)
            - D(book.get("campaign_realized", "0"))
        )
        return max(D(0), numerator / (qty * (D(1) - fee)))

    def projected_campaign_stop(self, coin, qty, price, buy_fee_rate, exit_fee_rate):
        qty, price = decimal(qty, positive=True), decimal(price, positive=True)
        buy_fee_rate, exit_fee_rate = decimal(buy_fee_rate), decimal(exit_fee_rate)
        book = self.book(coin)
        held = self.quantity(coin)
        if held:
            budget = D(book["campaign_budget"])
            realized = D(book.get("campaign_realized", "0"))
        else:
            params = book.get("strategy_params") or {}
            full_budget = D(str(params.get("campaign_loss_budget_krw") or 0))
            if full_budget <= 0:
                full_budget = self.equity() * decimal(self.config["book_risk_fraction"])
            unit = D(str(params.get("unit_qty") or qty))
            budget = full_budget * min(D(1), qty / max(unit, qty))
            realized = D(0)
        projected_qty = held + qty
        projected_avg = (self.cost(coin) + qty * price) / projected_qty
        projected_realized = realized - qty * price * buy_fee_rate
        numerator = projected_qty * projected_avg - budget - projected_realized
        return max(D(0), numerator / (projected_qty * (D(1) - exit_fee_rate)))

    def _remove_lots(self, coin, qty, lot_index=None):
        book = self.book(coin)
        basis = qty * D(book["avg"])
        remaining = qty
        if lot_index is not None and remaining:
            index = int(lot_index)
            if not 0 <= index < len(book["lots"]):
                raise CoinoneError("sale lot identity unavailable")
            lot = book["lots"][index]
            take = min(remaining, D(lot[0]))
            remaining -= take
            lot[0] = str(D(lot[0]) - take)
            if not D(lot[0]):
                book["lots"].pop(index)
        while remaining and book["lots"]:
            lot = book["lots"][-1]
            take = min(remaining, D(lot[0]))
            remaining -= take
            lot[0] = str(D(lot[0]) - take)
            if not D(lot[0]):
                book["lots"].pop()
        if remaining:
            raise CoinoneError("execution exceeds Track A-2 inventory")
        self._refresh_book(coin)
        return basis

    def submit(self, coin, role, side, kind, qty, *, fee_rate, before_send=None, **fields):
        active = self.active(coin)
        if self.active(coin, role) or any(order["side"] == side for order in active):
            raise RuntimeError("duplicate Track A-2 order side or role")
        cid = "ta2-" + role + "-" + uuid.uuid4().hex
        order = dict(
            cid=cid,
            coin=coin,
            role=role,
            side=side,
            type=kind,
            qty=str(decimal(qty, positive=True)),
            fee_rate=str(decimal(fee_rate)),
            status="INTENT",
            filled="0",
            gross="0",
            fee="0",
            created=self.clock(),
            **{key: str(value) if isinstance(value, D) else value for key, value in fields.items()},
        )
        self.state["orders"][cid] = order
        self.save("ORDER_INTENT", order=order)
        try:
            result = self.client.submit(order, before_send=before_send)
            order["exchange_id"] = result.get("order_id")
            order["status"] = "SUBMITTED"
            self.save("ORDER_SUBMITTED", cid=cid, coin=coin, role=role, exchange_id=order["exchange_id"])
        except EntryExpired as exc:
            order.update(status="REJECTED", not_sent=True, expiry_reason=str(exc))
            self.save(
                "ORDER_REJECTED", cid=cid, coin=coin, role=role,
                transmitted=False, error=str(exc),
            )
        except CoinoneError as exc:
            order["status"] = "REJECTED" if exc.code in REJECTED else "UNKNOWN"
            self.save("ORDER_REJECTED" if order["status"] == "REJECTED" else "ORDER_UNCERTAIN", cid=cid, coin=coin, role=role, error=str(exc))
            self.book(coin)["rejection_until"] = self.clock() + 1
            if role in ("protect", "exit", "trim") and order["status"] == "REJECTED":
                self.book(coin)["exit_reason"] = role + "_rejected"
        return order

    def apply(self, order, row):
        if (
            row.get("quote_currency") != "KRW"
            or row.get("target_currency") != order["coin"]
            or row.get("side") != order["side"]
            or (row.get("user_order_id") and row["user_order_id"] != order["cid"])
        ):
            raise CoinoneError("order identity mismatch")
        status = row.get("status")
        if status not in TERMINAL | LIVE:
            raise CoinoneError("unknown order status")
        qty = decimal(row.get("executed_qty") or 0)
        fee = decimal(row.get("fee") or 0)
        reported_rate = decimal(row.get("fee_rate") or 0)
        gross = qty * decimal(row.get("average_executed_price") or 0)
        if qty and not gross:
            raise CoinoneError("filled order has no execution price")
        if status in TERMINAL and row.get("remain_qty") is not None and decimal(row["remain_qty"]) > 0:
            raise CoinoneError("terminal order still reports a remainder")
        if qty > D(order["qty"]) or qty < D(order["filled"]) or gross < D(order["gross"]) or fee < D(order["fee"]):
            raise CoinoneError("nonmonotonic cumulative execution")
        allowance = gross * (D(order["fee_rate"]) + D("0.000001")) + D(1)
        fee_mismatch = (
            reported_rate > D(order["fee_rate"]) + D("0.000001")
            or fee > allowance
        )
        dq, dg, df = qty - D(order["filled"]), gross - D(order["gross"]), fee - D(order["fee"])
        fill = None
        if dq or dg or df:
            price = dg / dq if dq else None
            cash = D(self.state["cash_krw"]) + (dg if order["side"] == "SELL" else -dg) - df
            exit_reason = self.book(order["coin"]).get("exit_reason")
            if order["side"] == "BUY" and dq:
                self._add_lot(order["coin"], dq, price, order["cid"])
                pnl = -df
            elif order["side"] == "BUY":
                pnl = self._adjust_buy_gross(order, qty, dg) - df
            elif dq:
                basis = self._remove_lots(order["coin"], dq, order.get("lot"))
                pnl = dg - basis - df
                book = self.book(order["coin"])
                if self.quantity(order["coin"]):
                    book["last"], book["last_trim_px"] = "trim", str(price)
                if order["role"] == "protect":
                    book["exit_reason"] = "exchange_stop"
                elif order["role"] == "exit":
                    book["exit_reason"] = order.get("reason") or exit_reason or "engine_exit"
                elif order.get("purpose") == "risk" and not self.quantity(order["coin"]):
                    book["exit_reason"] = order.get("reason") or "strategy_risk_trim"
            else:
                pnl = dg - df
            self.state["cash_krw"] = str(cash)
            self._record_pnl(order["coin"], pnl)
            book = self.book(order["coin"])
            reason = order.get("reason") or exit_reason or book.get("exit_reason")
            if dq and self._risk_exit(order, reason) and not book["campaign_stop_counted"]:
                book["campaign_stop_counted"] = True
                self.state["day_stops"] += 1
            fill = dict(
                coin=order["coin"], role=order["role"], side=order["side"],
                qty=str(dq), price=str(price) if price is not None else None,
                gross_delta=str(dg), fee=str(df), pnl=str(pnl), cid=order["cid"],
                accounting="fill" if dq else "correction",
                reason=reason,
            )
        updates = dict(
            filled=str(qty),
            gross=str(gross),
            fee=str(fee),
            status=status,
            exchange_id=row.get("order_id") or order.get("exchange_id"),
        )
        if status in TERMINAL and order.get("status") not in TERMINAL:
            updates["terminal_at"] = self.clock()
        changed = any(order.get(key) != value for key, value in updates.items())
        order.update(updates)
        if fill or changed:
            self.save("FILL" if fill else "ORDER_STATUS", **(fill or dict(coin=order["coin"], cid=order["cid"], status=status)))
        if fee_mismatch:
            self.halt(
                "FEE_MISMATCH", coin=order["coin"], cid=order["cid"],
                expected_rate=order["fee_rate"], reported_rate=str(reported_rate),
                reported_fee=str(fee),
            )
        elif fill and D(self.state["cash_krw"]) < 0:
            self.halt("CASH_ACCOUNTING", coin=order["coin"], cid=order["cid"])
        return fill

    def finish_flat(self, coin):
        book = self.book(coin)
        if self.quantity(coin) or self.active(coin) or not book["campaign_open"]:
            return False
        reason = book.get("exit_reason")
        if reason and reason not in ("strategy_trim", "take_profit"):
            book["cooldown_until"] = self.clock() + float(self.config["strategy"]["stop_cooldown_s"])
        campaign = dict(
            realized=book.get("campaign_realized"),
            budget=book.get("campaign_budget"),
            started_at=book.get("campaign_started_at"),
        )
        book.update(
            campaign_open=False,
            campaign_realized="0",
            campaign_budget=None,
            campaign_full_budget=None,
            campaign_entry_cid=None,
            campaign_started_at=None,
            campaign_stop_counted=False,
            strategy_params=None,
            desired_stop=None,
            stop_limit=None,
            exit_reason=None,
        )
        self.save(
            "FLAT", coin=coin, reason=reason,
            cooldown_until=book["cooldown_until"], campaign=campaign,
        )
        return True

    def reconcile(self, *, force=False):
        fills = []
        candidates = list(self.active())
        active_ids = {order["cid"] for order in candidates}
        candidates.extend(
            order for order in self.state["orders"].values()
            if order["cid"] not in active_ids
            and order.get("terminal_at") is not None
            and 0 <= self.clock() - order["terminal_at"] <= self.config["settlement_reconcile_s"]
        )
        for order in candidates:
            if not force and 0 <= self.clock() - order.get("checked", -1e9) < self.config["reconcile_poll_s"]:
                continue
            try:
                fill = self.apply(order, self.client.detail(order["coin"], order["cid"]))
                if fill:
                    fills.append(fill)
                order["checked"] = self.clock()
            except CoinoneError as exc:
                self.store.event("RECONCILE_PENDING", coin=order["coin"], cid=order["cid"], error=str(exc))
                if self.clock() - order["created"] > self.config["reconcile_halt_s"]:
                    self.halt("ORDER_RECONCILIATION", coin=order["coin"], cid=order["cid"])
        expired = [
            cid for cid, order in self.state["orders"].items()
            if order["status"] in TERMINAL
            and order.get("terminal_at") is not None
            and self.clock() - order["terminal_at"] > self.config["settlement_reconcile_s"]
        ]
        if expired:
            for cid in expired:
                del self.state["orders"][cid]
            self.save("ORDER_ARCHIVE", count=len(expired), cids=sorted(expired))
        return fills

    def cancel(self, order):
        fills = []
        if order["status"] in TERMINAL:
            return fills
        if not order.get("cancel_requested"):
            order["cancel_requested"] = self.clock()
            self.save("CANCEL_INTENT", coin=order["coin"], cid=order["cid"], role=order["role"])
        attempts = int(order.get("cancel_attempts", 0))
        due = (
            order.get("cancel_last_attempt") is None
            or self.clock() - order["cancel_last_attempt"] >= self.config["cancel_retry_s"]
        )
        if due and attempts < self.config["cancel_max_attempts"]:
            order["cancel_attempts"] = attempts + 1
            order["cancel_last_attempt"] = self.clock()
            self.save(
                "CANCEL_ATTEMPT", coin=order["coin"], cid=order["cid"],
                role=order["role"], attempt=order["cancel_attempts"],
            )
            try:
                self.client.cancel(order["coin"], order["cid"])
            except CoinoneError as exc:
                self.store.event("CANCEL_PENDING", coin=order["coin"], cid=order["cid"], error=str(exc))
        try:
            fill = self.apply(order, self.client.detail(order["coin"], order["cid"]))
            if fill:
                fills.append(fill)
        except CoinoneError as exc:
            self.store.event("CANCEL_RECONCILE_PENDING", coin=order["coin"], cid=order["cid"], error=str(exc))
        if (
            order["status"] not in TERMINAL
            and int(order.get("cancel_attempts", 0)) >= self.config["cancel_max_attempts"]
        ):
            self.halt("CANCEL_RECONCILIATION", coin=order["coin"], cid=order["cid"])
        return fills

    def _mismatch(self, key, reason, **fields):
        started = self.state["mismatches"].setdefault(key, self.clock())
        if self.clock() - started >= self.config["account_mismatch_grace_s"]:
            self.halt(reason, **fields)

    def sync_account(self, balances, exchange_orders, qty_steps):
        self.roll_day()
        rows = {}
        for row in balances:
            coin = row.get("currency")
            if not coin or coin in rows:
                self.halt("AMBIGUOUS_BALANCE", currency=coin)
                return
            rows[coin] = dict(available=str(decimal(row["available"])), limit=str(decimal(row["limit"])))
        if "KRW" not in rows:
            self.halt("KRW_BALANCE_MISSING")
            return
        own_cids = set(self.state["orders"])
        own_exchange = {order.get("exchange_id") for order in self.state["orders"].values() if order.get("exchange_id")}
        for row in exchange_orders:
            cid, exchange_id = row.get("user_order_id"), row.get("order_id")
            if cid in own_cids or exchange_id in own_exchange:
                continue
            self.halt("UNJOURNALED_A2_ORDER" if str(cid or "").startswith("ta2-") else "FOREIGN_ORDER", coin=row.get("target_currency"))
        expected_coins = {coin for coin in self.state["books"] if self.quantity(coin)}
        for coin in (set(rows) | expected_coins) - {"KRW"}:
            row = rows.get(coin, {"available": "0", "limit": "0"})
            actual = D(row["available"]) + D(row["limit"])
            expected = self.quantity(coin)
            tolerance = D(str(qty_steps.get(coin, "0"))) / 2
            if coin not in expected_coins and actual > tolerance:
                self.halt("FOREIGN_ASSET", coin=coin, quantity=str(actual))
            elif actual + tolerance < expected:
                self._mismatch("inventory:" + coin, "INVENTORY_SHORTFALL", coin=coin, actual=str(actual), expected=str(expected))
            elif actual > expected + tolerance:
                self._mismatch("inventory:" + coin, "FOREIGN_ASSET", coin=coin, actual=str(actual), expected=str(expected))
            else:
                self.state["mismatches"].pop("inventory:" + coin, None)
        actual_cash = D(rows["KRW"]["available"]) + D(rows["KRW"]["limit"])
        exposed = bool(expected_coins or self.active())
        if not self.state["capital_initialized"]:
            if exposed:
                self.halt("CAPITAL_UNINITIALIZED_WITH_EXPOSURE")
            else:
                self.state.update(
                    capital_initialized=True, cash_krw=str(actual_cash),
                    initial_equity=str(actual_cash), day_start=str(actual_cash),
                    day_external_flows="0",
                )
                self.save("CAPITAL_INITIALIZED", balance=str(actual_cash))
        elif not exposed:
            delta = actual_cash - D(self.state["cash_krw"])
            self.state["cash_krw"] = str(actual_cash)
            self.state["external_flows"] = str(D(self.state["external_flows"]) + delta)
            self.state["day_external_flows"] = str(
                D(self.state.get("day_external_flows", "0")) + delta
            )
            self.state["mismatches"].pop("cash", None)
            if delta:
                self.save("EXTERNAL_CAPITAL", balance=str(actual_cash), external_delta=str(delta))
        elif abs(actual_cash - D(self.state["cash_krw"])) > D(1):
            self._mismatch("cash", "CASH_MISMATCH", actual=str(actual_cash), expected=self.state["cash_krw"])
        else:
            self.state["mismatches"].pop("cash", None)
        self.state["balances"] = rows
        self.state["account_at"] = self.clock()

    def can_buy(
        self, coin, qty, price, fee_rate, minimum, marks,
        *, exit_fee_rate=0, current_bid=None,
    ):
        qty, price, fee_rate, minimum = map(decimal, (qty, price, fee_rate, minimum))
        book = self.book(coin)
        if (
            self.config["mode"] != "live"
            or not self.config["execution_enabled"]
            or not self.config["portfolio_isolation_confirmed"]
            or self.state["halt"]
            or not self.state["capital_initialized"]
            or not 0 <= self.clock() - self.state["account_at"] <= self.config["account_fresh_s"]
            or not book["selected"]
            or book["wind_down"]
            or self.clock() < book["cooldown_until"]
            or self.clock() < book["rejection_until"]
            or self.daily_blocked(marks)
            or self.state["day_stops"] >= self.config["strategy"]["max_stops_day"]
            or self.active(coin, "trim")
            or self.active(coin, "exit")
        ):
            return False
        if not qty or qty * price < minimum:
            return False
        params = book.get("strategy_params") or {}
        if not params or (self.quantity(coin) + qty) * price > D(params["max_notional"]):
            return False
        open_books = sum(bool(self.quantity(name) or self.active(name, "buy")) for name in self.state["books"])
        if not self.quantity(coin) and not self.active(coin, "buy") and open_books >= self.config["max_open_books"]:
            return False
        required = qty * price * (D(1) + fee_rate)
        if required > self.free_cash():
            return False
        projected = self.portfolio_notional(marks) + qty * price
        if projected > self.equity(marks) * decimal(self.config["portfolio_notional_fraction"]):
            return False
        if current_bid is not None:
            projected_stop = self.projected_campaign_stop(
                coin, qty, price, fee_rate, exit_fee_rate,
            )
            if projected_stop >= decimal(current_bid, positive=True):
                return False
        return True

    def available_asset(self, coin):
        return D(self.state["balances"].get(coin, {}).get("available", "0"))
