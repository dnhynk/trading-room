"""Coinone order methods restricted to Track C identifiers. No transfer methods."""
import re
from .coinone import CoinoneError, CoinoneReadOnly, decimal, symbol


def own_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"tc-[a-z0-9_.-]{8,120}", value):
        raise CoinoneError("Track C order identifier required")
    return value


class CoinoneExecution(CoinoneReadOnly):
    def detail(self, coin, cid):
        result = self._signed("/v2.1/order/detail", dict(quote_currency="KRW", target_currency=symbol(coin), user_order_id=own_id(cid)))
        if not isinstance(result.get("order"), dict):
            raise CoinoneError("order detail missing")
        return result["order"]

    def cancel(self, coin, cid):
        return self._signed("/v2.1/order/cancel", dict(quote_currency="KRW", target_currency=symbol(coin), user_order_id=own_id(cid)))

    def submit(self, order):
        """The durable intent must exist before this method is called."""
        payload = dict(quote_currency="KRW", target_currency=symbol(order["coin"]), user_order_id=own_id(order["cid"]),
                       side=order["side"], type=order["type"], qty=format(decimal(order["qty"], positive=True), "f"))
        if (payload["side"], payload["type"]) not in (("BUY", "LIMIT"), ("SELL", "LIMIT"), ("SELL", "MARKET"), ("SELL", "STOP_LIMIT")):
            raise CoinoneError("unsupported Track C order shape")
        if payload["type"] in ("LIMIT", "STOP_LIMIT"):
            payload["price"] = format(decimal(order["price"], positive=True), "f")
        if payload["type"] == "LIMIT":
            payload["post_only"] = True
        if payload["type"] == "STOP_LIMIT":
            payload["trigger_price"] = format(decimal(order["trigger_price"], positive=True), "f")
        if payload['type']=='MARKET' and order.get('limit_price'):
            payload['limit_price']=format(decimal(order['limit_price'],positive=True),'f')
        return self._signed("/v2.1/order", payload)
