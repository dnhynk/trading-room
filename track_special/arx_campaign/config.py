"""Strict, dependency-free configuration validation for the ARX campaign."""
from __future__ import annotations

import hashlib, os
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import OperatingMode, decimal

LIVE_REQUIRED = ("api_family", "capital_budget_usdt", "base_leverage", "leverage_cap",
 "first_entry_risk_pct_of_E0", "aggregate_stop_loss_cap_pct_of_E0", "gross_stop_risk_cap_pct_of_E0",
 "gross_notional_cap_multiple_of_E0", "isolated_margin_cap_pct_of_E0", "daily_loss_trigger_pct_of_E0",
 "weekly_loss_trigger_pct_of_E0", "campaign_loss_trigger_pct_of_E0", "equity_drawdown_trigger_pct_of_E0",
 "consecutive_losing_cycles_limit", "cooldown_after_loss_streak_hours", "max_entry_stages",
 "stage_notional_cap_fractions", "realized_profit_reuse_fraction", "realized_profit_reserve_fraction",
 "max_funding_cost_pct_of_E0", "probe_max_holding_hours", "campaign_max_holding_hours",
 "liquidation_buffer_policy", "emergency_exit_policy", "campaign_end_at", "timezone", "state_directory")

@dataclass(frozen=True)
class CampaignConfig:
    raw: dict[str, Any]
    mode: OperatingMode
    config_hash: str
    state_directory: Path

    @property
    def live_permitted(self) -> bool:
        return False  # Deliberate development hard-stop; a separately reviewed adapter is required.

def _read(path: Path) -> dict[str, Any]:
    # Repository templates are JSON-shaped YAML; refusing loose YAML keeps parsing deterministic.
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValueError(f"configuration must be JSON-compatible: {exc}") from exc

def validate(raw: dict[str, Any]) -> CampaignConfig:
    try: mode = OperatingMode(raw["mode"])
    except (KeyError, ValueError) as exc: raise ValueError("mode must be observe, paper, replay, or live") from exc
    if raw.get("schema_version") != 1 or raw.get("venue") != "bitget": raise ValueError("unsupported campaign configuration")
    if raw.get("api_family") not in {"uta_v3", None}: raise ValueError("only Bitget UTA v3 is supported; Classic v2 is forbidden")
    i, a = raw.get("instrument", {}), raw.get("account", {})
    if (i.get("symbol"), i.get("category"), i.get("base_coin"), i.get("quote_coin"), i.get("settlement_coin"), i.get("contract_type"), i.get("linear_required")) != ("ARXUSDT", "USDT-FUTURES", "ARX", "USDT", "USDT", "perpetual", True):
        raise ValueError("instrument must be ARXUSDT USDT-FUTURES linear perpetual")
    if (a.get("margin_mode_required"), a.get("position_mode_required"), a.get("margin_coin_required"), a.get("auto_margin_top_up_allowed"), a.get("multi_asset_collateral_allowed"), a.get("borrowing_allowed"), a.get("transfers_allowed")) != ("isolated", "one_way", "USDT", False, False, False, False):
        raise ValueError("account controls must require isolated one-way USDT without top-up/collateral/borrowing/transfers")
    if mode is not OperatingMode.LIVE and raw.get("live_enabled") is not False: raise ValueError("non-live modes require live_enabled=false")
    if mode is OperatingMode.LIVE:
        if raw.get("live_enabled") is not True: raise ValueError("live requires explicit live_enabled=true")
        missing = [k for k in LIVE_REQUIRED if raw.get(k) is None]
        approval = raw.get("approval") or {}
        absent = missing + ["approval."+k for k in ("approved_by", "approved_at", "config_hash") if approval.get(k) is None]
        if absent: raise ValueError("live configuration is incomplete (template remains unchanged): " + ", ".join(absent))
    def numbers(value: Any, key: str=""):
        if isinstance(value, dict):
            for k,v in value.items(): numbers(v,k)
        elif isinstance(value, list):
            for v in value: numbers(v,key)
        elif value is not None and (key.endswith(("_usdt", "_pct_of_E0", "_multiple_of_E0", "_fraction", "_rate", "_bps")) or key in {"base_leverage","leverage_cap"}):
            d=decimal(value)
            if d < 0: raise ValueError(f"{key} cannot be negative")
            if "pct" in key and d > 100: raise ValueError(f"{key} exceeds 100")
    numbers(raw)
    stages=raw.get("stage_notional_cap_fractions")
    if stages is not None:
        vals=[decimal(x) for x in stages]
        if not vals or any(x <= 0 or x > 1 for x in vals) or sum(vals) > 1: raise ValueError("stage_notional_cap_fractions must partition no more than 1")
    profile=raw.get("research_profile")
    if mode is OperatingMode.LIVE and (not isinstance(profile,str) or profile == "aggressive_bounded_research"): raise ValueError("live profile must be explicitly approved, never copied from research")
    digest_raw=json.loads(json.dumps(raw))
    if isinstance(digest_raw.get("approval"),dict): digest_raw["approval"]["config_hash"]=None
    digest = hashlib.sha256(json.dumps(digest_raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if mode is OperatingMode.LIVE and raw["approval"]["config_hash"] != digest: raise ValueError("approval config_hash does not match configuration")
    home=os.environ.get("TRADING_ROOM_HOME")
    base=Path(home) if home else Path.cwd().resolve().parent / "trading-room-state"
    if not base.is_absolute(): raise ValueError("external state directory must be absolute")
    # Config templates cannot redirect state into the repository.
    state=base / "track-special-arx"
    return CampaignConfig(raw, mode, digest, state)

def load(path: str | Path) -> CampaignConfig: return validate(_read(Path(path)))
