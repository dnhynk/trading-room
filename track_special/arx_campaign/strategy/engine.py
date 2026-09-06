"""Pure intent generation for the long-only ARX campaign.

This module deliberately has no clock, exchange, or persistence dependency.
Callers append completed bars and pass their own UTC ``now`` when evaluating.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from ..contracts import CampaignState, OrderPurpose, utc

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Bar:
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    benchmark_close: Decimal
    completed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "opened_at", utc(self.opened_at))
        object.__setattr__(self, "closed_at", utc(self.closed_at))
        if self.closed_at <= self.opened_at or min(self.open, self.high, self.low, self.close) <= ZERO:
            raise ValueError("bar times and prices must be positive and ordered")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    box_lookback: int = 20
    pivot_left: int = 2
    pivot_right: int = 2
    volatility_lookback: int = 14
    contraction_ratio_max: Decimal = Decimal("0.70")
    relative_strength_lookback: int = 12
    relative_strength_min: Decimal = ZERO
    probe_max_holding: timedelta = timedelta(hours=24)
    campaign_max_holding: timedelta = timedelta(hours=168)
    cooldown: timedelta = timedelta(hours=48)
    harvest_fractions: tuple[Decimal, Decimal, Decimal] = (Decimal("0.15"), Decimal("0.15"), Decimal("0.70"))

    def __post_init__(self) -> None:
        if self.box_lookback < 2 or self.pivot_left < 1 or self.pivot_right < 1:
            raise ValueError("lookbacks must permit a confirmed pivot")
        if not ZERO < self.contraction_ratio_max <= Decimal("1"):
            raise ValueError("contraction ratio must be in (0, 1]")
        if sum(self.harvest_fractions) != Decimal("1"):
            raise ValueError("harvest fractions must exactly sum to one")


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    state: CampaignState
    purpose: OrderPurpose | None
    reason_codes: tuple[str, ...]
    stage: int | None = None
    reduce_fraction: Decimal | None = None
    protective_stop: Decimal | None = None


@dataclass(slots=True)
class CampaignStrategy:
    config: StrategyConfig
    state: CampaignState = CampaignState.WATCH
    state_since: datetime | None = None
    reference_quantity: Decimal = ZERO
    harvest_index: int = 0
    highest_close: Decimal = ZERO
    protective_stop: Decimal | None = None
    _last_build_confirmation: datetime | None = field(default=None, init=False)

    def _completed(self, bars: Sequence[Bar], now: datetime) -> list[Bar]:
        now = utc(now)
        return [b for b in bars if b.completed and b.closed_at <= now]

    def _transition(self, state: CampaignState, now: datetime) -> None:
        self.state = state
        self.state_since = utc(now)

    def _box(self, bars: Sequence[Bar]) -> tuple[Decimal, Decimal] | None:
        if len(bars) < self.config.box_lookback:
            return None
        sample = bars[-self.config.box_lookback:]
        return min(b.low for b in sample), max(b.high for b in sample)

    def _confirmed_pivot_low(self, bars: Sequence[Bar]) -> bool:
        # The right side is already closed; no future candle is consulted.
        n = self.config.pivot_left + self.config.pivot_right + 1
        if len(bars) < n:
            return False
        pivot_at = len(bars) - self.config.pivot_right - 1
        pivot = bars[pivot_at].low
        left = bars[pivot_at - self.config.pivot_left:pivot_at]
        right = bars[pivot_at + 1:pivot_at + 1 + self.config.pivot_right]
        return all(pivot < b.low for b in left + right)

    def _contracted(self, bars: Sequence[Bar]) -> bool:
        n = self.config.volatility_lookback
        if len(bars) < n * 2:
            return False
        recent = sum((b.high - b.low for b in bars[-n:]), ZERO) / Decimal(n)
        prior = sum((b.high - b.low for b in bars[-2 * n:-n]), ZERO) / Decimal(n)
        return prior > ZERO and recent / prior <= self.config.contraction_ratio_max

    def _relative_strength(self, bars: Sequence[Bar]) -> bool:
        n = self.config.relative_strength_lookback
        if len(bars) < n + 1 or bars[-n - 1].benchmark_close <= ZERO:
            return False
        asset = bars[-1].close / bars[-n - 1].close - Decimal("1")
        benchmark = bars[-1].benchmark_close / bars[-n - 1].benchmark_close - Decimal("1")
        return asset - benchmark >= self.config.relative_strength_min

    def evaluate(
        self, bars: Sequence[Bar], now: datetime, position_quantity: Decimal,
        current_stop: Decimal | None = None, existing_profitable_after_costs: bool = False,
        next_stage: int | None = None, stopped_out: bool = False,
    ) -> StrategyDecision:
        now = utc(now)
        closed = self._completed(bars, now)
        reasons: list[str] = ["COMPLETED_BARS_ONLY"]
        if self.state_since is None:
            self.state_since = now
        if stopped_out:
            self._transition(CampaignState.COOLDOWN, now)
            return StrategyDecision(self.state, OrderPurpose.REQUESTED_EXIT, ("STOPPED_OUT", "COOLDOWN_STARTED"))
        if self.state is CampaignState.COOLDOWN:
            if now - self.state_since < self.config.cooldown:
                return StrategyDecision(self.state, None, ("COOLDOWN_ACTIVE",))
            self._transition(CampaignState.WATCH, now)
        if position_quantity <= ZERO:
            self.reference_quantity = ZERO
            self.harvest_index = 0
            self.protective_stop = None
            if self.state not in {CampaignState.WATCH, CampaignState.COOLDOWN}:
                self._transition(CampaignState.FLAT, now)
                return StrategyDecision(self.state, None, ("POSITION_FLAT",))
            box = self._box(closed)
            if not box or not self._confirmed_pivot_low(closed) or not self._contracted(closed) or not self._relative_strength(closed):
                return StrategyDecision(CampaignState.WATCH, None, tuple(reasons + ["PROBE_CONDITIONS_INCOMPLETE"]))
            if closed[-1].close <= box[1]:
                return StrategyDecision(CampaignState.WATCH, None, tuple(reasons + ["BOX_NOT_BROKEN"]))
            self._transition(CampaignState.PROBE, now)
            return StrategyDecision(self.state, OrderPurpose.PROBE_ENTRY, tuple(reasons + ["BOX_CONFIRMED", "PIVOT_CONFIRMED", "VOLATILITY_CONTRACTED", "RELATIVE_STRENGTH_OK", "BREAKOUT_CONFIRMED"]), 1)
        if self.reference_quantity <= ZERO:
            self.reference_quantity = position_quantity
        self.highest_close = max(self.highest_close, closed[-1].close if closed else ZERO)
        if self.state is CampaignState.PROBE and now - self.state_since >= self.config.probe_max_holding:
            self._transition(CampaignState.HARVEST, now)
            return StrategyDecision(self.state, OrderPurpose.TAKE_PROFIT, ("PROBE_TIMEOUT",), reduce_fraction=Decimal("1"))
        if now - self.state_since >= self.config.campaign_max_holding:
            self._transition(CampaignState.HARVEST, now)
            return StrategyDecision(self.state, OrderPurpose.TAKE_PROFIT, ("CAMPAIGN_TIMEOUT",), reduce_fraction=Decimal("1"))
        # A protective stop can only move upward.
        if current_stop is not None:
            if self.protective_stop is not None and current_stop < self.protective_stop:
                return StrategyDecision(self.state, None, ("STOP_LOWERING_REJECTED",), protective_stop=self.protective_stop)
            self.protective_stop = max(self.protective_stop or current_stop, current_stop)
        if self.state in {CampaignState.PROBE, CampaignState.BUILD} and existing_profitable_after_costs and next_stage and next_stage > 1:
            marker = closed[-1].closed_at if closed else None
            if marker and marker != self._last_build_confirmation and self._relative_strength(closed) and self._confirmed_pivot_low(closed):
                self._last_build_confirmation = marker
                self._transition(CampaignState.BUILD, now)
                return StrategyDecision(self.state, OrderPurpose.PYRAMID_ENTRY, ("WINNER_ONLY", "NEW_CONFIRMATION", "PIVOT_CONFIRMED", "RELATIVE_STRENGTH_OK"), next_stage, protective_stop=self.protective_stop)
        if self.state is CampaignState.BUILD:
            self._transition(CampaignState.RIDE, now)
        return StrategyDecision(self.state if self.state is not CampaignState.WATCH else CampaignState.RIDE, None, ("RIDE_TREND",), protective_stop=self.protective_stop)
