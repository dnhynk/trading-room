"""Classic v2 account observations. Fixed-host GET requests; no write transport.

Wire responses stay separate from the UTA engine. Credentials are loaded only
when the caller explicitly supplies an environment file to this client.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


PUBLIC_PATHS = frozenset({
    "/api/v2/public/time",
    "/api/v2/mix/market/ticker",
    "/api/v2/mix/market/contracts",
    "/api/v2/mix/market/candles",
    "/api/v2/mix/market/merge-depth",
    "/api/v2/mix/market/fills",
    "/api/v2/mix/market/current-fund-rate",
    "/api/v2/mix/market/query-position-lever",
})
PRIVATE_PATHS = frozenset({
    "/api/v2/mix/account/account",
    "/api/v2/mix/account/open-count",
    "/api/v2/mix/position/all-position",
    "/api/v2/mix/order/orders-pending",
    "/api/v2/mix/order/orders-plan-pending",
})
SYMBOL = "ARXUSDT"
PRODUCT = "USDT-FUTURES"


class ClassicReadError(RuntimeError):
    """Only a code is exposed; exchange error text may contain private data."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class ClassicReadOnlyClient:
    def __init__(self, key: str, secret: str, passphrase: str) -> None:
        if not all((key, secret, passphrase)):
            raise ValueError("CLASSIC_READ_CREDENTIALS_MISSING")
        self._key, self._secret, self._passphrase = key, secret, passphrase
        self.account_binding = hashlib.sha256(key.encode()).hexdigest()
        self._offset_ms = 0

    @classmethod
    def from_env(cls, path: Path) -> ClassicReadOnlyClient:
        from dotenv import dotenv_values

        if not path.is_file():
            raise ValueError("CLASSIC_ENV_FILE_MISSING")
        values = dotenv_values(path)
        return cls(*(values.get(name) or "" for name in (
            "BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE"
        )))

    def get(self, path: str, **params: str) -> Any:
        if path not in PUBLIC_PATHS | PRIVATE_PATHS:
            raise ValueError("CLASSIC_READ_ENDPOINT_NOT_ALLOWED")
        query = "?" + urlencode(params) if params else ""
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if path in PRIVATE_PATHS:
            timestamp = str(int(time.time() * 1000) + self._offset_ms)
            message = timestamp + "GET" + path + query
            signature = base64.b64encode(hmac.new(
                self._secret.encode(), message.encode(), hashlib.sha256
            ).digest()).decode()
            headers.update({
                "ACCESS-KEY": self._key, "ACCESS-SIGN": signature,
                "ACCESS-TIMESTAMP": timestamp, "ACCESS-PASSPHRASE": self._passphrase,
            })
        request = Request("https://api.bitget.com" + path + query,
                          headers=headers, method="GET")
        try:
            with build_opener(_NoRedirect()).open(request, timeout=10) as response:
                payload = json.loads(response.read(), parse_float=Decimal)
        except HTTPError as exc:
            try:
                code = str(json.loads(exc.read()).get("code", "HTTP_ERROR"))
            except (ValueError, AttributeError):
                code = "HTTP_ERROR"
            # Never surface response text, request headers, or credentials.
            raise ClassicReadError(code if code.isdigit() else "HTTP_ERROR") from None
        except (URLError, TimeoutError, OSError):
            raise ClassicReadError("CLASSIC_READ_NETWORK_ERROR") from None
        except ValueError:
            raise ClassicReadError("CLASSIC_READ_INVALID_JSON") from None
        if not isinstance(payload, dict) or payload.get("code") != "00000":
            raise ClassicReadError("CLASSIC_READ_REJECTED")
        if "data" not in payload:
            raise ClassicReadError("CLASSIC_READ_SCHEMA_ERROR")
        return payload["data"]

    def _pending_count(self, *, plan_type: str | None = None,
                       status: str = "live") -> int:
        path = "/api/v2/mix/order/" + (
            "orders-plan-pending" if plan_type else "orders-pending"
        )
        params = {"productType": PRODUCT, "limit": "100"}
        if plan_type:
            params["planType"] = plan_type
        else:
            params["status"] = status
        total = 0
        seen_cursors: set[str] = set()
        for _ in range(20):
            data = self.get(path, **params)
            if not isinstance(data, dict) or "entrustedList" not in data:
                raise ClassicReadError("CLASSIC_ORDERS_SCHEMA_ERROR")
            rows = data["entrustedList"]
            if rows is None:
                rows = []  # Classic explicitly returns null for an empty list.
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise ClassicReadError("CLASSIC_ORDERS_SCHEMA_ERROR")
            total += len(rows)
            if len(rows) < 100:
                return total
            cursor = data.get("endId")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise ClassicReadError("CLASSIC_ORDER_PAGINATION_INCOMPLETE")
            seen_cursors.add(cursor)
            params["idLessThan"] = cursor
        raise ClassicReadError("CLASSIC_ORDER_PAGINATION_INCOMPLETE")

    def snapshot(self) -> dict[str, Any]:
        server = self.get("/api/v2/public/time")
        self._offset_ms = int(server["serverTime"]) - int(time.time() * 1000)
        started_ms = int(time.time() * 1000) + self._offset_ms
        calls = {
            "account": lambda: self.get(
                "/api/v2/mix/account/account", symbol=SYMBOL,
                productType=PRODUCT, marginCoin="USDT"),
            "positions": lambda: self.get(
                "/api/v2/mix/position/all-position", productType=PRODUCT,
                marginCoin="USDT"),
            "ticker": lambda: self.get(
                "/api/v2/mix/market/ticker", symbol=SYMBOL, productType=PRODUCT),
            "contract": lambda: self.get(
                "/api/v2/mix/market/contracts", symbol=SYMBOL, productType=PRODUCT),
            "regular_orders": lambda: self._pending_count(),
            "partial_orders": lambda: self._pending_count(status="partially_filled"),
            "trigger_orders": lambda: self._pending_count(plan_type="normal_plan"),
            "protective_orders": lambda: self._pending_count(plan_type="profit_loss"),
        }
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {name: pool.submit(fn) for name, fn in calls.items()}
            result = {name: future.result() for name, future in pending.items()}
        for name in ("ticker", "contract"):
            rows = result[name]
            if (not isinstance(rows, list) or len(rows) != 1
                    or rows[0].get("symbol") != SYMBOL):
                raise ClassicReadError("CLASSIC_INSTRUMENT_IDENTITY_MISMATCH")
            result[name] = rows[0]
        result.update(api_family="classic_v2", started_ms=started_ms,
                      finished_ms=int(time.time() * 1000) + self._offset_ms)
        return result

    def estimate_quantity(self, amount: str, price: str, leverage: str) -> str:
        data = self.get(
            "/api/v2/mix/account/open-count", symbol=SYMBOL,
            productType=PRODUCT, marginCoin="USDT", openAmount=amount,
            openPrice=price, leverage=leverage,
        )
        if not isinstance(data, dict) or not isinstance(data.get("size"), str):
            raise ClassicReadError("CLASSIC_OPEN_COUNT_SCHEMA_ERROR")
        return str(data["size"])

    def market_snapshot(self) -> dict[str, Any]:
        """Unsigned public observations; account credentials are not transmitted."""
        started_ms = int(time.time() * 1000) + self._offset_ms
        params = {"symbol": SYMBOL, "productType": PRODUCT}
        calls = {
            "ticker": ("ticker", params),
            "contract": ("contracts", params),
            "candles": ("candles", params | {"granularity": "1m", "limit": "100"}),
            "book": ("merge-depth", params | {"precision": "scale0", "limit": "50"}),
            "trades": ("fills", params | {"limit": "100"}),
            "funding": ("current-fund-rate", params),
            "tiers": ("query-position-lever", params),
        }
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {name: pool.submit(self.get, "/api/v2/mix/market/" + path, **args)
                       for name, (path, args) in calls.items()}
            result = {name: future.result() for name, future in pending.items()}
        for name in ("ticker", "contract", "funding"):
            rows = result[name]
            if (not isinstance(rows, list) or len(rows) != 1
                    or rows[0].get("symbol") != SYMBOL):
                raise ClassicReadError("CLASSIC_INSTRUMENT_IDENTITY_MISMATCH")
            result[name] = rows[0]
        result.update(api_family="classic_v2", started_ms=started_ms,
                      finished_ms=int(time.time() * 1000) + self._offset_ms)
        return result
