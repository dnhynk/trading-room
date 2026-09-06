"""Strict, dependency-free configuration validation for the ARX campaign."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import OperatingMode, decimal


LIVE_REQUIRED = (
    "api_family",
    "capital_budget_usdt",
    "base_leverage",
    "leverage_cap",
    "first_entry_risk_pct_of_E0",
    "aggregate_stop_loss_cap_pct_of_E0",
    "gross_stop_risk_cap_pct_of_E0",
    "gross_notional_cap_multiple_of_E0",
    "isolated_margin_cap_pct_of_E0",
    "daily_loss_trigger_pct_of_E0",
    "weekly_loss_trigger_pct_of_E0",
    "campaign_loss_trigger_pct_of_E0",
    "equity_drawdown_trigger_pct_of_E0",
    "consecutive_losing_cycles_limit",
    "cooldown_after_loss_streak_hours",
    "max_entry_stages",
    "stage_notional_cap_fractions",
    "realized_profit_reuse_fraction",
    "realized_profit_reserve_fraction",
    "max_funding_cost_pct_of_E0",
    "probe_max_holding_hours",
    "campaign_max_holding_hours",
    "liquidation_buffer_policy",
    "emergency_exit_policy",
    "campaign_end_at",
    "timezone",
    "state_directory",
)


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    raw: dict[str, Any]
    mode: OperatingMode
    config_hash: str
    state_directory: Path
    live_issues: tuple[str, ...]

    @property
    def live_permitted(self) -> bool:
        # A reviewed authenticated adapter does not exist in this build.
        return False


def _read(path: Path) -> dict[str, Any]:
    # Repository templates are JSON-shaped YAML; refusing loose YAML keeps
    # parsing deterministic and avoids an optional loader in the order path.
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"configuration must be JSON-compatible: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    return value


def _state_directory(raw: dict[str, Any]) -> Path:
    explicit = raw.get("state_directory")
    if explicit:
        state = Path(str(explicit))
    else:
        configured_home = os.environ.get("TRADING_ROOM_HOME")
        base = (
            Path(configured_home)
            if configured_home
            else Path(__file__).resolve().parents[2].parent / "trading-room-state"
        )
        state = base / "track-special-arx"
    if not state.is_absolute():
        raise ValueError("external state directory must be absolute")
    resolved = state.resolve()
    repository = Path(__file__).resolve().parents[2]
    if resolved == repository or resolved.is_relative_to(repository):
        raise ValueError("campaign state cannot be stored inside the source repository")
    return resolved


def _validate_numbers(value: Any, key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            _validate_numbers(child, child_key)
        return
    if isinstance(value, list):
        for child in value:
            _validate_numbers(child, key)
        return
    numeric_key = key.endswith(
        ("_usdt", "_pct_of_E0", "_multiple_of_E0", "_fraction", "_rate", "_bps")
    ) or key in {"base_leverage", "leverage_cap"}
    if value is None or not numeric_key:
        return
    number = decimal(value)
    if number < 0:
        raise ValueError(f"{key} cannot be negative")
    if "pct" in key and number > 100:
        raise ValueError(f"{key} exceeds 100")


def validate(raw: dict[str, Any]) -> CampaignConfig:
    try:
        mode = OperatingMode(raw["mode"])
    except (KeyError, ValueError) as exc:
        raise ValueError("mode must be observe, paper, replay, or live") from exc
    if raw.get("schema_version") != 1 or raw.get("venue") != "bitget":
        raise ValueError("unsupported campaign configuration")
    if raw.get("api_family") not in {"uta_v3", None}:
        raise ValueError("only Bitget UTA v3 is supported; Classic v2 is forbidden")

    instrument = raw.get("instrument", {})
    expected_instrument = (
        "ARXUSDT",
        "USDT-FUTURES",
        "ARX",
        "USDT",
        "USDT",
        "perpetual",
        True,
    )
    actual_instrument = (
        instrument.get("symbol"),
        instrument.get("category"),
        instrument.get("base_coin"),
        instrument.get("quote_coin"),
        instrument.get("settlement_coin"),
        instrument.get("contract_type"),
        instrument.get("linear_required"),
    )
    if actual_instrument != expected_instrument:
        raise ValueError("instrument must be ARXUSDT USDT-FUTURES linear perpetual")

    account = raw.get("account", {})
    expected_account = ("isolated", "one_way", "USDT", False, False, False, False)
    actual_account = (
        account.get("margin_mode_required"),
        account.get("position_mode_required"),
        account.get("margin_coin_required"),
        account.get("auto_margin_top_up_allowed"),
        account.get("multi_asset_collateral_allowed"),
        account.get("borrowing_allowed"),
        account.get("transfers_allowed"),
    )
    if actual_account != expected_account:
        raise ValueError(
            "account controls must require isolated one-way USDT without "
            "top-up/collateral/borrowing/transfers"
        )
    if mode is not OperatingMode.LIVE and raw.get("live_enabled") is not False:
        raise ValueError("non-live modes require live_enabled=false")

    _validate_numbers(raw)
    issues: list[str] = []
    if mode is OperatingMode.LIVE:
        if raw.get("live_enabled") is not True:
            issues.append("LIVE_DISABLED")
        missing = [key for key in LIVE_REQUIRED if raw.get(key) is None]
        issues.extend(f"MISSING_{key.upper()}" for key in missing)
        approval = raw.get("approval") or {}
        issues.extend(
            f"MISSING_APPROVAL_{key.upper()}"
            for key in ("approved_by", "approved_at", "config_hash")
            if approval.get(key) is None
        )
        if raw.get("research_profile") == "aggressive_bounded_research":
            issues.append("RESEARCH_PROFILE_NOT_LIVE_APPROVED")
        stages = raw.get("stage_notional_cap_fractions")
        if stages is not None:
            values = [decimal(item) for item in stages]
            if (
                len(values) != raw.get("max_entry_stages")
                or any(item <= 0 or item > 1 for item in values)
                or sum(values) != Decimal("1")
            ):
                issues.append("INVALID_STAGE_FRACTIONS")
        reuse = raw.get("realized_profit_reuse_fraction")
        reserve = raw.get("realized_profit_reserve_fraction")
        if reuse is not None and reserve is not None and decimal(reuse) + decimal(reserve) != 1:
            issues.append("INVALID_PROFIT_ALLOCATION")
        if raw.get("base_leverage") is not None and raw.get("leverage_cap") is not None:
            if not ZERO < decimal(raw["base_leverage"]) <= decimal(raw["leverage_cap"]):
                issues.append("INVALID_LEVERAGE_RANGE")

    digest_source = json.loads(json.dumps(raw))
    if isinstance(digest_source.get("approval"), dict):
        digest_source["approval"]["config_hash"] = None
    digest = hashlib.sha256(
        json.dumps(digest_source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if mode is OperatingMode.LIVE:
        supplied_hash = (raw.get("approval") or {}).get("config_hash")
        if supplied_hash is not None and supplied_hash != digest:
            issues.append("APPROVAL_CONFIG_HASH_MISMATCH")
        # A config that claims live activation must be complete, but the null
        # example remains loadable so validate-live can report every blocker.
        if raw.get("live_enabled") is True and issues:
            raise ValueError("live configuration blocked: " + ", ".join(issues))

    return CampaignConfig(raw, mode, digest, _state_directory(raw), tuple(issues))


ZERO = Decimal("0")


def load(path: str | Path) -> CampaignConfig:
    return validate(_read(Path(path)))
