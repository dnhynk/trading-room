"""Deterministic Coinone execution boundary for Track A-2 replay."""
import copy
from decimal import Decimal as D

from track_a_2.execution.oms import LIVE, TERMINAL
from track_c.execution.coinone import CoinoneError, decimal


class ReplayClock:
    def __init__(self, value=0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def set_ms(self, value):
        self.value = int(value) / 1000


class SimClient:
    """Exchange-side cash, inventory, queue, latency, and cumulative details."""
    def __init__(
        self, cash, fees, *, clock, latency_ms=250, depth_fraction="0.1",
    ):
        self.cash = decimal(cash)
        self.assets = {}
        self.fees_by_coin = {
            coin: {name: decimal(value) for name, value in row.items()}
            for coin, row in fees.items()
        }
        self.clock = clock
        self.latency_ms = int(latency_ms)
        self.depth_fraction = decimal(depth_fraction, positive=True)
        self.rows = {}
        self.orders = {}
        self.meta = {}
        self.books = {}
        self.seen_trades = set()
        self.submissions = []
        self.cancellations = []

    @property
    def now_ms(self):
        return int(self.clock() * 1000)

    def set_book(self, coin, book):
        self.books[coin] = copy.deepcopy(book) if book else None

    def _fee_rate(self, order):
        fees = self.fees_by_coin[order["coin"]]
        return fees["maker"] if order["role"] == "buy" else fees["taker"]

    def submit(self, order, *, before_send=None):
        if before_send:
            before_send()
        saved = copy.deepcopy(order)
        self.submissions.append(saved)
        cid = order["cid"]
        status = "NOT_TRIGGERED" if order["type"] == "STOP_LIMIT" else "LIVE"
        row = dict(
            quote_currency="KRW",
            target_currency=order["coin"],
            user_order_id=cid,
            order_id="replay-" + cid,
            type=order["type"],
            side=order["side"],
            status=status,
            original_qty=order["qty"],
            executed_qty="0",
            canceled_qty="0",
            remain_qty=order["qty"],
            average_executed_price="0",
            fee="0",
            fee_rate=str(self._fee_rate(order)),
        )
        self.rows[cid] = row
        self.orders[cid] = saved
        self.meta[cid] = dict(
            eligible_ms=self.now_ms + self.latency_ms,
            arrived=False,
            queue=None,
            level=None,
            seen=D(0),
            gross=D(0),
            cancel_at=None,
        )
        return {"order_id": row["order_id"]}

    def detail(self, coin, cid):
        row = self.rows.get(cid)
        if row is None or row["target_currency"] != coin:
            raise CoinoneError("replay order unavailable")
        if self.now_ms < self.meta[cid]["eligible_ms"]:
            raise CoinoneError("replay order transport pending")
        return copy.deepcopy(row)

    def cancel(self, coin, cid):
        row = self.rows.get(cid)
        if row is None or row["target_currency"] != coin:
            raise CoinoneError("replay order unavailable")
        self.cancellations.append(cid)
        self.meta[cid]["cancel_at"] = self.now_ms + self.latency_ms
        return {}

    def _remaining(self, cid):
        return decimal(self.rows[cid]["original_qty"]) - decimal(self.rows[cid]["executed_qty"])

    def _fill(self, cid, qty, price):
        qty, price = decimal(qty, positive=True), decimal(price, positive=True)
        row, order, meta = self.rows[cid], self.orders[cid], self.meta[cid]
        qty = min(qty, self._remaining(cid))
        if not qty:
            return
        gross = qty * price
        fee = gross * self._fee_rate(order)
        previous_qty = decimal(row["executed_qty"])
        meta["gross"] += gross
        total_qty = previous_qty + qty
        row.update(
            executed_qty=str(total_qty),
            average_executed_price=str(meta["gross"] / total_qty),
            fee=str(decimal(row["fee"]) + fee),
            remain_qty=str(decimal(row["original_qty"]) - total_qty),
        )
        coin = row["target_currency"]
        if row["side"] == "BUY":
            self.cash -= gross + fee
            self.assets[coin] = self.assets.get(coin, D(0)) + qty
        else:
            if qty > self.assets.get(coin, D(0)):
                raise CoinoneError("replay sell exceeds inventory")
            self.cash += gross - fee
            self.assets[coin] -= qty
        row["status"] = "FILLED" if not self._remaining(cid) else "PARTIALLY_FILLED"

    def _cancel_due(self):
        for cid, row in self.rows.items():
            when = self.meta[cid].get("cancel_at")
            if row["status"] in TERMINAL or when is None or self.now_ms < when:
                continue
            remaining = self._remaining(cid)
            row["canceled_qty"] = str(decimal(row["canceled_qty"]) + remaining)
            row["remain_qty"] = "0"
            row["status"] = (
                "NOT_TRIGGERED_CANCELED"
                if row["type"] == "STOP_LIMIT" and row["status"] == "NOT_TRIGGERED"
                else "CANCELED"
            )

    def on_trade(self, coin, trade):
        identity = (coin, str(trade.get("id")))
        if identity in self.seen_trades:
            return
        self.seen_trades.add(identity)
        price = decimal(trade["price"], positive=True)
        size = decimal(trade["qty"], positive=True)
        event_ms = int(trade.get("timestamp", self.now_ms))
        aggressive_sell = trade.get("is_seller_maker") is False
        for cid, row in list(self.rows.items()):
            order, meta = self.orders[cid], self.meta[cid]
            if (
                row["target_currency"] != coin
                or order["role"] != "buy"
                or row["status"] not in LIVE
                or self.now_ms < meta["eligible_ms"]
                or event_ms < meta["eligible_ms"]
            ):
                continue
            limit = decimal(order["price"], positive=True)
            if not self._arrive_buy(cid):
                continue
            if price < limit:
                self._fill(cid, self._remaining(cid), limit)
            elif price == limit and aggressive_sell:
                ahead = min(size, meta["queue"])
                meta["queue"] -= ahead
                residual = size - ahead
                if residual > 0:
                    self._fill(cid, residual, limit)
        self._cancel_due()

    def _market_sell(self, cid, bids):
        order = self.orders[cid]
        limit = decimal(order["limit_price"]) if order.get("limit_price") else None
        remaining = self._remaining(cid)
        for row in bids[:5]:
            price = decimal(row["price"], positive=True)
            if limit is not None and price < limit:
                continue
            available = decimal(row["qty"]) * self.depth_fraction
            take = min(remaining, available)
            if take:
                self._fill(cid, take, price)
                remaining = self._remaining(cid)
            if not remaining:
                return
        result = self.rows[cid]
        if remaining:
            result["canceled_qty"] = str(decimal(result["canceled_qty"]) + remaining)
            result["remain_qty"] = "0"
            result["status"] = "CANCELED_LIMIT_PRICE_EXCEED" if limit is not None else "CANCELED_NO_ORDER"

    def _reject_post_only_cross(self, cid):
        remaining = self._remaining(cid)
        row = self.rows[cid]
        row["canceled_qty"] = str(decimal(row["canceled_qty"]) + remaining)
        row["remain_qty"] = "0"
        row["status"] = "REJECTED"

    def _arrive_buy(self, cid):
        meta, order = self.meta[cid], self.orders[cid]
        if meta["arrived"]:
            return self.rows[cid]["status"] not in TERMINAL
        book = self.books.get(order["coin"]) or {}
        bids, asks = book.get("bids", []), book.get("asks", [])
        if not bids or not asks:
            return False
        limit = decimal(order["price"], positive=True)
        if decimal(asks[0]["price"], positive=True) <= limit:
            self._reject_post_only_cross(cid)
            return False
        level = next(
            (decimal(row["qty"]) for row in bids if decimal(row["price"]) == limit),
            D(0),
        )
        meta.update(arrived=True, queue=level, level=level)
        return True

    def on_book(self, coin, book):
        self.set_book(coin, book)
        bids = (book or {}).get("bids", [])
        asks = (book or {}).get("asks", [])
        if not bids or not asks:
            self._cancel_due()
            return
        best_bid = decimal(bids[0]["price"], positive=True)
        best_ask = decimal(asks[0]["price"], positive=True)
        for cid, row in list(self.rows.items()):
            order, meta = self.orders[cid], self.meta[cid]
            if (
                row["target_currency"] != coin
                or row["status"] in TERMINAL
                or self.now_ms < meta["eligible_ms"]
            ):
                continue
            if order["type"] == "MARKET":
                self._market_sell(cid, bids)
                continue
            if order["type"] == "STOP_LIMIT":
                if row["status"] == "NOT_TRIGGERED" and best_bid <= decimal(order["trigger_price"]):
                    row["status"] = "TRIGGERED"
                if row["status"] in ("TRIGGERED", "PARTIALLY_FILLED"):
                    eligible = [level for level in bids if decimal(level["price"]) >= decimal(order["price"])]
                    for level in eligible[:5]:
                        take = min(
                            self._remaining(cid),
                            decimal(level["qty"]) * self.depth_fraction,
                        )
                        if take:
                            self._fill(cid, take, level["price"])
                        if not self._remaining(cid):
                            break
                continue
            if order["role"] == "buy":
                limit = decimal(order["price"], positive=True)
                if not self._arrive_buy(cid):
                    continue
                if best_ask <= limit:
                    # Once accepted as resting post-only liquidity, a later
                    # crossed ask is a maker fill rather than a rejection.
                    available = sum(
                        decimal(level["qty"])
                        for level in asks
                        if decimal(level["price"]) <= limit
                    ) * self.depth_fraction
                    if available:
                        self._fill(cid, min(self._remaining(cid), available), limit)
                else:
                    displayed = next(
                        (decimal(x["qty"]) for x in bids if decimal(x["price"]) == limit),
                        D(0),
                    )
                    meta["queue"] = min(meta["queue"], displayed)
        self._cancel_due()

    def advance(self):
        """Advance exchange-side latency against the latest recorded books."""
        for coin, book in list(self.books.items()):
            self.on_book(coin, book)
        self._cancel_due()

    def balances(self):
        locked_cash = D(0)
        locked_assets = {coin: D(0) for coin in self.assets}
        for cid, row in self.rows.items():
            if row["status"] in TERMINAL or self.now_ms < self.meta[cid]["eligible_ms"]:
                continue
            remaining = self._remaining(cid)
            order = self.orders[cid]
            if row["side"] == "BUY":
                locked_cash += remaining * decimal(order["price"]) * (D(1) + self._fee_rate(order))
            else:
                locked_assets[row["target_currency"]] = locked_assets.get(row["target_currency"], D(0)) + remaining
        result = [dict(currency="KRW", available=str(max(D(0), self.cash - locked_cash)), limit=str(locked_cash))]
        for coin in sorted(self.assets):
            locked = min(self.assets[coin], locked_assets.get(coin, D(0)))
            result.append(dict(currency=coin, available=str(self.assets[coin] - locked), limit=str(locked)))
        return result

    def active_orders(self):
        return [
            copy.deepcopy(row)
            for cid, row in self.rows.items()
            if row["status"] not in TERMINAL
            and self.now_ms >= self.meta[cid]["eligible_ms"]
        ]
