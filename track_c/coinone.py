"""Coinone v2.1 account inspection. This client cannot place/cancel/withdraw.

Authentication follows https://docs.coinone.co.kr/docs/about-public-api.
Never log request headers, credentials, raw errors, or signed payloads.
"""
import base64
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
import uuid

ORIGIN = "https://api.coinone.co.kr"
TOKEN_NAMES = ("COINONE_ACCESS_TOKEN", "COINONE_API_KEY", "coinone-access-token", "coinone-api-key")
SECRET_NAMES = ("COINONE_SECRET_KEY", "COINONE_API_SECRET", "coinone-secret-key", "coinone-api-secret")
PRIVATE_READS = frozenset(("/v2.1/account/balance/all", "/v2.1/order/active_orders"))


class CoinoneError(RuntimeError):
    """Contains only a locally selected label and numeric status, never response text."""
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Credentials:
    access_token: str = field(repr=False)
    secret_key: str = field(repr=False)

    @classmethod
    def read(cls, path, environ=None, *, profile="default"):
        env = os.environ if environ is None else environ
        if profile not in ("default", "aws"):
            raise CoinoneError("invalid credential profile")
        token_names = TOKEN_NAMES if profile == "default" else ("coinone-api-key-aws", "COINONE_ACCESS_TOKEN_AWS")
        secret_names = SECRET_NAMES if profile == "default" else ("coinone-secret-key-aws", "COINONE_SECRET_KEY_AWS")
        values = {}
        try:
            lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            lines = []
        except (OSError, UnicodeError):
            raise CoinoneError("credential file unreadable") from None
        names = set(token_names + secret_names)
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
                raise CoinoneError("conflicting credential declarations")
            values[name] = value

        def select(aliases):
            # Environment variables are an alternative complete source, never a
            # way to silently combine an environment token with another file's secret.
            return {values[n] for n in aliases if values.get(n)}

        if any(env.get(n) for n in names):
            values = {n: env[n] for n in names if env.get(n)}
        token, secret = select(token_names), select(secret_names)
        if len(token) > 1 or len(secret) > 1:
            raise CoinoneError("conflicting credential aliases")
        if not token or not secret:
            raise CoinoneError("Coinone access token / secret key not configured")
        return cls(token.pop(), secret.pop())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CoinoneError("redirect refused")


def exchange(request, timeout):
    # Redirects must not forward signed account requests to another origin.
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise CoinoneError("response too large")
        value = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise CoinoneError(f"HTTP {int(exc.code)}") from None
    except (OSError, ValueError):
        raise CoinoneError("network or response decoding failure") from None
    if not isinstance(value, dict):
        raise CoinoneError("invalid response envelope")
    if value.get("result") != "success" or str(value.get("error_code")) != "0":
        code = str(value.get("error_code", ""))
        safe = code if re.fullmatch(r"[0-9]{1,6}", code) else "unknown"
        raise CoinoneError(f"Coinone API error {safe}", code=int(safe) if safe.isdigit() else None)
    return value


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", value):
        raise ValueError("invalid market symbol")
    return value


def decimal(value, *, positive=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CoinoneError("invalid numeric response") from None
    if not number.is_finite() or number < 0 or (positive and not number):
        raise CoinoneError("invalid numeric response")
    return number


class CoinoneReadOnly:
    def __init__(self, credentials=None, *, transport=exchange, timeout=10):
        self._credentials = credentials
        self._transport = transport
        self._timeout = timeout

    def _post(self, path):
        if path not in PRIVATE_READS and not re.fullmatch(r"/v2\.1/account/trade_fee/KRW/[A-Z0-9]{1,20}", path):
            raise CoinoneError("endpoint is outside the read-only allowlist")
        return self._signed(path, {})

    def _signed(self, path, fields):
        if self._credentials is None:
            raise CoinoneError("Coinone credentials required")
        payload = dict(fields, access_token=self._credentials.access_token, nonce=str(uuid.uuid4()))
        encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(self._credentials.secret_key.encode("utf-8"), encoded, hashlib.sha512).hexdigest()
        req = urllib.request.Request(ORIGIN + path, data=base64.b64decode(encoded), method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "X-COINONE-PAYLOAD": encoded.decode("ascii"), "X-COINONE-SIGNATURE": signature,
        })
        return self._transport(req, self._timeout)

    def _get(self, resource, coin):
        if resource not in ("markets", "range_units", "orderbook"):
            raise CoinoneError("public endpoint not allowed")
        path = f"/public/v2/{resource}/KRW/{symbol(coin)}"
        if resource == "orderbook":
            path += "?size=15&order_book_unit=0"
        return self._transport(urllib.request.Request(ORIGIN + path, headers={"Accept": "application/json"}), self._timeout)

    @staticmethod
    def _rows(response, name):
        rows = response.get(name)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CoinoneError("invalid response rows")
        return rows

    def balances(self):
        return self._rows(self._post("/v2.1/account/balance/all"), "balances")

    def active_orders(self):
        # Omit order_type to include both ordinary and conditional resting orders.
        return self._rows(self._post("/v2.1/order/active_orders"), "active_orders")

    def fees(self, coin):
        rows = self._rows(self._post(f"/v2.1/account/trade_fee/KRW/{symbol(coin)}"), "fee_rates")
        matches = [r for r in rows if r.get("quote_currency") == "KRW" and r.get("target_currency") == coin]
        if len(matches) != 1:
            raise CoinoneError("requested fee pair missing or ambiguous")
        return {k: str(decimal(matches[0][k])) for k in ("maker", "taker")}

    def market(self, coin):
        rows = self._rows(self._get("markets", coin), "markets")
        matches = [r for r in rows if r.get("quote_currency") == "KRW" and r.get("target_currency") == coin]
        if len(matches) != 1:
            raise CoinoneError("requested market missing or ambiguous")
        return matches[0]

    def price_units(self, coin):
        return self._rows(self._get("range_units", coin), "range_price_units")

    def orderbook(self, coin):
        row = self._get("orderbook", coin)
        if row.get("quote_currency") != "KRW" or row.get("target_currency") != coin or decimal(row.get("order_book_unit")) != 0:
            raise CoinoneError("unexpected orderbook market or aggregation")
        return row

    def universe(self):
        def get(path):
            return self._transport(urllib.request.Request(ORIGIN + path, headers={"Accept": "application/json"}), self._timeout)
        def normalized(rows):
            # ticker_new currently returns lower-case currencies while markets
            # and websocket messages use upper case. Join at the API boundary.
            result = []
            for row in rows:
                quote, target = row.get("quote_currency"), row.get("target_currency")
                if not isinstance(quote, str) or quote.upper() != "KRW" or not isinstance(target, str):
                    raise CoinoneError("invalid universe market identity")
                result.append(dict(row, quote_currency="KRW", target_currency=symbol(target.upper())))
            return result
        return (normalized(self._rows(get("/public/v2/markets/KRW"), "markets")),
                normalized(self._rows(get("/public/v2/ticker_new/KRW"), "tickers")))

    def candles(self, coin):
        path = f"/public/v2/chart/KRW/{symbol(coin)}?interval=1m&size=200"
        return self._rows(self._transport(urllib.request.Request(ORIGIN + path, headers={"Accept": "application/json"}), self._timeout), "chart")
