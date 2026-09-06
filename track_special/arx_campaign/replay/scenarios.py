"""Named synthetic failure scenarios. They are never return evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .engine import ReplayBar


@dataclass(frozen=True, slots=True)
class ChaosScenario:
    name: str
    bars: tuple[ReplayBar, ...]
    operational_events: tuple[str, ...]
    profitability_evidence: bool = False


def _bar(
    open_: str,
    high: str,
    low: str,
    close: str,
    bid: str,
    ask: str,
    **values: object,
) -> ReplayBar:
    return ReplayBar(
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        Decimal(bid),
        Decimal(ask),
        **values,  # type: ignore[arg-type]
    )


SCENARIOS = {
    "long_decline": ChaosScenario(
        "long_decline",
        (_bar("10", "10.1", "8", "8.2", "8.1", "10.05", mark=Decimal("8.2")),),
        ("trend_invalidated",),
    ),
    "pump_absent": ChaosScenario(
        "pump_absent",
        (_bar("10", "10.2", "9.7", "9.9", "9.85", "10.05", mark=Decimal("9.9")),),
        ("no_price_progress",),
    ),
    "probe_only_then_rally": ChaosScenario(
        "probe_only_then_rally",
        (_bar("10", "13", "9.8", "12", "11.8", "12.1", ask_depth=Decimal("1"), mark=Decimal("12")),),
        ("partial_probe_fill", "unfilled_add_not_caught_up"),
    ),
    "fake_breakout_after_add": ChaosScenario(
        "fake_breakout_after_add",
        (_bar("10", "12", "8.5", "9", "8.8", "10.1", mark=Decimal("9")),),
        ("add_then_false_breakout",),
    ),
    "gap_collapse": ChaosScenario(
        "gap_collapse",
        (_bar("10", "10.1", "7", "7.5", "7.2", "10.1", delay_bps=Decimal("100"), mark=Decimal("7.4"), liquidation_price=Decimal("7.8")),),
        ("gap_through_stop",),
    ),
    "mark_last_divergence": ChaosScenario(
        "mark_last_divergence",
        (_bar("10", "11", "9", "10.5", "10.4", "10.6", mark=Decimal("9"), last=Decimal("10.5")),),
        ("mark_last_divergence",),
    ),
    "funding_spike_interval_change": ChaosScenario(
        "funding_spike_interval_change",
        (_bar("10", "10.2", "9.8", "10", "9.95", "10.05", funding_rate=Decimal("0.01"), funding_settlement=True, mark=Decimal("10")),),
        ("funding_spike", "interval_changed"),
    ),
    "trading_halt": ChaosScenario(
        "trading_halt",
        (_bar("10", "10", "10", "10", "9.9", "10.1", bid_depth=Decimal("0"), ask_depth=Decimal("0"), mark=Decimal("10")),),
        ("venue_trading_halt", "exposure_may_remain"),
    ),
    "server_stop_rejected": ChaosScenario(
        "server_stop_rejected",
        (_bar("10", "10.1", "9", "9.2", "9.1", "10.05", mark=Decimal("9.2")),),
        ("server_stop_rejected", "pause_entries", "emergency_reduction_requested"),
    ),
    "residual_orders_after_liquidation": ChaosScenario(
        "residual_orders_after_liquidation",
        (_bar("10", "10", "6", "7", "6.8", "10.1", mark=Decimal("7"), liquidation_price=Decimal("7.5")),),
        ("liquidated", "residual_reduce_orders_detected"),
    ),
    "adl_forced_reduction": ChaosScenario(
        "adl_forced_reduction",
        (_bar("10", "12", "9.5", "11", "10.9", "11.1", mark=Decimal("11")),),
        ("adl_or_forced_reduction", "position_reconcile"),
    ),
    "restart_duplicate_order": ChaosScenario(
        "restart_duplicate_order",
        (_bar("10", "10.2", "9.8", "10", "9.9", "10.1", mark=Decimal("10")),),
        ("result_unknown", "restart", "client_oid_reconcile_before_retry"),
    ),
}


def get_scenario(name: str) -> ChaosScenario:
    try:
        return SCENARIOS[name]
    except KeyError as exc:
        raise ValueError(f"unknown synthetic scenario: {name}") from exc
