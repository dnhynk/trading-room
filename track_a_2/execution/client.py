"""Coinone v2.1 client restricted to Track A-2 order identifiers.

The authenticated transport primitives are shared with Track C, but credentials,
identifiers, portfolios, ledgers, and runtime state are not.
"""
import os
from pathlib import Path
import re
import urllib.request

from track_c.execution.coinone import (
    ORIGIN,
    CoinoneError,
    CoinoneReadOnly,
    Credentials,
    decimal,
    symbol,
)


TOKEN_NAMES = ("COINONE_A2_ACCESS_TOKEN", "coinone-a2-access-token")
SECRET_NAMES = ("COINONE_A2_SECRET_KEY", "coinone-a2-secret-key")
INTERVALS = frozenset(("1m", "15m", "1d"))


def own_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"ta2-[a-z0-9_.-]{8,120}", value):
        raise CoinoneError("Track A-2 order identifier required")
    return value


def read_credentials(path, environ=None):
    """Read only A-2 credential names; Track C keys are never a fallback."""
    env = os.environ if environ is None else environ
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeError):
        raise CoinoneError("A-2 credential file unreadable") from None
    names = set(TOKEN_NAMES + SECRET_NAMES)
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or name not in names:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if name in values and values[name] != value:
            raise CoinoneError("conflicting A-2 credential declarations")
        values[name] = value
    if any(env.get(name) for name in names):
        values = {name: env[name] for name in names if env.get(name)}
    tokens = {values[name] for name in TOKEN_NAMES if values.get(name)}
    secrets = {values[name] for name in SECRET_NAMES if values.get(name)}
    if len(tokens) != 1 or len(secrets) != 1:
        raise CoinoneError("dedicated Coinone A-2 credentials not configured")
    return Credentials(tokens.pop(), secrets.pop())


class CoinoneA2(CoinoneReadOnly):
    """No transfer/withdrawal methods and no ability to address foreign orders."""

    def detail(self, coin, cid):
        result = self._signed(
            "/v2.1/order/detail",
            dict(quote_currency="KRW", target_currency=symbol(coin), user_order_id=own_id(cid)),
        )
        if not isinstance(result.get("order"), dict):
            raise CoinoneError("order detail missing")
        return result["order"]

    def cancel(self, coin, cid):
        return self._signed(
            "/v2.1/order/cancel",
            dict(quote_currency="KRW", target_currency=symbol(coin), user_order_id=own_id(cid)),
        )

    def submit(self, order, *, before_send=None):
        payload = dict(
            quote_currency="KRW",
            target_currency=symbol(order["coin"]),
            user_order_id=own_id(order["cid"]),
            side=order["side"],
            type=order["type"],
            qty=format(decimal(order["qty"], positive=True), "f"),
        )
        shape = (payload["side"], payload["type"])
        if shape not in {
            ("BUY", "LIMIT"),
            ("SELL", "LIMIT"),
            ("SELL", "MARKET"),
            ("SELL", "STOP_LIMIT"),
        }:
            raise CoinoneError("unsupported Track A-2 order shape")
        if payload["type"] in ("LIMIT", "STOP_LIMIT"):
            payload["price"] = format(decimal(order["price"], positive=True), "f")
        if payload["type"] == "LIMIT":
            payload["post_only"] = True
        if payload["type"] == "STOP_LIMIT":
            payload["trigger_price"] = format(decimal(order["trigger_price"], positive=True), "f")
        if payload["type"] == "MARKET" and order.get("limit_price"):
            payload["limit_price"] = format(decimal(order["limit_price"], positive=True), "f")
        return self._signed("/v2.1/order", payload, before_send=before_send)

    def candles(self, coin, interval="1m", size=500):
        if interval not in INTERVALS or type(size) is not int or not 1 <= size <= 500:
            raise ValueError("unsupported Coinone candle request")
        path = f"/public/v2/chart/KRW/{symbol(coin)}?interval={interval}&size={size}"
        response = self._transport(
            urllib.request.Request(ORIGIN + path, headers={"Accept": "application/json"}),
            self._timeout,
        )
        return self._rows(response, "chart")
