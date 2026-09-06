"""Small stdlib-only client for documented Bitget UTA v3 public GET data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://api.bitget.com"
FUTURES_CATEGORY = "USDT-FUTURES"
FUTURES_SYMBOL = "ARXUSDT"
SPOT_SYMBOL = "ARXUSDT"
SPOT_CATEGORY = "SPOT"
BENCHMARK_SYMBOLS = ("BTCUSDT", "ETHUSDT")


@dataclass(frozen=True, slots=True)
class EndpointCapability:
    """A public endpoint claim; undocumented routes remain intentionally absent."""

    name: str
    path: str
    documented: bool
    supports_futures: bool
    supports_spot: bool
    notes: str


CAPABILITIES = {
    "instruments": EndpointCapability("instruments", "/api/v3/market/instruments", True, True, True, "Product identity and precision."),
    "tickers": EndpointCapability("tickers", "/api/v3/market/tickers", True, True, True, "Ticker list; fields vary by category."),
    "orderbook": EndpointCapability("orderbook", "/api/v3/market/orderbook", True, True, True, "Snapshot only; no implied sequence continuity."),
    "funding_current": EndpointCapability("funding_current", "/api/v3/market/current-fund-rate", True, True, False, "Futures funding current rate."),
    "funding_history": EndpointCapability("funding_history", "/api/v3/market/history-fund-rate", True, True, False, "Historical futures funding."),
    "open_interest": EndpointCapability("open_interest", "/api/v3/market/open-interest", True, True, False, "Open-interest snapshot."),
    "candles": EndpointCapability("candles", "/api/v3/market/candles", True, True, True, "Completed status must be established from interval/end time."),
    "position_tiers": EndpointCapability("position_tiers", "/api/v3/market/position-tier", True, True, False, "Public futures position tiers."),
    "fills": EndpointCapability("fills", "/api/v3/market/fills", True, True, True, "Public completed trades."),
    "liquidations": EndpointCapability("liquidations", "/api/v3/market/liquidations", True, True, False, "Response data.list with cursor."),
}


def _response_items(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    data = payload.get("data")
    if isinstance(data, list):
        return tuple(item for item in data if isinstance(item, Mapping))
    if isinstance(data, Mapping):
        for key in ("list", "resultList"):
            if isinstance(data.get(key), list):
                return tuple(item for item in data[key] if isinstance(item, Mapping))
        return (data,)
    return ()


class BitgetUtaV3PublicClient:
    """GET-only client.  It has no signing, credential, or write capability."""

    def __init__(self, base_url: str = BASE_URL, timeout_seconds: float = 10.0) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Bitget public base URL must be HTTPS")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get(self, capability: str, params: Mapping[str, str | int] | None = None) -> tuple[dict[str, Any], datetime]:
        endpoint = CAPABILITIES[capability]
        if not endpoint.documented:
            raise ValueError(f"{capability} is not a verified public UTA v3 capability")
        query = urlencode(params or {})
        url = f"{self.base_url}{endpoint.path}" + (f"?{query}" if query else "")
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "arx-public-research/1"}, method="GET")
        with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310: fixed https default, caller may fixture override
            payload = json.loads(response.read().decode("utf-8"))
        received_at = datetime.now(timezone.utc)
        if not isinstance(payload, dict) or payload.get("code") != "00000":
            raise RuntimeError(f"Bitget public API rejected {capability}: {payload.get('code')} {payload.get('msg')}")
        self._validate_identity(payload, params or {}, capability)
        return payload, received_at

    @staticmethod
    def _validate_identity(payload: Mapping[str, Any], params: Mapping[str, str | int], capability: str) -> None:
        """Reject a nonempty response that claims a different requested identity."""
        symbol, category = params.get("symbol"), params.get("category")
        for item in _response_items(payload):
            if symbol is not None and item.get("symbol") not in {None, str(symbol)}:
                raise RuntimeError(f"Bitget {capability} returned unexpected symbol {item.get('symbol')!r}")
            if category is not None and item.get("category") not in {None, str(category)}:
                raise RuntimeError(f"Bitget {capability} returned unexpected category {item.get('category')!r}")

    def _market(self, capability: str, category: str, symbol: str, **extra: str | int) -> tuple[dict[str, Any], datetime]:
        return self.get(capability, {"category": category, "symbol": symbol, **extra})

    def futures_instruments(self) -> tuple[dict[str, Any], datetime]:
        return self._market("instruments", FUTURES_CATEGORY, FUTURES_SYMBOL)

    def spot_instruments(self) -> tuple[dict[str, Any], datetime]:
        return self._market("instruments", SPOT_CATEGORY, SPOT_SYMBOL)

    def futures_ticker(self, symbol: str = FUTURES_SYMBOL) -> tuple[dict[str, Any], datetime]:
        return self._market("tickers", FUTURES_CATEGORY, symbol)

    def spot_ticker(self, symbol: str = SPOT_SYMBOL) -> tuple[dict[str, Any], datetime]:
        return self._market("tickers", SPOT_CATEGORY, symbol)

    def futures_funding_current(self) -> tuple[dict[str, Any], datetime]:
        return self._market("funding_current", FUTURES_CATEGORY, FUTURES_SYMBOL)

    def futures_funding_history(self, *, cursor: int = 1, limit: int = 100) -> tuple[dict[str, Any], datetime]:
        return self._market("funding_history", FUTURES_CATEGORY, FUTURES_SYMBOL, cursor=cursor, limit=limit)

    def futures_position_tiers(self) -> tuple[dict[str, Any], datetime]:
        return self._market("position_tiers", FUTURES_CATEGORY, FUTURES_SYMBOL)

    def futures_open_interest(self) -> tuple[dict[str, Any], datetime]:
        return self._market("open_interest", FUTURES_CATEGORY, FUTURES_SYMBOL)

    def futures_orderbook(self, *, limit: int = 50) -> tuple[dict[str, Any], datetime]:
        return self._market("orderbook", FUTURES_CATEGORY, FUTURES_SYMBOL, limit=limit)

    def spot_orderbook(self, *, limit: int = 50) -> tuple[dict[str, Any], datetime]:
        return self._market("orderbook", SPOT_CATEGORY, SPOT_SYMBOL, limit=limit)

    def futures_candles(self, *, interval: str = "1m", candle_type: str = "market", limit: int = 100) -> tuple[dict[str, Any], datetime]:
        return self._market("candles", FUTURES_CATEGORY, FUTURES_SYMBOL, interval=interval, type=candle_type, limit=limit)

    def spot_candles(self, *, interval: str = "1m", candle_type: str = "market", limit: int = 100) -> tuple[dict[str, Any], datetime]:
        return self._market("candles", SPOT_CATEGORY, SPOT_SYMBOL, interval=interval, type=candle_type, limit=limit)

    def futures_fills(self, *, limit: int = 100) -> tuple[dict[str, Any], datetime]:
        return self._market("fills", FUTURES_CATEGORY, FUTURES_SYMBOL, limit=limit)

    def spot_fills(self, *, limit: int = 100) -> tuple[dict[str, Any], datetime]:
        return self._market("fills", SPOT_CATEGORY, SPOT_SYMBOL, limit=limit)

    def futures_liquidations(self, *, cursor: str | None = None, limit: int = 100) -> tuple[dict[str, Any], datetime]:
        extra: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            extra["cursor"] = cursor
        return self._market("liquidations", FUTURES_CATEGORY, FUTURES_SYMBOL, **extra)
