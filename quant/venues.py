"""Public spot data normalization and frozen venue contracts. No signing or orders."""
from collections import Counter, deque
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import json
import math

from .config import Config
from .data import Event, epoch_ms

WS = {"coinone": "wss://stream.coinone.co.kr", "bithumb": "wss://ws-api.bithumb.com/websocket/v1",
      "upbit": "wss://api.upbit.com/websocket/v1", "korbit": "wss://ws-api.korbit.co.kr/v2/public"}
BITHUMB_END = epoch_ms("2026-09-06T18:00:00+09:00")
BITHUMB_TICKS = [[0, .0001], [1, .001], [10, .01], [100, 1], [1000, 1], [5000, 5],
                 [10000, 10], [50000, 50], [100000, 100], [500000, 500], [1000000, 1000]]
UPBIT_TICKS = [[0, 1e-8], [.00001, 1e-7], [.0001, 1e-6], [.001, 1e-5], [.01, .0001],
               [.1, .001], [1, .01], [10, .1], [100, 1], [1000, 1], [5000, 5],
               [10000, 10], [50000, 50], [100000, 100], [500000, 500], [1000000, 1000]]


def tick_at(ladder, price):
    return next(unit for floor, unit in reversed(ladder) if price >= floor)


def round_price(ladder, price, up=False):
    value = Decimal(str(price))
    for _ in range(3):
        tick = Decimal(str(tick_at(ladder, float(value))))
        rounded = (value / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick
        if tick_at(ladder, float(rounded)) == float(tick):
            return float(rounded)
        value = rounded
    raise ValueError("price ladder boundary cannot be rounded")


def subscriptions(venue, coins):
    if venue == "coinone":
        return [dict(request_type="SUBSCRIBE", channel=ch, topic=dict(quote_currency="KRW", target_currency=c))
                for c in coins for ch in ("ORDERBOOK", "TRADE")]
    if venue == "korbit":
        return [[dict(method="subscribe", type=ch, symbols=[c.lower() + "_krw" for c in coins]) for ch in ("orderbook", "trade")]]
    return [[dict(ticket="quant-public-research"), *[dict(type=ch, codes=["KRW-" + c for c in coins]) for ch in ("orderbook", "trade")]]]


def message(symbol, channel, data, timestamp, action="update"):
    return dict(arg=dict(instId=symbol, channel=channel), action=action, ts=timestamp, data=data)


class Normalizer:
    """Keep native receive order, reject stale trades, deduplicate by exchange ID.

    Books are complete snapshots; zero-size placeholder levels are removed BEFORE
    sorting. Bithumb book timestamps are microseconds; trade timestamps are ms.
    """
    def __init__(self, venue, coins):
        if venue not in WS:
            raise ValueError("unsupported public venue")
        self.venue, self.coins = venue, set(coins)
        self.quality = Counter()
        self.ids, self.recent = set(), deque()
        self.last_t = 0

    def parse(self, recv, raw):
        try:
            if recv < self.last_t:
                self.quality["receive_regression"] += 1
                return None
            self.last_t = recv
            m = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if not isinstance(m, dict):
                return None
            if m.get("error") or m.get("response_type") == "ERROR":
                raise ValueError("exchange error")
            if self.venue == "coinone":
                if m.get("response_type") != "DATA":
                    return None
                d, ch = m["data"], m["channel"].lower()
                coin, ts = d["target_currency"], int(d["timestamp"])
                snapshot = False
                trades = [dict(price=d.get("price"), size=d.get("qty"), side="buy" if d.get("is_seller_maker") else "sell", tradeId=d.get("id"), ts=ts)]
            elif self.venue == "korbit":
                ch, coin, d = m.get("type"), m.get("symbol", "").split("_")[0].upper(), m.get("data")
                if ch not in {"orderbook", "trade"}:
                    return None
                ts, snapshot = int(m["timestamp"]), bool(m.get("snapshot"))
                if ch == "orderbook":
                    ts = int(d["timestamp"])
                    trades = []
                else:
                    trades = [dict(price=x["price"], size=x["qty"], side="buy" if x["isBuyerTaker"] else "sell", tradeId=x["tradeId"], ts=int(x["timestamp"])) for x in d]
            else:
                ch, coin, d = m.get("type"), m.get("code", "")[4:], m
                if ch not in {"orderbook", "trade"}:
                    return None
                ts = int(m.get("trade_timestamp", m["timestamp"]))
                if self.venue == "bithumb" and ch == "orderbook" and ts > 10**14:
                    ts //= 1000
                snapshot = m.get("stream_type") == "SNAPSHOT"
                trades = [dict(price=m.get("trade_price"), size=m.get("trade_volume"), side="buy" if m.get("ask_bid") == "BID" else "sell", tradeId=m.get("sequential_id"), ts=ts)]
            if coin not in self.coins or ch not in {"orderbook", "trade"}:
                return None
            if ts > recv + 1000 or ts <= 0:
                raise ValueError("exchange clock")
            symbol = coin + "KRW"
            if ch == "orderbook":
                if self.venue in {"coinone", "korbit"}:
                    sides = {side: [(float(x["price"]), float(x["qty"])) for x in d[side]] for side in ("bids", "asks")}
                else:
                    sides = {side: [(float(x[p + "_price"]), float(x[p + "_size"])) for x in d["orderbook_units"]]
                             for side, p in (("bids", "bid"), ("asks", "ask"))}
                for side, rows in sides.items():
                    if any(not math.isfinite(p + q) or p <= 0 or q < 0 for p, q in rows):
                        raise ValueError("invalid depth")
                    rows = sorted((p, q) for p, q in rows if q > 0)
                    if len(set(p for p, q in rows)) != len(rows):
                        raise ValueError("duplicate price level")
                    sides[side] = rows[::-1] if side == "bids" else rows
                if not all(sides.values()) or sides["bids"][0][0] >= sides["asks"][0][0]:
                    raise ValueError("empty/crossed book")
                out = message(symbol, "books15", [dict(**sides, ts=ts)], ts, "snapshot")
            else:
                if snapshot:
                    self.quality["trade_snapshots_excluded"] += 1
                    return None
                fresh = []
                for x in trades:
                    p, q, tt = float(x["price"]), float(x["size"]), x["ts"]
                    if not math.isfinite(p + q) or min(p, q) <= 0 or x["tradeId"] is None:
                        raise ValueError("invalid trade")
                    identity = (symbol, str(x["tradeId"]))
                    if identity in self.ids:
                        self.quality["duplicate_trades"] += 1
                        continue
                    self.ids.add(identity)
                    self.recent.append(identity)
                    if len(self.recent) > 100000:
                        self.ids.discard(self.recent.popleft())
                    if not 0 <= recv - tt <= 5000:
                        self.quality["stale_trades"] += 1
                        continue
                    fresh.append(x)
                if not fresh:
                    return None
                out = message(symbol, "trade", fresh, ts)
            self.quality[ch] += 1
            return Event(recv, symbol, out["arg"]["channel"], out)
        except (ValueError, TypeError, KeyError, IndexError):
            self.quality["invalid_messages"] += 1
            return None


def research_config(base, venue="bitget", contracts=None, equity=None):
    d = base.data
    d.update(version=2, name=f"{venue}-scalp-development", equity=equity or d["equity"])
    d["books"] = contracts or {s: dict(b, min_order=5, price_ladder=[[0, b["tick"]]]) for s, b in d["books"].items()}
    fees = {"bitget": (.0002, .0006), "coinone": (0, 0), "bithumb": (.0004, .0004), "upbit": (.0005, .0005), "korbit": (0, 0)}[venue]
    d["execution"].update(maker=fees[0], taker=fees[1])
    # Bithumb base deliberately evaluates the post-promotion coupon tariff for the
    # entire tape. A separate calendar scenario records the actual scheduled fee.
    d["research"] = dict(venue=venue, market="perpetual" if venue == "bitget" else "spot", policy="scalp", regime="structure",
                         entry_ttl_s=5, max_hold_s=60, invalidation_ticks=2, invalidation_confirm_s=2,
                         min_trades_10s=3, phase_max_age_s=1200, fee_schedule=[[0, *fees]],
                         fee_stress_floor={"coinone": .0002, "bithumb": .0004, "korbit": .0006}.get(venue, 0))
    return Config.create(d)
