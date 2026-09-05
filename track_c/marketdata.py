"""Receive-time causal Coinone features with independent book/trade freshness."""
from collections import deque
from decimal import Decimal as D

from bot.signal import Features
from .coinone import CoinoneError, decimal
from .microstructure import Micro


class Market:
    def __init__(self, coin, config, contract, units, fees, candles):
        self.coin, self.config, self.contract, self.units, self.fees = coin, config, contract, units, fees
        self.features = Features(config["signal"])
        self.book, self.received, self.exchange_ms = None, 0, 0
        self.trades, self.ids, self.seen = deque(), deque(), set()
        self.last_id, self.last_trade_ms = -1, 0
        self.counts = dict(books=0, trades=0, rejected=0)
        self.micro = Micro(coin, stale_ms=config['quote_max_age_ms'])
        self.seed(candles)

    def seed(self, candles, now_ms=None):
        import time
        now_ms = now_ms or int(time.time()*1000)
        rows = sorted((dict(ts=int(r["timestamp"]), o=float(r["open"]), h=float(r["high"]), l=float(r["low"]), c=float(r["close"]), v=float(r["target_volume"]))
                       for r in candles if int(r["timestamp"])+60000 <= now_ms), key=lambda r:r["ts"])
        if not self.features.candles:
            self.features.seed_candles(rows)
        else:
            latest = self.features.candles[-1]["ts"]
            added = [r for r in rows if r["ts"] > latest]
            if added:
                self.features.candles.extend(added)
                del self.features.candles[:-600]
                self.features._candle_closed()

    def fresh(self, now):
        limit = self.config["quote_max_age_ms"]
        return self.book is not None and 0 <= now-self.received <= limit and 0 <= now-self.exchange_ms <= limit

    def volume(self, now):
        while self.trades and self.trades[0][0] < now-10000:
            self.trades.popleft()
        return sum((q for _,q in self.trades), D(0)), len(self.trades)

    def feed(self, channel, data, recv):
        try:
            if data.get("quote_currency") != "KRW" or data.get("target_currency") != self.coin:
                raise CoinoneError("market identity mismatch")
            ts = int(data["timestamp"])
            if not 0 <= recv-ts <= self.config["quote_max_age_ms"]:
                self.counts["rejected"] += 1
                return []
            if channel == "ORDERBOOK":
                identity = int(data["id"])
                if identity <= self.last_id:
                    return []
                bids = sorted(((decimal(r["price"], positive=True), decimal(r["qty"])) for r in data["bids"] if decimal(r["qty"]) > 0), reverse=True)
                asks = sorted((decimal(r["price"], positive=True), decimal(r["qty"])) for r in data["asks"] if decimal(r["qty"]) > 0)
                if not bids or not asks or bids[0][0] >= asks[0][0]:
                    raise CoinoneError("invalid public book")
                self.last_id, self.received, self.exchange_ms = identity, recv, ts
                self.book = dict(bids=[dict(price=str(p), qty=str(q)) for p,q in bids], asks=[dict(price=str(p), qty=str(q)) for p,q in asks])
                msg = dict(arg=dict(channel="books15"), ts=recv, data=[dict(ts=recv, bids=[[str(p),str(q)] for p,q in bids], asks=[[str(p),str(q)] for p,q in asks])])
                self.counts["books"] += 1
            elif channel == "TRADE":
                key = str(data["id"])
                if key in self.seen or ts < self.last_trade_ms:
                    return []
                if type(data["is_seller_maker"]) is not bool:
                    raise CoinoneError("trade side unavailable")
                qty, px = decimal(data["qty"], positive=True), decimal(data["price"], positive=True)
                self.seen.add(key)
                self.ids.append(key)
                if len(self.ids) > 50000:
                    self.seen.remove(self.ids.popleft())
                self.last_trade_ms = ts
                self.trades.append((recv, qty))
                self.volume(recv)
                msg = dict(arg=dict(channel="trade"), ts=recv, data=[dict(side="buy" if data["is_seller_maker"] else "sell", size=str(qty), price=str(px))])
                self.counts["trades"] += 1
            else:
                return []
            self.micro.feed(channel, data, recv)
            return self.features.feed(msg)
        except (CoinoneError, KeyError, ValueError, TypeError, OverflowError):
            self.counts["rejected"] += 1
            return []
