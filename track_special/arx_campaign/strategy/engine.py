"""Deterministic intent generation for the long-only ARX campaign."""

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
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "opened_at", utc(self.opened_at))
        object.__setattr__(self, "closed_at", utc(self.closed_at))
        if self.confirmed_at is not None:
            object.__setattr__(self, "confirmed_at", utc(self.confirmed_at))
        if (
            self.closed_at <= self.opened_at
            or self.confirmed_at is not None
            and self.confirmed_at < self.closed_at
            or min(self.open, self.high, self.low, self.close, self.benchmark_close) <= ZERO
            or self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
            or self.low > self.high
        ):
            raise ValueError("bar times and OHLC prices must be positive and ordered")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    box_lookback: int = 20
    pivot_left: int = 2
    pivot_right: int = 2
    volatility_lookback: int = 14
    contraction_ratio_max: Decimal = Decimal("0.70")
    relative_strength_lookback: int = 12
    relative_strength_min: Decimal = ZERO
    probe_min_box_position: Decimal = Decimal("0.50")
    probe_max_holding: timedelta = timedelta(hours=24)
    campaign_max_holding: timedelta = timedelta(hours=168)
    no_progress_timeout: timedelta = timedelta(hours=24)
    cooldown: timedelta = timedelta(hours=48)
    harvest_fractions: tuple[Decimal, Decimal, Decimal] = (
        Decimal("0.15"),
        Decimal("0.15"),
        Decimal("0.70"),
    )
    harvest_r_multiples: tuple[Decimal, Decimal] = (Decimal("1"), Decimal("2"))

    def __post_init__(self) -> None:
        if self.box_lookback < 2 or self.pivot_left < 1 or self.pivot_right < 1:
            raise ValueError("lookbacks must permit a confirmed pivot")
        if self.volatility_lookback < 1 or self.relative_strength_lookback < 1:
            raise ValueError("volatility and relative-strength lookbacks must be positive")
        if not ZERO < self.contraction_ratio_max <= Decimal("1"):
            raise ValueError("contraction ratio must be in (0, 1]")
        if not ZERO <= self.probe_min_box_position <= Decimal("1"):
            raise ValueError("probe box position must be in [0, 1]")
        if sum(self.harvest_fractions) != Decimal("1"):
            raise ValueError("harvest fractions must exactly sum to one")
        if len(self.harvest_r_multiples) != 2 or not (
            ZERO < self.harvest_r_multiples[0] < self.harvest_r_multiples[1]
        ):
            raise ValueError("two increasing positive harvest R thresholds are required")


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    state: CampaignState
    purpose: OrderPurpose | None
    reason_codes: tuple[str, ...]
    stage: int | None = None
    reduce_fraction: Decimal | None = None
    reduce_quantity: Decimal | None = None
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
    _next_stage: int = field(default=2, init=False)
    _harvested_quantity: Decimal = field(default=ZERO, init=False)
    _last_progress_at: datetime | None = field(default=None, init=False)

    def _completed(self, bars: Sequence[Bar], now: datetime) -> list[Bar]:
        now = utc(now)
        completed = [b for b in bars if b.completed and (b.confirmed_at or b.closed_at) <= now]
        if any(right.opened_at <= left.opened_at for left, right in zip(completed, completed[1:])):
            raise ValueError("completed bars must be unique and strictly ordered")
        return completed

    def _transition(self, state: CampaignState, now: datetime) -> None:
        self.state = state
        self.state_since = utc(now)

    def _box(self, bars: Sequence[Bar]) -> tuple[Decimal, Decimal] | None:
        if len(bars) < self.config.box_lookback:
            return None
        sample = bars[-self.config.box_lookback :]
        return min(b.low for b in sample), max(b.high for b in sample)

    def _pre_signal_box(self, bars: Sequence[Bar]) -> tuple[Decimal, Decimal] | None:
        if len(bars) <= self.config.box_lookback:
            return None
        return self._box(bars[-self.config.box_lookback - 1 : -1])

    def _breakout_confirmation(self, bars: Sequence[Bar]) -> bool:
        box = self._pre_signal_box(bars)
        return box is not None and bars[-1].close > box[1]

    def _build_confirmation(self, bars: Sequence[Bar]) -> bool:
        """Require a fresh breakout hold or a closed pullback-resumption pattern."""

        if len(bars) < 3:
            return False
        signal, prior, before = bars[-1], bars[-2], bars[-3]
        box = self._pre_signal_box(bars)
        breakout_hold = (
            box is not None
            and prior.close > box[1]
            and signal.close > box[1]
            and signal.close >= prior.close
        )
        pullback_resumption = (
            prior.close < before.close
            and signal.close > prior.high
            and signal.close > before.close
        )
        return self._breakout_confirmation(bars) or breakout_hold or pullback_resumption

    def _confirmed_pivot_low(self, bars: Sequence[Bar]) -> Decimal | None:
        """Return only a pivot whose right-hand confirmation bars are already closed."""

        width = self.config.pivot_left + self.config.pivot_right + 1
        if len(bars) < width:
            return None
        pivot_at = len(bars) - self.config.pivot_right - 1
        pivot = bars[pivot_at].low
        left = bars[pivot_at - self.config.pivot_left : pivot_at]
        right = bars[pivot_at + 1 : pivot_at + 1 + self.config.pivot_right]
        return pivot if all(pivot < bar.low for bar in (*left, *right)) else None

    def _contracted(self, bars: Sequence[Bar]) -> bool:
        lookback = self.config.volatility_lookback
        if len(bars) < lookback * 2:
            return False
        recent = sum((bar.high - bar.low for bar in bars[-lookback:]), ZERO) / Decimal(lookback)
        prior = sum((bar.high - bar.low for bar in bars[-2 * lookback : -lookback]), ZERO) / Decimal(lookback)
        return prior > ZERO and recent / prior <= self.config.contraction_ratio_max

    def _relative_strength(self, bars: Sequence[Bar]) -> bool:
        lookback = self.config.relative_strength_lookback
        if len(bars) < lookback + 1:
            return False
        asset_base = bars[-lookback - 1].close
        benchmark_base = bars[-lookback - 1].benchmark_close
        if asset_base <= ZERO or benchmark_base <= ZERO:
            return False
        asset_return = bars[-1].close / asset_base - Decimal("1")
        benchmark_return = bars[-1].benchmark_close / benchmark_base - Decimal("1")
        return asset_return - benchmark_return >= self.config.relative_strength_min

    def _probe_stabilized(self, bars: Sequence[Bar]) -> bool:
        box = self._pre_signal_box(bars)
        pivot = self._confirmed_pivot_low(bars)
        if box is None or pivot is None:
            return False
        lower, upper = box
        width = upper - lower
        if width <= ZERO:
            return False
        signal = bars[-1]
        minimum_close = lower + width * self.config.probe_min_box_position
        # A probe is deliberately pre-breakout; the first spike is not chased.
        return signal.low >= pivot and minimum_close <= signal.close <= upper

    def evaluate(
        self,
        bars: Sequence[Bar],
        now: datetime,
        position_quantity: Decimal,
        current_stop: Decimal | None = None,
        existing_profitable_after_costs: bool = False,
        next_stage: int | None = None,
        stopped_out: bool = False,
        executable_exit_price: Decimal | None = None,
        entry_costs_paid: Decimal = ZERO,
        future_exit_costs: Decimal = ZERO,
        position_cost_basis: Decimal | None = None,
        filled_reduction_quantity: Decimal = ZERO,
        pending_entry: bool = False,
        net_pnl_usdt: Decimal | None = None,
        initial_risk_unit_usdt: Decimal | None = None,
        projected_funding_cost_usdt: Decimal | None = None,
        funding_cost_cap_usdt: Decimal | None = None,
        trend_invalidated: bool = False,
        funding_exit_required: bool = False,
        giveback_exit_required: bool = False,
    ) -> StrategyDecision:
        """Evaluate known information only.

        ``filled_reduction_quantity`` is the newly reconciled fill delta, not a
        cumulative value.  The legacy profitability boolean is intentionally
        ignored; pyramiding needs executable prices and explicit costs.
        """

        del existing_profitable_after_costs
        now = utc(now)
        closed = self._completed(bars, now)
        reasons: list[str] = ["COMPLETED_BARS_ONLY"]
        if self.state_since is None:
            self.state_since = now

        if stopped_out:
            self._transition(CampaignState.COOLDOWN, now)
            return StrategyDecision(
                self.state,
                OrderPurpose.REQUESTED_EXIT,
                ("STOPPED_OUT", "COOLDOWN_STARTED"),
                reduce_quantity=max(position_quantity, ZERO),
            )
        if self.state is CampaignState.COOLDOWN:
            if now - self.state_since < self.config.cooldown:
                return StrategyDecision(self.state, None, ("COOLDOWN_ACTIVE",))
            self._transition(CampaignState.WATCH, now)

        if position_quantity <= ZERO:
            self.reference_quantity = ZERO
            self.harvest_index = 0
            self._harvested_quantity = ZERO
            self._next_stage = 2
            self.protective_stop = None
            self.highest_close = ZERO
            self._last_progress_at = None
            if pending_entry:
                return StrategyDecision(self.state, None, ("ENTRY_PENDING",))
            if self.state not in {CampaignState.WATCH, CampaignState.FLAT}:
                self._transition(CampaignState.FLAT, now)
                return StrategyDecision(self.state, None, ("POSITION_FLAT",))
            if not closed or not self._probe_stabilized(closed) or not self._contracted(closed) or not self._relative_strength(closed):
                return StrategyDecision(
                    CampaignState.WATCH,
                    None,
                    tuple(reasons + ["PROBE_CONDITIONS_INCOMPLETE"]),
                )
            self._transition(CampaignState.PROBE, now)
            return StrategyDecision(
                self.state,
                OrderPurpose.PROBE_ENTRY,
                tuple(
                    reasons
                    + [
                        "PRE_BREAKOUT_STABILIZATION",
                        "PIVOT_CONFIRMED_WITH_RIGHT_BARS",
                        "VOLATILITY_CONTRACTED",
                        "RELATIVE_STRENGTH_OK",
                        "FIRST_SPIKE_NOT_CHASED",
                    ]
                ),
                1,
            )

        if self.state in {CampaignState.WATCH, CampaignState.FLAT}:
            self._transition(CampaignState.PROBE, now)
        if filled_reduction_quantity < ZERO:
            raise ValueError("filled reduction delta cannot be negative")
        if filled_reduction_quantity > ZERO:
            self._harvested_quantity += filled_reduction_quantity
        if self.harvest_index == 0:
            self.reference_quantity = max(
                self.reference_quantity, position_quantity + self._harvested_quantity
            )

        latest_close = closed[-1].close if closed else ZERO
        if latest_close > self.highest_close:
            self.highest_close = latest_close
            self._last_progress_at = now
        if self._last_progress_at is None:
            self._last_progress_at = now

        if current_stop is not None:
            if current_stop <= ZERO:
                raise ValueError("protective stop must be positive")
            if self.protective_stop is not None and current_stop < self.protective_stop:
                return StrategyDecision(
                    self.state,
                    None,
                    ("STOP_LOWERING_REJECTED",),
                    protective_stop=self.protective_stop,
                )
            self.protective_stop = max(self.protective_stop or current_stop, current_stop)

        exit_reasons: list[str] = []
        if self.state is CampaignState.PROBE and now - self.state_since >= self.config.probe_max_holding:
            exit_reasons.append("PROBE_MAX_HOLDING")
        if now - self.state_since >= self.config.campaign_max_holding:
            exit_reasons.append("CAMPAIGN_MAX_HOLDING")
        if now - self._last_progress_at >= self.config.no_progress_timeout:
            exit_reasons.append("NO_PRICE_PROGRESS")
        if (
            projected_funding_cost_usdt is not None
            and funding_cost_cap_usdt is not None
            and projected_funding_cost_usdt >= funding_cost_cap_usdt
        ):
            exit_reasons.append("FUNDING_COST_CAP")
        if trend_invalidated:
            exit_reasons.append("TREND_INVALIDATED")
        if funding_exit_required:
            exit_reasons.append("FUNDING_EXIT")
        if giveback_exit_required:
            exit_reasons.append("PROFIT_GIVEBACK_EXIT")
        if exit_reasons:
            self._transition(CampaignState.HARVEST, now)
            return StrategyDecision(
                self.state,
                OrderPurpose.REQUESTED_EXIT,
                tuple(exit_reasons),
                reduce_fraction=Decimal("1"),
                reduce_quantity=position_quantity,
                protective_stop=self.protective_stop,
            )

        executable_profit = (
            executable_exit_price is not None
            and position_cost_basis is not None
            and position_quantity * (executable_exit_price - position_cost_basis)
            - entry_costs_paid
            - future_exit_costs
            > ZERO
        )
        if (
            self.state in {CampaignState.PROBE, CampaignState.BUILD, CampaignState.RIDE}
            and executable_profit
            and next_stage is not None
            and 1 < next_stage <= 4
            and next_stage == self._next_stage
        ):
            marker = closed[-1].closed_at if closed else None
            if (
                marker
                and marker != self._last_build_confirmation
                and self._relative_strength(closed)
                and self._build_confirmation(closed)
            ):
                self._last_build_confirmation = marker
                self._next_stage += 1
                self._transition(CampaignState.BUILD, now)
                return StrategyDecision(
                    self.state,
                    OrderPurpose.PYRAMID_ENTRY,
                    (
                        "WINNER_ONLY_EXECUTABLE_PNL_REQUIRED",
                        "NEW_BREAKOUT_OR_RESUMPTION",
                        "RELATIVE_STRENGTH_OK",
                    ),
                    next_stage,
                    protective_stop=self.protective_stop,
                )

        if net_pnl_usdt is not None and initial_risk_unit_usdt is not None:
            if initial_risk_unit_usdt <= ZERO:
                raise ValueError("initial risk unit must remain positive and fixed")
            multiple = net_pnl_usdt / initial_risk_unit_usdt
            while self.harvest_index < 2:
                target_multiple = self.config.harvest_r_multiples[self.harvest_index]
                if multiple < target_multiple:
                    break
                self._transition(CampaignState.HARVEST, now)
                target = self.reference_quantity * self.config.harvest_fractions[self.harvest_index]
                filled_before_target = max(
                    ZERO,
                    self._harvested_quantity
                    - self.reference_quantity
                    * sum(self.config.harvest_fractions[: self.harvest_index], ZERO),
                )
                remaining_target = max(ZERO, target - filled_before_target)
                if remaining_target > ZERO:
                    return StrategyDecision(
                        self.state,
                        OrderPurpose.TAKE_PROFIT,
                        (f"HARVEST_{target_multiple}R", "FROZEN_REFERENCE_QUANTITY"),
                        reduce_fraction=self.config.harvest_fractions[self.harvest_index],
                        reduce_quantity=min(position_quantity, remaining_target),
                        protective_stop=self.protective_stop,
                    )
                self.harvest_index += 1

        if self.state is CampaignState.BUILD:
            self._transition(CampaignState.RIDE, now)
        elif self.state is CampaignState.PROBE and self._next_stage > 2:
            self._transition(CampaignState.RIDE, now)
        return StrategyDecision(
            self.state,
            None,
            ("RIDE_TREND",),
            protective_stop=self.protective_stop,
        )
