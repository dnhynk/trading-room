"""Causal Coinone public-feed adapter for the inherited Track A feature engine."""
from collections import Counter, deque
import time

from common.signal import Features
from track_c.execution.coinone import CoinoneError, decimal
from track_a_2.market.units import price_unit


def candle(row):
    return dict(
        ts=int(row["timestamp"]),
        o=float(row["open"]),
        h=float(row["high"]),
        l=float(row["low"]),
        c=float(row["close"]),
        v=float(row["target_volume"]),
    )


class Market:
    def __init__(self, coin, config, contract, units, fees, candles1=(), candles15=(), daily=(), now_ms=None):
        self.coin, self.config = coin, config
        self.contract, self.units, self.fees = contract, units, fees
        self.features = Features(config["signal"])
        self.book = None
        self.book_received = self.book_exchange = 0
        self.book_id = -1
        self.trade_exchange = -1
        self.trade_ids, self.seen = deque(), set()
        self.trade_candle = None
        self.signals = deque()
        self.counts = Counter()
        self.revision = 0
        self.seed(candles1, candles15, daily, now_ms=now_ms)

    def seed(self, candles1, candles15=(), daily=(), *, now_ms=None):
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        one = sorted((candle(row) for row in candles1 if int(row["timestamp"]) + 60_000 <= now_ms), key=lambda row: row["ts"])
        fifteen = sorted((candle(row) for row in candles15 if int(row["timestamp"]) + 900_000 <= now_ms), key=lambda row: row["ts"])
        days = sorted(
            (candle(row) for row in daily if int(row["timestamp"]) + 86_400_000 <= now_ms),
            key=lambda row: row["ts"],
        )
        self.features.seed_candles(one, fifteen)
        self.features.seed_daily(days)

    def tick(self):
        if not self.book:
            return None
        return price_unit(self.units, self.book["bids"][0]["price"])

    def fresh(self, now_ms):
        age = self.config["quote_max_age_ms"]
        return bool(
            self.book
            and 0 <= now_ms - self.book_received <= age
            and 0 <= now_ms - self.book_exchange <= age
        )

    def drain(self):
        result = list(self.signals)
        self.signals.clear()
        return result

    def _trade_bar(self, timestamp, price, qty):
        slot = timestamp // 60_000 * 60_000
        if self.trade_candle is None or self.trade_candle[0] != slot:
            self.trade_candle = [slot, price, price, price, price, qty]
        else:
            bar = self.trade_candle
            bar[2] = max(bar[2], price)
            bar[3] = min(bar[3], price)
            bar[4] = price
            bar[5] += qty
        self.features._candle(self.trade_candle)

    def feed(self, channel, data, received_ms):
        try:
            if data.get("quote_currency") != "KRW" or data.get("target_currency") != self.coin:
                raise CoinoneError("market identity mismatch")
            timestamp = int(data["timestamp"])
            if not 0 <= received_ms - timestamp <= self.config["quote_max_age_ms"]:
                self.counts["stale"] += 1
                return []
            if channel == "ORDERBOOK":
                identity = int(data["id"])
                if identity <= self.book_id:
                    self.counts["duplicate_book"] += 1
                    return []
                bids = sorted(
                    ((decimal(row["price"], positive=True), decimal(row["qty"])) for row in data["bids"] if decimal(row["qty"]) > 0),
                    reverse=True,
                )
                asks = sorted(
                    (decimal(row["price"], positive=True), decimal(row["qty"])) for row in data["asks"] if decimal(row["qty"]) > 0
                )
                if not bids or not asks or bids[0][0] >= asks[0][0]:
                    raise CoinoneError("invalid public order book")
                self.book_id = identity
                self.book_received, self.book_exchange = received_ms, timestamp
                self.book = dict(
                    bids=[dict(price=str(price), qty=str(qty)) for price, qty in bids],
                    asks=[dict(price=str(price), qty=str(qty)) for price, qty in asks],
                )
                message = dict(
                    arg=dict(channel="books15"),
                    ts=received_ms,
                    data=[dict(ts=received_ms, bids=[[str(p), str(q)] for p, q in bids], asks=[[str(p), str(q)] for p, q in asks])],
                )
                self.counts["books"] += 1
            elif channel == "TRADE":
                identity = str(data["id"])
                if identity in self.seen or timestamp < self.trade_exchange:
                    self.counts["duplicate_trade"] += 1
                    return []
                if type(data["is_seller_maker"]) is not bool:
                    raise CoinoneError("trade side unavailable")
                qty = decimal(data["qty"], positive=True)
                price = decimal(data["price"], positive=True)
                self.seen.add(identity)
                self.trade_ids.append(identity)
                if len(self.trade_ids) > 50_000:
                    self.seen.remove(self.trade_ids.popleft())
                self.trade_exchange = timestamp
                self._trade_bar(timestamp, float(price), float(qty))
                message = dict(
                    arg=dict(channel="trade"),
                    ts=received_ms,
                    data=[dict(side="buy" if data["is_seller_maker"] else "sell", size=str(qty), price=str(price))],
                )
                self.counts["trades"] += 1
            else:
                return []
            emitted = self.features.feed(message)
            self.signals.extend(emitted)
            self.revision += 1
            return emitted
        except (CoinoneError, KeyError, TypeError, ValueError, OverflowError):
            self.counts["invalid"] += 1
            return []
