"""Independent USDT-linear liquidation estimate and protection-buffer checks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


ZERO = Decimal("0")
BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class LiquidationCheck:
    independent_price: Decimal
    exchange_price: Decimal | None
    discrepancy_bps: Decimal | None
    stop_to_liquidation_buffer: Decimal | None
    required_buffer: Decimal
    sufficient: bool
    reason_codes: tuple[str, ...]


def independent_long_liquidation_price(
    *,
    quantity_base: Decimal,
    average_entry_price: Decimal,
    isolated_margin_usdt: Decimal,
    maintenance_margin_rate: Decimal,
    liquidation_close_fee_rate: Decimal,
    unbooked_funding_cost_usdt: Decimal = ZERO,
) -> Decimal:
    """Solve the linear isolated-margin equity/MMR equality.

    This is an independent diagnostic estimate. It is not an order-approval
    substitute for the venue tier, reserved orders, and returned position price.
    """

    if quantity_base <= ZERO or average_entry_price <= ZERO or isolated_margin_usdt < ZERO:
        raise ValueError("invalid isolated long position")
    if maintenance_margin_rate < ZERO or liquidation_close_fee_rate < ZERO:
        raise ValueError("maintenance and close-fee rates cannot be negative")
    denominator_rate = Decimal("1") - maintenance_margin_rate - liquidation_close_fee_rate
    if denominator_rate <= ZERO:
        raise ValueError("maintenance and close fee leave no solvable buffer")
    numerator = (
        quantity_base * average_entry_price
        - isolated_margin_usdt
        + max(unbooked_funding_cost_usdt, ZERO)
    )
    return max(ZERO, numerator / (quantity_base * denominator_rate))


def check_liquidation_buffer(
    *,
    independent_price: Decimal,
    exchange_price: Decimal | None,
    protective_stop: Decimal,
    volatility_buffer: Decimal,
    gap_stress: Decimal,
    expected_slippage: Decimal,
    exchange_semantics_verified: bool,
) -> LiquidationCheck:
    if independent_price <= ZERO or protective_stop <= ZERO:
        raise ValueError("liquidation estimate and protection must be positive")
    required = volatility_buffer + gap_stress + expected_slippage
    if required < ZERO:
        raise ValueError("buffer components cannot be negative")
    reasons: list[str] = []
    if not exchange_semantics_verified or exchange_price is None or exchange_price <= ZERO:
        reasons.append("EXCHANGE_LIQUIDATION_UNVERIFIED")
        return LiquidationCheck(
            independent_price,
            exchange_price,
            None,
            None,
            required,
            False,
            tuple(reasons),
        )
    conservative_liquidation = max(independent_price, exchange_price)
    discrepancy = abs(exchange_price - independent_price) / exchange_price * BPS
    buffer = protective_stop - conservative_liquidation
    if buffer < required:
        reasons.append("LIQUIDATION_BUFFER_INSUFFICIENT")
    if protective_stop <= conservative_liquidation:
        reasons.append("STOP_NOT_ABOVE_LIQUIDATION")
    return LiquidationCheck(
        independent_price,
        exchange_price,
        discrepancy,
        buffer,
        required,
        not reasons,
        tuple(reasons or ("LIQUIDATION_CHECK_OK",)),
    )
