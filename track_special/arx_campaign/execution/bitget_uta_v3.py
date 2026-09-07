"""Strict UTA-v3 private-read boundary and account observation parser.

This module contains no signer and no private-write method. A future live
adapter must be a separate reviewed change; Classic-v2 fields are not accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from ..contracts import (
    AccountMode,
    AccountSnapshot,
    ApiFamily,
    MarginMode,
    PositionMode,
)


@dataclass(frozen=True, slots=True)
class PrivateCapability:
    name: str
    method: str
    path: str
    permission: str


READ_CAPABILITIES = {
    "account_settings": PrivateCapability(
        "account_settings", "GET", "/api/v3/account/settings", "UTA mgt. (read)"
    ),
    "positions": PrivateCapability(
        "positions",
        "GET",
        "/api/v3/position/current-position",
        "UTA trade (read)",
    ),
}

DOCUMENTED_WRITE_ENDPOINTS_NOT_IMPLEMENTED = {
    "place_order": "/api/v3/trade/place-order",
    "place_strategy_order": "/api/v3/trade/place-strategy-order",
}


class ReadOnlyPrivateProbe:
    """Uses a caller-owned signed GET transport and exposes no POST operation."""

    def __init__(
        self,
        signed_get: Callable[[str, Mapping[str, str]], Mapping[str, Any]],
    ) -> None:
        self._signed_get = signed_get

    def account_settings(self) -> Mapping[str, Any]:
        return self._signed_get(READ_CAPABILITIES["account_settings"].path, {})

    def positions(self) -> Mapping[str, Any]:
        return self._signed_get(
            READ_CAPABILITIES["positions"].path,
            {"category": "USDT-FUTURES", "symbol": "ARXUSDT"},
        )


def account_snapshot_from_uta_settings(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime,
    strategy_equity_usdt: Decimal | None,
    available_usdt: Decimal | None,
    margin_coin: str | None,
    reconciled: bool,
    dedicated_or_verifiably_separated: bool,
    external_exposure_detected: bool,
    auto_margin_top_up_disabled: bool | None,
) -> AccountSnapshot:
    """Map only documented UTA fields; missing controls remain unverified."""

    if payload.get("code") != "00000" or not isinstance(payload.get("data"), Mapping):
        raise ValueError("not a successful UTA account-settings response")
    data = payload["data"]
    account_mode_value = data.get("accountMode")
    level = data.get("accountLevel")
    if account_mode_value not in {"unified", "hybrid"}:
        api_family = ApiFamily.UNVERIFIED
        account_mode = AccountMode.UNVERIFIED
    else:
        api_family = ApiFamily.UTA_V3
        account_mode = {
            "isolated": AccountMode.UTA_ISOLATED,
            "basic": AccountMode.UTA_STANDARD,
            "advanced": AccountMode.UTA_ADVANCED,
        }.get(level, AccountMode.UNVERIFIED)

    hold_mode = {
        "one_way_mode": PositionMode.ONE_WAY,
        "hedge_mode": PositionMode.HEDGE,
    }.get(data.get("holdMode"), PositionMode.UNVERIFIED)
    symbol_configs = data.get("symbolConfigList")
    exact = []
    if isinstance(symbol_configs, list):
        exact = [
            item
            for item in symbol_configs
            if isinstance(item, Mapping)
            and item.get("category") == "USDT-FUTURES"
            and item.get("symbol") == "ARXUSDT"
        ]
    margin_mode = (
        {
            "isolated": MarginMode.ISOLATED,
            "crossed": MarginMode.CROSSED,
        }.get(str(exact[0].get("marginMode")), MarginMode.UNVERIFIED)
        if len(exact) == 1
        else MarginMode.UNVERIFIED
    )
    return AccountSnapshot(
        observed_at=observed_at,
        api_family=api_family,
        account_mode=account_mode,
        margin_mode=margin_mode,
        position_mode=hold_mode,
        margin_coin=margin_coin or "UNVERIFIED",
        strategy_equity_usdt=strategy_equity_usdt,
        available_usdt=available_usdt,
        reconciled=reconciled,
        dedicated_or_verifiably_separated=dedicated_or_verifiably_separated,
        external_exposure_detected=external_exposure_detected,
        auto_margin_top_up_disabled=auto_margin_top_up_disabled,
        asset_mode=str(data.get("assetMode") or "unverified"),
    )
