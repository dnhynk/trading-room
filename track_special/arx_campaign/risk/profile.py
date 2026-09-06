"""Validated research-profile parsing; profiles cannot promote themselves to live."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

from ..contracts import InstrumentSpec, decimal
from .engine import RiskLimits


@dataclass(frozen=True, slots=True)
class ResearchRiskProfile:
    name: str
    base_leverage: Decimal
    leverage_cap: Decimal
    first_entry_risk_pct: Decimal
    aggregate_stop_loss_pct: Decimal
    gross_stop_risk_pct: Decimal
    gross_notional_multiple: Decimal
    isolated_margin_pct: Decimal
    daily_loss_pct: Decimal
    weekly_loss_pct: Decimal
    campaign_loss_pct: Decimal
    equity_drawdown_pct: Decimal
    consecutive_losing_cycles_limit: int
    cooldown_hours: int
    max_entry_stages: int
    stage_fractions: tuple[Decimal, ...]
    reuse_fraction: Decimal
    reserve_fraction: Decimal
    live_approved: bool

    @classmethod
    def from_mapping(cls, name: str, value: Mapping[str, Any]) -> "ResearchRiskProfile":
        profile = cls(
            name,
            decimal(value["base_leverage"]),
            decimal(value["leverage_cap"]),
            decimal(value["first_entry_risk_pct_of_E0"]),
            decimal(value["aggregate_stop_loss_cap_pct_of_E0"]),
            decimal(value["gross_stop_risk_cap_pct_of_E0"]),
            decimal(value["gross_notional_cap_multiple_of_E0"]),
            decimal(value["isolated_margin_cap_pct_of_E0"]),
            decimal(value["daily_loss_trigger_pct_of_E0"]),
            decimal(value["weekly_loss_trigger_pct_of_E0"]),
            decimal(value["campaign_loss_trigger_pct_of_E0"]),
            decimal(value["equity_drawdown_trigger_pct_of_E0"]),
            int(value["consecutive_losing_cycles_limit"]),
            int(value["cooldown_after_loss_streak_hours"]),
            int(value["max_entry_stages"]),
            tuple(decimal(item) for item in value["stage_notional_cap_fractions"]),
            decimal(value["realized_profit_reuse_fraction"]),
            decimal(value["realized_profit_reserve_fraction"]),
            bool(value.get("live_approved", False)),
        )
        if profile.live_approved:
            raise ValueError("repository research profiles cannot be live-approved")
        if profile.base_leverage <= 0 or profile.base_leverage > profile.leverage_cap:
            raise ValueError("invalid research leverage range")
        if len(profile.stage_fractions) != profile.max_entry_stages or sum(profile.stage_fractions) != 1:
            raise ValueError("research stage fractions must exactly partition one")
        if profile.reuse_fraction + profile.reserve_fraction != 1:
            raise ValueError("research profit allocation must exactly partition one")
        return profile

    def limits(
        self,
        *,
        e0_usdt: Decimal,
        stage: int,
        instrument: InstrumentSpec,
        configured_leverage: Decimal | None = None,
        liquidation_buffer_min: Decimal,
        giveback_cap_usdt: Decimal,
        funding_cost_cap_usdt: Decimal,
        timezone_name: str = "Asia/Seoul",
    ) -> RiskLimits:
        if stage < 1 or stage > self.max_entry_stages:
            raise ValueError("stage is outside the research profile")
        leverage = configured_leverage or self.base_leverage
        if leverage <= 0 or leverage > self.leverage_cap:
            raise ValueError("configured leverage exceeds research cap")
        pct = Decimal("0.01")
        gross_notional_cap = e0_usdt * self.gross_notional_multiple
        return RiskLimits(
            leverage=leverage,
            e0=e0_usdt,
            aggregate_loss_cap=e0_usdt * self.aggregate_stop_loss_pct * pct,
            gross_stop_cap=e0_usdt * self.gross_stop_risk_pct * pct,
            gross_notional_cap=gross_notional_cap,
            isolated_margin_cap=e0_usdt * self.isolated_margin_pct * pct,
            liquidation_buffer_min=liquidation_buffer_min,
            stage_notional_cap=gross_notional_cap * self.stage_fractions[stage - 1],
            quantity_step=instrument.quantity_step,
            min_quantity=instrument.min_order_quantity,
            daily_loss_cap=e0_usdt * self.daily_loss_pct * pct,
            weekly_loss_cap=e0_usdt * self.weekly_loss_pct * pct,
            campaign_loss_cap=e0_usdt * self.campaign_loss_pct * pct,
            drawdown_cap=e0_usdt * self.equity_drawdown_pct * pct,
            loss_streak_limit=self.consecutive_losing_cycles_limit,
            first_entry_loss_cap=e0_usdt * self.first_entry_risk_pct * pct,
            giveback_cap=giveback_cap_usdt,
            funding_cost_cap=funding_cost_cap_usdt,
            max_quantity=instrument.max_limit_quantity,
            min_notional=instrument.min_order_notional,
            price_tick=instrument.price_tick,
            timezone_name=timezone_name,
            max_entry_stages=self.max_entry_stages,
            cooldown_after_loss=timedelta(hours=self.cooldown_hours),
        )


def load_research_profile(
    path: str | Path, name: str = "aggressive_bounded_research"
) -> ResearchRiskProfile:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, dict) or not isinstance(profiles.get(name), dict):
        raise ValueError(f"research profile {name!r} is unavailable")
    return ResearchRiskProfile.from_mapping(name, profiles[name])
