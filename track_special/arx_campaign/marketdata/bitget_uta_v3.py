"""Small stdlib-only client for Bitget UTA v3 public GET market data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.bitget.com"
FUTURES_CATEGORY = "USDT-FUTURES"
FUTURES_SYMBOL = "ARXUSDT"
SPOT_SYMBOL = "ARXUSDT"


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
    "ticker": EndpointCapability("ticker", "/api/v3/market/ticker", True, True, True, "Last and best bid/ask; field availability varies."),
    "orderbook": EndpointCapability("orderbook", "/api/v3/market/orderbook", True, True, True, "Snapshot only; no implied sequence continuity."),
    "funding_current": EndpointCapability("funding_current", "/api/v3/market/current-fund-rate", True, True, False, "Futures funding current rate."),
    "funding_history": EndpointCapability("funding_history", "/api/v3/market/history-fund-rate", True, True, False, "Historical futures funding."),
    "open_interest": EndpointCapability("open_interest", "/api/v3/market/open-interest", True, True, False, "Open-interest snapshot."),
    "candles": EndpointCapability("candles", "/api/v3/market/candles", True, True, True, "Completed status must be established from interval/end time."),
    "trades": EndpointCapability("trades", "/api/v3/market/fills", True, True, True, "Public completed trades."),
    # Retained as unsupported until a current official UTA public document is pinned.
    "position_tiers": EndpointCapability("position_tiers", "", False, False, False, "No public UTA v3 route verified; do not synthesize tiers."),
}


class BitgetUtaV3PublicClient:
    """GET-only client.  It has no signing, credential, or write capability."""

    def __init__(self, base_url: str = BASE_URL, timeout_seconds: float = 10.0) -> None:
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
        if payload.get("code") != "00000":
            raise RuntimeError(f"Bitget public API rejected {capability}: {payload.get('code')} {payload.get('msg')}")
        return payload, received_at

    def futures_instruments(self) -> tuple[dict[str, Any], datetime]:
        return self.get("instruments", {"category": FUTURES_CATEGORY, "symbol": FUTURES_SYMBOL})

    def spot_instruments(self) -> tuple[dict[str, Any], datetime]:
        return self.get("instruments", {"category": "SPOT", "symbol": SPOT_SYMBOL})

    def futures_ticker(self) -> tuple[dict[str, Any], datetime]:
        return self.get("ticker", {"category": FUTURES_CATEGORY, "symbol": FUTURES_SYMBOL})

    def spot_ticker(self) -> tuple[dict[str, Any], datetime]:
        return self.get("ticker", {"category": "SPOT", "symbol": SPOT_SYMBOL})

    def futures_funding_current(self) -> tuple[dict[str, Any], datetime]:
        return self.get("funding_current", {"category": FUTURES_CATEGORY, "symbol": FUTURES_SYMBOL})

    def futures_funding_history(self, *, page_no: int = 1, page_size: int = 100) -> tuple[dict[str, Any], datetime]:
        return self.get("funding_history", {"category": FUTURES_CATEGORY, "symbol": FUTURES_SYMBOL, "pageNo": page_no, "pageSize": page_size})

    def futures_open_interest(self) -> tuple[dict[str, Any], datetime]:
        return self.get("open_interest", {"category": FUTURES_CATEGORY, "symbol": FUTURES_SYMBOL})

    def futures_orderbook(self, *, limit: int = 50) -> tuple[dict[str, Any], datetime]:
        return self.get("orderbook", {"category": FUTURES_CATEGORY, "symbol": FUTURES_SYMBOL, "limit": limit})

    def spot_orderbook(self, *, limit: int = 50) -> tuple[dict[str, Any], datetime]:
        return self.get("orderbook", {"category": "SPOT", "symbol": SPOT_SYMBOL, "limit": limit})

    def futures_candles(self, granularity: str, start_time: int, end_time: int) -> tuple[dict[str, Any], datetime]:
        return self.get("candles", {"category": FUTURES_CATEGORY, "symbol": FUTURES_SYMBOL, "granularity": granularity, "startTime": start_time, "endTime": end_time})

    def spot_candles(self, granularity: str, start_time: int, end_time: int) -> tuple[dict[str, Any], datetime]:
        return self.get("candles", {"category": "SPOT", "symbol": SPOT_SYMBOL, "granularity": granularity, "startTime": start_time, "endTime": end_time})

    def futures_trades(self, *, limit: int = 100) -> tuple[dict[str, Any], datetime]:
        return self.get("trades", {"category": FUTURES_CATEGORY, "symbol": FUTURES_SYMBOL, "limit": limit})

    def spot_trades(self, *, limit: int = 100) -> tuple[dict[str, Any], datetime]:
        return self.get("trades", {"category": "SPOT", "symbol": SPOT_SYMBOL, "limit": limit})
