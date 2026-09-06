"""Fail-closed aggregate risk calculation for the ARX campaign."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, ROUND_DOWN
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from ..contracts import CampaignBook, OrderStatus, RiskState, utc


ZERO = Decimal("0")
INFINITY = Decimal("Infinity")
ACTIVE = frozenset(
    {
        OrderStatus.RESERVED,
        OrderStatus.SUBMITTING,
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.RESULT_UNKNOWN,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_PENDING,
    }
)


def _zone(name: str) -> tzinfo:
    # Windows may not ship the IANA database. The campaign's two supported
    # operational zones are fixed-offset and do not observe daylight saving.
    if name == "UTC":
        return timezone.utc
    if name == "Asia/Seoul":
        return timezone(timedelta(hours=9), name="Asia/Seoul")
    return ZoneInfo(name)


@dataclass(frozen=True, slots=True)
class RiskLimits:
    leverage: Decimal
    e0: Decimal
    aggregate_loss_cap: Decimal
    gross_stop_cap: Decimal
    gross_notional_cap: Decimal
    isolated_margin_cap: Decimal
    liquidation_buffer_min: Decimal
    stage_notional_cap: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    daily_loss_cap: Decimal = INFINITY
    weekly_loss_cap: Decimal = INFINITY
    campaign_loss_cap: Decimal = INFINITY
    drawdown_cap: Decimal = INFINITY
    loss_streak_limit: int = 3
    first_entry_loss_cap: Decimal = INFINITY
    giveback_cap: Decimal = INFINITY
    funding_cost_cap: Decimal = INFINITY
    max_quantity: Decimal | None = None
    min_notional: Decimal = ZERO
    price_tick: Decimal | None = None
    timezone_name: str = "UTC"
    max_entry_stages: int = 4
    cooldown_after_loss: timedelta = timedelta(hours=48)

    def __post_init__(self) -> None:
        if self.leverage <= ZERO or self.e0 <= ZERO:
            raise ValueError("leverage and E0 must be positive")
        if self.quantity_step <= ZERO or self.min_quantity <= ZERO:
            raise ValueError("quantity precision and minimum must be positive")
        if self.price_tick is not None and self.price_tick <= ZERO:
            raise ValueError("price tick must be positive")
        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            raise ValueError("maximum quantity is below the minimum")
        if self.max_entry_stages < 1 or self.loss_streak_limit < 1:
            raise ValueError("stage and loss-streak limits must be positive")
        if any(
            value < ZERO
            for value in (
                self.aggregate_loss_cap,
                self.gross_stop_cap,
                self.gross_notional_cap,
                self.isolated_margin_cap,
                self.liquidation_buffer_min,
                self.stage_notional_cap,
                self.daily_loss_cap,
                self.weekly_loss_cap,
                self.campaign_loss_cap,
                self.drawdown_cap,
                self.first_entry_loss_cap,
                self.giveback_cap,
                self.funding_cost_cap,
                self.min_notional,
            )
        ):
            raise ValueError("risk caps cannot be negative")
        _zone(self.timezone_name)


@dataclass(frozen=True, slots=True)
class EntryCandidate:
    quantity: Decimal
    worst_fill_price: Decimal
    stop_price: Decimal
    mark_price: Decimal
    # These are totals for ``quantity`` and are scaled down with an approval.
    entry_fee: Decimal = ZERO
    stressed_funding: Decimal = ZERO
    liquidation_price: Decimal | None = None
    # Additional campaign-level future exit cost plus a proportional exit rate.
    future_exit_fee: Decimal = ZERO
    future_exit_fee_rate: Decimal = ZERO
    liquidity_max_quantity: Decimal | None = None
    tier_verified: bool | None = None
    account_verified: bool | None = None
    account_stale: bool = False
    liquidation_verified: bool | None = None
    require_private_verification: bool = False
    stage: int | None = None
    at: datetime | None = None
    equity_usdt: Decimal | None = None
    available_usdt: Decimal | None = None
    volatility_gap_slippage: Decimal = ZERO
    is_pyramid: bool = False
    existing_position_profitable_after_costs: bool = False
    protective_stop_lowered: bool = False
    recovery_order_larger: bool = False
    funding_allowed: bool = True
    liquidity_allowed: bool = True


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    approved_quantity: Decimal
    state: RiskState
    reason_codes: tuple[str, ...]
    principal_loss_at_stop: Decimal
    gross_stop_risk: Decimal
    gross_notional: Decimal
    isolated_margin: Decimal
    giveback_at_stop: Decimal = ZERO
    pnl_at_stop: Decimal = ZERO
    stressed_future_funding: Decimal = ZERO
    effective_leverage: Decimal | None = None
    available_funds_after_margin: Decimal | None = None


@dataclass(slots=True)
class DurableRiskState:
    """Contribution-adjusted loss state that never resets on process restart."""

    day: date | None = None
    week_start: date | None = None
    day_equity_start: Decimal = ZERO
    week_equity_start: Decimal = ZERO
    contribution_adjusted_high_water: Decimal = ZERO
    consecutive_losses: int = 0
    state: RiskState = RiskState.NORMAL
    campaign_e0: Decimal = ZERO
    contributions: Decimal = ZERO
    timezone_name: str = "UTC"
    permanent_halt: bool = False
    cooldown_until: datetime | None = None
    periodic_halt_day: date | None = None
    periodic_halt_week: date | None = None
    last_trigger_codes: tuple[str, ...] = ()

    def _local_boundaries(self, at: datetime) -> tuple[date, date]:
        local = utc(at).astimezone(_zone(self.timezone_name))
        day = local.date()
        return day, day.fromordinal(day.toordinal() - day.weekday())

    def roll(self, at: datetime, equity: Decimal, net_external_flow: Decimal = ZERO) -> None:
        day, week = self._local_boundaries(at)
        self.contributions += net_external_flow
        adjusted = equity - self.contributions
        if self.day != day:
            self.day, self.day_equity_start = day, adjusted
        if self.week_start != week:
            self.week_start, self.week_equity_start = week, adjusted
        if self.contribution_adjusted_high_water == ZERO:
            self.contribution_adjusted_high_water = adjusted
        else:
            self.contribution_adjusted_high_water = max(
                self.contribution_adjusted_high_water, adjusted
            )

    def apply_cycle(
        self,
        realized_net: Decimal,
        limits: RiskLimits,
        at: datetime | None = None,
    ) -> None:
        self.consecutive_losses = self.consecutive_losses + 1 if realized_net < ZERO else 0
        if self.consecutive_losses >= limits.loss_streak_limit:
            self.state = RiskState.EXIT_ONLY
            self.last_trigger_codes = ("LOSS_STREAK_LIMIT",)
            if at is not None:
                self.cooldown_until = utc(at) + limits.cooldown_after_loss

    def gate(
        self,
        at: datetime,
        equity: Decimal,
        book: CampaignBook,
        limits: RiskLimits,
        net_external_flow: Decimal = ZERO,
    ) -> tuple[RiskState, tuple[str, ...]]:
        self.roll(at, equity, net_external_flow)
        adjusted = equity - self.contributions
        reasons: list[str] = []
        if self.day_equity_start - adjusted >= limits.daily_loss_cap:
            reasons.append("DAILY_LOSS_LIMIT")
        if self.week_equity_start - adjusted >= limits.weekly_loss_cap:
            reasons.append("WEEKLY_LOSS_LIMIT")
        if book.current_campaign_net_pnl_usdt <= -limits.campaign_loss_cap:
            reasons.append("CAMPAIGN_LOSS_LIMIT")
        if self.contribution_adjusted_high_water - adjusted >= limits.drawdown_cap:
            reasons.append("DRAWDOWN_LIMIT")
        if reasons:
            day, week = self._local_boundaries(at)
            self.state = RiskState.EXIT_ONLY
            self.last_trigger_codes = tuple(reasons)
            if "DAILY_LOSS_LIMIT" in reasons:
                self.periodic_halt_day = day
            if "WEEKLY_LOSS_LIMIT" in reasons:
                self.periodic_halt_week = week
            if {"CAMPAIGN_LOSS_LIMIT", "DRAWDOWN_LIMIT"}.intersection(reasons):
                self.permanent_halt = True
        elif self.state is not RiskState.NORMAL:
            reasons.extend(self.last_trigger_codes or ("DURABLE_EXIT_ONLY",))
        return (RiskState.EXIT_ONLY if reasons else RiskState.NORMAL, tuple(dict.fromkeys(reasons)))

    def attempt_periodic_resume(self, at: datetime) -> bool:
        """Explicit resume check; a clock boundary alone never calls this method."""

        at = utc(at)
        day, week = self._local_boundaries(at)
        if self.permanent_halt or (self.cooldown_until and at < self.cooldown_until):
            return False
        if self.periodic_halt_day is not None and day <= self.periodic_halt_day:
            return False
        if self.periodic_halt_week is not None and week <= self.periodic_halt_week:
            return False
        self.state = RiskState.NORMAL
        self.periodic_halt_day = None
        self.periodic_halt_week = None
        self.last_trigger_codes = ()
        self.consecutive_losses = 0
        return True

    def save(self, path: str | Path) -> None:
        payload = asdict(self)
        for name in ("day", "week_start", "periodic_halt_day", "periodic_halt_week"):
            value = payload[name]
            payload[name] = value.isoformat() if value else None
        payload["cooldown_until"] = (
            self.cooldown_until.isoformat() if self.cooldown_until else None
        )
        payload["state"] = self.state.value
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = str(value)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".new")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(target)

    @classmethod
    def load(cls, path: str | Path) -> "DurableRiskState":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for name in ("day", "week_start", "periodic_halt_day", "periodic_halt_week"):
            payload[name] = date.fromisoformat(payload[name]) if payload.get(name) else None
        payload["cooldown_until"] = (
            datetime.fromisoformat(payload["cooldown_until"])
            if payload.get("cooldown_until")
            else None
        )
        for name in (
            "day_equity_start",
            "week_equity_start",
            "contribution_adjusted_high_water",
            "campaign_e0",
            "contributions",
        ):
            payload[name] = Decimal(payload[name])
        payload["state"] = RiskState(payload["state"])
        payload["last_trigger_codes"] = tuple(payload.get("last_trigger_codes", ()))
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class _Totals:
    pnl_at_stop: Decimal
    principal_loss: Decimal
    giveback: Decimal
    gross_stop: Decimal
    gross_notional: Decimal
    isolated_margin: Decimal
    pending_margin_and_fees: Decimal
    stressed_funding: Decimal
    stage_notional: Decimal


class RiskEngine:
    @staticmethod
    def _floor(value: Decimal, step: Decimal) -> Decimal:
        if step <= ZERO:
            raise ValueError("quantity step must be positive")
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _on_tick(value: Decimal, tick: Decimal | None) -> bool:
        return tick is None or value % tick == ZERO

    def assess(
        self,
        book: CampaignBook,
        candidate: EntryCandidate,
        limits: RiskLimits,
        durable: DurableRiskState | None = None,
    ) -> RiskAssessment:
        stage = candidate.stage or 1
        static_reasons: list[str] = []
        if book.e0_usdt != limits.e0:
            static_reasons.append("E0_MISMATCH")
        if (
            min(candidate.quantity, candidate.worst_fill_price, candidate.mark_price) <= ZERO
            or candidate.stop_price >= candidate.worst_fill_price
        ):
            static_reasons.append("INVALID_OR_NON_LOSS_STOP")
        if candidate.volatility_gap_slippage < ZERO:
            static_reasons.append("INVALID_GAP_STRESS")
        if any(
            value < ZERO
            for value in (
                candidate.entry_fee,
                candidate.stressed_funding,
                candidate.future_exit_fee,
                candidate.future_exit_fee_rate,
            )
        ):
            static_reasons.append("INVALID_COST_INPUT")
        if not self._on_tick(candidate.worst_fill_price, limits.price_tick) or not self._on_tick(
            candidate.stop_price, limits.price_tick
        ):
            static_reasons.append("PRICE_PRECISION_INVALID")
        if stage < 1 or stage > limits.max_entry_stages:
            static_reasons.append("ENTRY_STAGE_INVALID")
        if candidate.is_pyramid and not candidate.existing_position_profitable_after_costs:
            static_reasons.append("PYRAMID_NOT_PROFITABLE_AFTER_COSTS")
        if candidate.protective_stop_lowered:
            static_reasons.append("PROTECTIVE_STOP_LOWERING")
        if candidate.recovery_order_larger:
            static_reasons.append("RECOVERY_SIZE_INCREASE")
        if not candidate.funding_allowed:
            static_reasons.append("FUNDING_BLOCK")
        if not candidate.liquidity_allowed:
            static_reasons.append("LIQUIDITY_BLOCK")
        if candidate.account_verified is False or candidate.account_stale:
            static_reasons.append("ACCOUNT_UNKNOWN_OR_STALE")
        if candidate.tier_verified is False:
            static_reasons.append("POSITION_TIER_UNVERIFIED")
        if candidate.liquidation_verified is False or (
            candidate.liquidation_verified is True and candidate.liquidation_price is None
        ):
            static_reasons.append("LIQUIDATION_UNVERIFIED")
        if candidate.liquidation_price is not None and candidate.liquidation_price <= ZERO:
            static_reasons.append("LIQUIDATION_PRICE_INVALID")
        if candidate.require_private_verification and not (
            candidate.account_verified is True
            and candidate.tier_verified is True
            and candidate.liquidation_verified is True
            and candidate.liquidation_price is not None
            and candidate.equity_usdt is not None
            and candidate.available_usdt is not None
        ):
            static_reasons.append("PRIVATE_PREFLIGHT_INCOMPLETE")
        if durable and candidate.at and candidate.equity_usdt is not None:
            _, durable_reasons = durable.gate(candidate.at, candidate.equity_usdt, book, limits)
            static_reasons.extend(durable_reasons)
        elif durable and durable.state is not RiskState.NORMAL:
            static_reasons.extend(durable.last_trigger_codes or ("DURABLE_EXIT_ONLY",))

        reservations = tuple(r for r in book.reservations if r.status in ACTIVE)
        stressed_stop = candidate.stop_price - candidate.volatility_gap_slippage
        if stressed_stop <= ZERO:
            static_reasons.append("STRESSED_STOP_INVALID")

        def scaled(total: Decimal, approved: Decimal) -> Decimal:
            if candidate.quantity <= ZERO:
                return ZERO
            return total * approved / candidate.quantity

        def totals(approved: Decimal) -> _Totals:
            pnl = book.campaign_realized_net_pnl_usdt
            gross_stop = ZERO
            gross_notional = ZERO
            isolated_margin = ZERO
            pending_commitment = ZERO
            stressed_funding = ZERO
            stage_notional = book.stage_filled_notional_usdt.get(stage, ZERO)

            for lot in book.lots:
                exit_cost = lot.quantity_base * stressed_stop * candidate.future_exit_fee_rate
                pnl += lot.quantity_base * (stressed_stop - lot.entry_price) - exit_cost
                gross_stop += lot.quantity_base * max(lot.entry_price - stressed_stop, ZERO) + exit_cost
                gross_notional += lot.quantity_base * candidate.mark_price
                isolated_margin += lot.quantity_base * candidate.mark_price / limits.leverage

            for reservation in reservations:
                exit_cost = reservation.quantity_base * stressed_stop * candidate.future_exit_fee_rate
                reservation_cost = (
                    reservation.reserved_entry_fee_usdt
                    + reservation.stressed_funding_usdt
                    + exit_cost
                )
                pnl += reservation.quantity_base * (
                    stressed_stop - reservation.worst_fill_price
                ) - reservation_cost
                gross_stop += reservation.quantity_base * max(
                    reservation.worst_fill_price - stressed_stop, ZERO
                ) + reservation_cost
                gross_notional += reservation.quantity_base * candidate.mark_price
                reservation_margin = (
                    reservation.quantity_base
                    * max(candidate.mark_price, reservation.worst_fill_price)
                    / limits.leverage
                )
                isolated_margin += reservation_margin
                pending_commitment += reservation_margin + reservation.reserved_entry_fee_usdt
                stressed_funding += reservation.stressed_funding_usdt
                if reservation.stage in {None, stage}:
                    stage_notional += reservation.quantity_base * reservation.worst_fill_price

            entry_fee = scaled(candidate.entry_fee, approved)
            entry_funding = scaled(candidate.stressed_funding, approved)
            exit_cost = approved * stressed_stop * candidate.future_exit_fee_rate
            candidate_cost = entry_fee + entry_funding + exit_cost
            pnl += approved * (stressed_stop - candidate.worst_fill_price) - candidate_cost
            gross_stop += approved * max(
                candidate.worst_fill_price - stressed_stop, ZERO
            ) + candidate_cost
            gross_notional += approved * candidate.mark_price
            candidate_margin = (
                approved
                * max(candidate.mark_price, candidate.worst_fill_price)
                / limits.leverage
            )
            isolated_margin += candidate_margin
            pending_commitment += candidate_margin + entry_fee
            stressed_funding += entry_funding
            stage_notional += approved * candidate.worst_fill_price

            if approved > ZERO or book.lots or reservations:
                pnl -= candidate.future_exit_fee
                gross_stop += candidate.future_exit_fee
            principal = max(ZERO, -pnl)
            giveback = max(ZERO, book.current_campaign_net_pnl_usdt - pnl)
            return _Totals(
                pnl,
                principal,
                giveback,
                gross_stop,
                gross_notional,
                isolated_margin,
                pending_commitment,
                stressed_funding,
                stage_notional,
            )

        def failures(approved: Decimal, value: _Totals) -> tuple[str, ...]:
            reasons: list[str] = []
            if value.principal_loss > limits.aggregate_loss_cap:
                reasons.append("AGGREGATE_STOP_LOSS_CAP")
            if value.giveback > limits.giveback_cap:
                reasons.append("GIVEBACK_CAP")
            if value.gross_stop > limits.gross_stop_cap:
                reasons.append("GROSS_STOP_RISK_CAP")
            if value.gross_notional > limits.gross_notional_cap:
                reasons.append("GROSS_NOTIONAL_CAP")
            if value.isolated_margin > limits.isolated_margin_cap:
                reasons.append("ISOLATED_MARGIN_CAP")
            if value.stage_notional > limits.stage_notional_cap:
                reasons.append("STAGE_NOTIONAL_CAP")
            if value.stressed_funding > limits.funding_cost_cap:
                reasons.append("FUNDING_COST_CAP")
            if not book.lots and not reservations and value.principal_loss > limits.first_entry_loss_cap:
                reasons.append("FIRST_ENTRY_RISK_CAP")
            if candidate.liquidation_price is not None and (
                stressed_stop - candidate.liquidation_price < limits.liquidation_buffer_min
            ):
                reasons.append("LIQUIDATION_BUFFER_CAP")
            if candidate.available_usdt is not None and value.pending_margin_and_fees > candidate.available_usdt:
                reasons.append("AVAILABLE_FUNDS_CAP")
            if approved * candidate.worst_fill_price < limits.min_notional:
                reasons.append("MIN_NOTIONAL")
            return tuple(reasons)

        zero_totals = totals(ZERO) if stressed_stop > ZERO else _Totals(*(ZERO,) * 9)
        if static_reasons:
            return RiskAssessment(
                ZERO,
                RiskState.EXIT_ONLY,
                tuple(dict.fromkeys(static_reasons)),
                zero_totals.principal_loss,
                zero_totals.gross_stop,
                zero_totals.gross_notional,
                zero_totals.isolated_margin,
                zero_totals.giveback,
                zero_totals.pnl_at_stop,
                zero_totals.stressed_funding,
            )

        maximum = self._floor(candidate.quantity, limits.quantity_step)
        for cap in (limits.max_quantity, candidate.liquidity_max_quantity):
            if cap is not None:
                maximum = min(maximum, self._floor(cap, limits.quantity_step))
        units = int(maximum / limits.quantity_step)

        # Caps are monotone in added long quantity. Binary search avoids a large
        # decrement loop on base-coin contracts with a one-unit quantity step.
        low, high = 0, units
        while low < high:
            middle = (low + high + 1) // 2
            trial = limits.quantity_step * middle
            trial_failures = tuple(
                reason for reason in failures(trial, totals(trial)) if reason != "MIN_NOTIONAL"
            )
            if trial_failures:
                high = middle - 1
            else:
                low = middle
        approved = limits.quantity_step * low
        approved_totals = totals(approved)
        final_failures = failures(approved, approved_totals)
        if approved < limits.min_quantity or final_failures:
            attempted = totals(maximum)
            denied = tuple(dict.fromkeys(failures(maximum, attempted)))
            if approved < limits.min_quantity:
                denied += ("MINIMUM_QUANTITY",)
            return RiskAssessment(
                ZERO,
                RiskState.PAUSE_ENTRIES,
                denied or ("MINIMUM_SIZE_FAILS",),
                zero_totals.principal_loss,
                zero_totals.gross_stop,
                zero_totals.gross_notional,
                zero_totals.isolated_margin,
                zero_totals.giveback,
                zero_totals.pnl_at_stop,
                zero_totals.stressed_funding,
                available_funds_after_margin=candidate.available_usdt,
            )

        effective_leverage = (
            approved_totals.gross_notional / candidate.equity_usdt
            if candidate.equity_usdt is not None and candidate.equity_usdt > ZERO
            else None
        )
        available_after = (
            candidate.available_usdt - approved_totals.pending_margin_and_fees
            if candidate.available_usdt is not None
            else None
        )
        reason_codes: tuple[str, ...] = ("ALL_GATES_OK",)
        if approved < maximum:
            reason_codes += ("SIZE_REDUCED_TO_LIMITS",)
        return RiskAssessment(
            approved,
            RiskState.NORMAL,
            reason_codes,
            approved_totals.principal_loss,
            approved_totals.gross_stop,
            approved_totals.gross_notional,
            approved_totals.isolated_margin,
            approved_totals.giveback,
            approved_totals.pnl_at_stop,
            approved_totals.stressed_funding,
            effective_leverage,
            available_after,
        )
