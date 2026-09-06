import copy
from decimal import Decimal as D

from track_c.execution.coinone import CoinoneError


class Clock:
    def __init__(self, value=1_800_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Client:
    def __init__(self):
        self.rows = {}
        self.submissions = []
        self.cancellations = []
        self.submit_error = None
        self.accept_before_error = False
        self.balance_rows = [dict(currency="KRW", available="100000", limit="0")]

    @staticmethod
    def row(order):
        return dict(
            quote_currency="KRW",
            target_currency=order["coin"],
            user_order_id=order["cid"],
            order_id="exchange-" + order["cid"],
            side=order["side"],
            status="NOT_TRIGGERED" if order["role"] == "protect" else "LIVE",
            executed_qty="0",
            average_executed_price="0",
            fee="0",
            remain_qty=order["qty"],
        )

    def submit(self, order, *, before_send=None):
        if before_send:
            before_send()
        saved = copy.deepcopy(order)
        self.submissions.append(saved)
        if self.accept_before_error:
            self.rows[order["cid"]] = self.row(order)
        if self.submit_error:
            raise self.submit_error
        self.rows[order["cid"]] = self.row(order)
        return {"order_id": self.rows[order["cid"]]["order_id"]}

    def detail(self, coin, cid):
        if cid not in self.rows:
            raise CoinoneError("Coinone API error 104", code=104)
        return copy.deepcopy(self.rows[cid])

    def fill(self, cid, qty, price, *, fee="0", status="FILLED"):
        row = self.rows[cid]
        row.update(
            executed_qty=str(qty),
            average_executed_price=str(price),
            fee=str(fee),
            status=status,
            remain_qty="0" if status in ("FILLED", "CANCELED") else str(D(row["remain_qty"]) - D(str(qty))),
        )

    def cancel(self, coin, cid):
        self.cancellations.append(cid)
        row = self.rows[cid]
        row.update(status="CANCELED", remain_qty="0")
        return {}

    def balances(self):
        return copy.deepcopy(self.balance_rows)

    def active_orders(self):
        terminal = {"FILLED", "CANCELED", "REJECTED", "NOT_TRIGGERED_CANCELED"}
        return [copy.deepcopy(row) for row in self.rows.values() if row["status"] not in terminal]
