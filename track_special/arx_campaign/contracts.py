"""Shared immutable contracts for the ARX futures special track.

These types deliberately contain no exchange I/O.  Strategy, risk, execution,
and ledger code exchange values through these records so that UTA and Classic
wire fields cannot leak across layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Mapping


ZERO = Decimal("0")


def decimal(value: Decimal | str | int) -> Decimal:
    """Create a Decimal without accepting lossy binary floats."""

    if isinstance(value, float):
        raise TypeError("binary float is not permitted for financial values")
    return value if isinstance(value, Decimal) else Decimal(value)


def utc(value: datetime) -> datetime:
    """Require an aware timestamp and normalize it to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class OperatingMode(StrEnum):
    OBSERVE = "observe"
    PAPER = "paper"
    REPLAY = "replay"
    LIVE = "live"


class ApiFamily(StrEnum):
    UNVERIFIED = "unverified"
    UTA_V3 = "uta_v3"
    CLASSIC_V2 = "classic_v2"


class AccountMode(StrEnum):
    UNVERIFIED = "unverified"
    CLASSIC = "classic"
    UTA_ISOLATED = "uta_isolated"
    UTA_STANDARD = "uta_standard"
    UTA_ADVANCED = "uta_advanced"


class MarginMode(StrEnum):
    UNVERIFIED = "unverified"
    ISOLATED = "isolated"
    CROSSED = "crossed"


class PositionMode(StrEnum):
    UNVERIFIED = "unverified"
    ONE_WAY = "one_way"
    HEDGE = "hedge"


class CampaignState(StrEnum):
    WATCH = "WATCH"
    PROBE = "PROBE"
    BUILD = "BUILD"
    RIDE = "RIDE"
    HARVEST = "HARVEST"
    FLAT = "FLAT"
    COOLDOWN = "COOLDOWN"


class RiskState(StrEnum):
    NORMAL = "NORMAL"
    PAUSE_ENTRIES = "PAUSE_ENTRIES"
    EXIT_ONLY = "EXIT_ONLY"
    EMERGENCY_HALT = "EMERGENCY_HALT"


class OrderPurpose(StrEnum):
    PROBE_ENTRY = "probe_entry"
    PYRAMID_ENTRY = "pyramid_entry"
    TAKE_PROFIT = "take_profit"
    PROTECTIVE_STOP = "protective_stop"
    REQUESTED_EXIT = "requested_exit"
    EMERGENCY_REDUCTION = "emergency_reduction"


class OrderStatus(StrEnum):
    INTENDED = "intended"
    RESERVED = "reserved"
    SUBMITTING = "submitting"
    ACKNOWLEDGED = "acknowledged"
    RESULT_UNKNOWN = "result_unknown"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"


class ProtectionState(StrEnum):
    UNVERIFIED = "unverified"
    PENDING = "pending"
    ACTIVE = "active"
    INSUFFICIENT = "insufficient"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    venue: str
    api_family: ApiFamily
    symbol: str
    category: str
    base_coin: str
    quote_coin: str
    settlement_coin: str
    contract_type: str
    is_linear: bool
    contract_multiplier: Decimal
    status: str
    price_tick: Decimal
    quantity_step: Decimal
    min_order_quantity: Decimal
    min_order_notional: Decimal
    max_limit_quantity: Decimal | None
    max_market_quantity: Decimal | None
    min_leverage: Decimal | None
    max_leverage: Decimal | None
    funding_interval_hours: int | None
    maker_fee_rate: Decimal | None
    taker_fee_rate: Decimal | None
    observed_at: datetime
    raw_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", utc(self.observed_at))
        if any(
            value <= ZERO
            for value in (
                self.contract_multiplier,
                self.price_tick,
                self.quantity_step,
                self.min_order_quantity,
                self.min_order_notional,
            )
        ):
            raise ValueError("instrument multiplier, precision, and minimums must be positive")

    @property
    def live_identity_verified(self) -> bool:
        return (
            self.api_family is ApiFamily.UTA_V3
            and self.symbol == "ARXUSDT"
            and self.category == "USDT-FUTURES"
            and self.base_coin == "ARX"
            and self.quote_coin == "USDT"
            and self.settlement_coin == "USDT"
            and self.contract_type == "perpetual"
            and self.is_linear
            and self.status == "online"
        )


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    exchange_time: datetime
    received_at: datetime
    mark_price: Decimal
    index_price: Decimal
    last_price: Decimal
    executable_bid: Decimal
    executable_ask: Decimal
    funding_rate_displayed: Decimal | None
    next_funding_at: datetime | None
    open_interest_base: Decimal | None
    source_sequence: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange_time", utc(self.exchange_time))
        object.__setattr__(self, "received_at", utc(self.received_at))
        if self.next_funding_at is not None:
            object.__setattr__(self, "next_funding_at", utc(self.next_funding_at))
        if self.symbol != "ARXUSDT" or any(
            value <= ZERO
            for value in (
                self.mark_price,
                self.index_price,
                self.last_price,
                self.executable_bid,
                self.executable_ask,
            )
        ):
            raise ValueError("ARX market snapshot needs distinct positive prices")
        if self.executable_bid > self.executable_ask:
            raise ValueError("executable bid cannot exceed ask")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    observed_at: datetime
    api_family: ApiFamily
    account_mode: AccountMode
    margin_mode: MarginMode
    position_mode: PositionMode
    margin_coin: str
    strategy_equity_usdt: Decimal | None
    available_usdt: Decimal | None
    reconciled: bool
    dedicated_or_verifiably_separated: bool
    external_exposure_detected: bool
    auto_margin_top_up_disabled: bool | None
    asset_mode: str = "unverified"

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", utc(self.observed_at))

    @property
    def entry_preconditions_verified(self) -> bool:
        return (
            self.api_family is ApiFamily.UTA_V3
            and self.account_mode in {AccountMode.UTA_ISOLATED, AccountMode.UTA_STANDARD}
            and self.margin_mode is MarginMode.ISOLATED
            and self.position_mode is PositionMode.ONE_WAY
            and self.margin_coin == "USDT"
            and self.asset_mode == "single_asset"
            and self.strategy_equity_usdt is not None
            and self.strategy_equity_usdt > ZERO
            and self.reconciled
            and self.dedicated_or_verifiably_separated
            and not self.external_exposure_detected
            and self.auto_margin_top_up_disabled is True
        )


@dataclass(frozen=True, slots=True)
class PositionLot:
    lot_id: str
    quantity_base: Decimal
    entry_price: Decimal
    entry_fee_usdt: Decimal
    opened_at: datetime
    initial_risk_unit_usdt: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "opened_at", utc(self.opened_at))
        if self.quantity_base <= ZERO or self.entry_price <= ZERO:
            raise ValueError("long position lot quantity and price must be positive")


@dataclass(frozen=True, slots=True)
class Reservation:
    intention_id: str
    client_order_id: str
    quantity_base: Decimal
    worst_fill_price: Decimal
    reserved_entry_fee_usdt: Decimal
    stressed_funding_usdt: Decimal
    created_at: datetime
    expires_at: datetime
    status: OrderStatus
    stage: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", utc(self.created_at))
        object.__setattr__(self, "expires_at", utc(self.expires_at))
        if self.quantity_base <= ZERO or self.worst_fill_price <= ZERO:
            raise ValueError("reservation quantity and price must be positive")
        if self.expires_at <= self.created_at:
            raise ValueError("reservation expiry must follow creation")
        if self.stage is not None and self.stage not in {1, 2, 3, 4}:
            raise ValueError("reservation stage must be 1..4")


@dataclass(frozen=True, slots=True)
class RiskApproval:
    """Short-lived, immutable bridge from aggregate risk to execution.

    The execution database stores these totals in the same transaction as the
    order reservation.  This object is an approval record, never permission to
    call a private exchange endpoint.
    """

    approval_id: str
    intention_id: str
    campaign_id: str
    config_hash: str
    approved_quantity_base: Decimal
    principal_loss_at_stop_usdt: Decimal
    giveback_at_stop_usdt: Decimal
    gross_stop_risk_usdt: Decimal
    gross_notional_usdt: Decimal
    isolated_margin_usdt: Decimal
    market_observed_at: datetime
    account_observed_at: datetime | None
    created_at: datetime
    expires_at: datetime
    risk_state: RiskState
    live_private_inputs_verified: bool = False

    def __post_init__(self) -> None:
        for name in ("market_observed_at", "created_at", "expires_at"):
            object.__setattr__(self, name, utc(getattr(self, name)))
        if self.account_observed_at is not None:
            object.__setattr__(self, "account_observed_at", utc(self.account_observed_at))
        if not self.approval_id or not self.config_hash:
            raise ValueError("approval identity and config hash are required")
        if self.approved_quantity_base <= ZERO:
            raise ValueError("approval quantity must be positive")
        if self.expires_at <= self.created_at:
            raise ValueError("approval expiry must follow creation")
        if any(
            value < ZERO
            for value in (
                self.principal_loss_at_stop_usdt,
                self.giveback_at_stop_usdt,
                self.gross_stop_risk_usdt,
                self.gross_notional_usdt,
                self.isolated_margin_usdt,
            )
        ):
            raise ValueError("risk approval totals cannot be negative")


@dataclass(frozen=True, slots=True)
class CampaignBook:
    campaign_id: str
    e0_usdt: Decimal
    lots: tuple[PositionLot, ...]
    reservations: tuple[Reservation, ...]
    campaign_realized_net_pnl_usdt: Decimal
    current_campaign_net_pnl_usdt: Decimal
    realized_profit_high_water_usdt: Decimal
    reusable_profit_allocated_usdt: Decimal
    reserve_profit_allocated_usdt: Decimal
    stage_filled_notional_usdt: Mapping[int, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.e0_usdt <= ZERO:
            raise ValueError("E0 must be positive")


@dataclass(frozen=True, slots=True)
class ProtectionSnapshot:
    observed_at: datetime
    state: ProtectionState
    trigger_reference: str
    stop_price: Decimal | None
    protected_quantity_base: Decimal
    exchange_order_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", utc(self.observed_at))
        if self.protected_quantity_base < ZERO:
            raise ValueError("protected quantity cannot be negative")
        if self.stop_price is not None and self.stop_price <= ZERO:
            raise ValueError("protection stop must be positive")
        if self.state is ProtectionState.ACTIVE and (
            self.stop_price is None
            or self.protected_quantity_base <= ZERO
            or not self.exchange_order_id
            or self.trigger_reference != "mark_price"
        ):
            raise ValueError("active protection needs a queried mark-price server order")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intention_id: str
    campaign_id: str
    purpose: OrderPurpose
    side: str
    quantity_base: Decimal
    limit_price: Decimal | None
    time_in_force: str
    reduce_only: bool
    stage: int | None
    config_hash: str
    market_observed_at: datetime
    created_at: datetime
    expires_at: datetime
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_observed_at", utc(self.market_observed_at))
        object.__setattr__(self, "created_at", utc(self.created_at))
        object.__setattr__(self, "expires_at", utc(self.expires_at))
        if self.quantity_base <= ZERO:
            raise ValueError("intent quantity must be positive")
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.side == "sell" and not self.reduce_only:
            raise ValueError("long-only sell intents must be reduce-only")
        if self.purpose in {OrderPurpose.PROBE_ENTRY, OrderPurpose.PYRAMID_ENTRY}:
            if self.side != "buy" or self.reduce_only:
                raise ValueError("long entry must be a non-reduce-only buy")
            if self.stage not in {1, 2, 3, 4}:
                raise ValueError("entry stage must be 1..4")
        elif self.side != "sell" or not self.reduce_only:
            raise ValueError("all non-entry intents must reduce the one-way long")
        if self.limit_price is not None and self.limit_price <= ZERO:
            raise ValueError("limit price must be positive")
        if not self.config_hash:
            raise ValueError("config hash is required")
        if self.market_observed_at > self.created_at or self.created_at >= self.expires_at:
            raise ValueError("intent observation, creation, and expiry must be ordered")
