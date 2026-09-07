"""Conservative deterministic synthetic replay; never profitability evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable


ZERO = Decimal("0")
BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class ReplayBar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    bid: Decimal
    ask: Decimal
    funding_rate: Decimal = ZERO
    available_quantity: Decimal = Decimal("999999")
    delay_bps: Decimal = ZERO
    maintenance_rate: Decimal = ZERO  # retained metadata; never used as a liquidation shortcut
    timestamp: datetime | None = None
    mark: Decimal | None = None
    last: Decimal | None = None
    funding_settlement: bool = False
    bid_depth: Decimal | None = None
    ask_depth: Decimal | None = None
    liquidation_price: Decimal | None = None

    def __post_init__(self) -> None:
        if (
            min(self.open, self.high, self.low, self.close, self.bid, self.ask) <= ZERO
            or self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
            or self.bid > self.ask
            or self.delay_bps < ZERO
        ):
            raise ValueError("invalid replay OHLC/book observation")
        for value in (self.mark, self.last, self.liquidation_price):
            if value is not None and value <= ZERO:
                raise ValueError("optional replay prices must be positive")


@dataclass(frozen=True, slots=True)
class ReplayResult:
    filled_quantity: Decimal
    unfilled_quantity: Decimal
    entry_cost: Decimal
    exit_value: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal
    liquidated: bool
    notes: tuple[str, ...]
    current_value: Decimal = ZERO
    final_value: Decimal = ZERO
    unprotected_quantity: Decimal = ZERO
    liquidation_verifiable: bool = False
    exit_filled_quantity: Decimal = ZERO
    remaining_position_quantity: Decimal = ZERO
    estimated_remaining_exit_cost: Decimal = ZERO
    protection_gap_detected: bool = False


def replay(
    bars: Iterable[ReplayBar],
    quantity: Decimal,
    fee_rate: Decimal = Decimal("0.0006"),
    stop: Decimal | None = None,
    take_profit: Decimal | None = None,
    *,
    stop_trigger_reference: str = "mark_price",
    server_protection_verified: bool = False,
) -> ReplayResult:
    observations = list(bars)
    if quantity <= ZERO or not observations:
        return ReplayResult(
            ZERO,
            max(quantity, ZERO),
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            False,
            ("no_fill",),
        )
    if fee_rate < ZERO:
        raise ValueError("fee rate cannot be negative")
    if stop_trigger_reference not in {"mark_price", "last_price"}:
        raise ValueError("unsupported synthetic stop trigger reference")

    first = observations[0]
    displayed_depth = first.ask_depth if first.ask_depth is not None else first.available_quantity
    filled = min(quantity, max(displayed_depth, ZERO))
    entry_price = first.ask * (Decimal("1") + first.delay_bps / BPS)
    fees = filled * entry_price * fee_rate
    funding = ZERO
    exit_value = ZERO
    exit_filled = ZERO
    remaining = filled
    notes: list[str] = []
    liquidated = False
    liquidation_verifiable = all(bar.liquidation_price is not None for bar in observations)

    for bar in observations:
        if bar.funding_settlement:
            if bar.mark is None:
                notes.append("funding_settlement_mark_unavailable")
            else:
                funding += remaining * bar.mark * bar.funding_rate

        executable_bid = bar.bid * (Decimal("1") - bar.delay_bps / BPS)
        trigger_price = bar.mark if stop_trigger_reference == "mark_price" else bar.last
        if trigger_price is None:
            notes.append(f"{stop_trigger_reference}_unavailable")

        # With an explicit position liquidation estimate, a same-bar gap is
        # ordered adversely before an assumed protective fill.
        liquidation_crossed = bar.liquidation_price is not None and (
            (bar.mark is not None and bar.mark <= bar.liquidation_price)
            or bar.low <= bar.liquidation_price
        )
        if liquidation_crossed:
            assert bar.liquidation_price is not None
            exit_price = min(executable_bid, bar.liquidation_price)
            exit_value += remaining * exit_price
            fees += remaining * exit_price * fee_rate
            exit_filled += remaining
            remaining = ZERO
            liquidated = True
            notes.append("explicit_synthetic_liquidation_before_stop")
            break

        stop_crossed = stop is not None and (
            (trigger_price is not None and trigger_price <= stop) or bar.low <= stop
        )
        if stop_crossed:
            assert stop is not None
            exit_price = min(stop, executable_bid)
            exit_depth = bar.bid_depth if bar.bid_depth is not None else bar.available_quantity
            exit_quantity = min(remaining, max(exit_depth, ZERO))
            exit_value += exit_quantity * exit_price
            fees += exit_quantity * exit_price * fee_rate
            exit_filled += exit_quantity
            remaining -= exit_quantity
            notes.append("stop_before_profit_same_bar")
            if not server_protection_verified:
                notes.append("protective_execution_unverified")
            if remaining > ZERO:
                notes.append("partial_stop_exit_exposure_remains")
                continue
            break
        if take_profit is not None and bar.high >= take_profit:
            exit_price = min(take_profit, executable_bid)
            exit_depth = bar.bid_depth if bar.bid_depth is not None else bar.available_quantity
            exit_quantity = min(remaining, max(exit_depth, ZERO))
            exit_value += exit_quantity * exit_price
            fees += exit_quantity * exit_price * fee_rate
            exit_filled += exit_quantity
            remaining -= exit_quantity
            notes.append("take_profit")
            if remaining > ZERO:
                notes.append("partial_take_profit_exposure_remains")
                continue
            break

    last = observations[-1]
    current_value = remaining * last.bid
    estimated_remaining_exit_cost = current_value * fee_rate
    if remaining > ZERO:
        notes.append("marked_to_conservative_executable_bid")
        notes.append("position_exposure_remains")
    if not liquidation_verifiable:
        notes.append("liquidation_not_verifiable_without_position_estimates")

    net = (
        exit_value
        + current_value
        - filled * entry_price
        - fees
        - funding
        - estimated_remaining_exit_cost
    )
    unprotected = ZERO if server_protection_verified else remaining
    return ReplayResult(
        filled,
        quantity - filled,
        filled * entry_price,
        exit_value,
        fees,
        funding,
        net,
        liquidated,
        tuple(dict.fromkeys(notes)),
        current_value,
        exit_value + current_value,
        unprotected,
        liquidation_verifiable,
        exit_filled,
        remaining,
        estimated_remaining_exit_cost,
        filled > ZERO and not server_protection_verified,
    )


def compare(actual: ReplayResult, baseline: ReplayResult) -> dict[str, Decimal | bool | int]:
    return {
        "net_pnl_delta": actual.net_pnl - baseline.net_pnl,
        "fill_delta": actual.filled_quantity - baseline.filled_quantity,
        "exit_fill_delta": actual.exit_filled_quantity - baseline.exit_filled_quantity,
        "remaining_exposure_delta": (
            actual.remaining_position_quantity - baseline.remaining_position_quantity
        ),
        "unfilled_entry_delta": actual.unfilled_quantity - baseline.unfilled_quantity,
        "worse_than_baseline": actual.net_pnl < baseline.net_pnl,
        "liquidation_delta": int(actual.liquidated) - int(baseline.liquidated),
        "protection_gap_delta": int(actual.protection_gap_detected)
        - int(baseline.protection_gap_detected),
    }


def comparisons(
    *,
    cash: ReplayResult,
    spot: ReplayResult,
    no_pyramid: ReplayResult,
    pyramid: ReplayResult,
) -> dict[str, dict[str, Decimal | bool | int]]:
    """Fixed-input scenario comparisons, not a claim of profitability."""

    return {
        "spot_vs_cash": compare(spot, cash),
        "no_pyramid_vs_cash": compare(no_pyramid, cash),
        "pyramid_vs_cash": compare(pyramid, cash),
        "pyramid_vs_no_pyramid": compare(pyramid, no_pyramid),
    }
