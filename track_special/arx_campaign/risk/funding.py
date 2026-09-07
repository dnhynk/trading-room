"""Deterministic funding-cost scenarios; displayed rates are never treated as final."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from enum import StrEnum
from typing import Mapping


ZERO = Decimal("0")


class FundingRateKind(StrEnum):
    DISPLAYED = "displayed"
    ASSUMED = "assumed"
    FINAL_SETTLED = "final_settled"


@dataclass(frozen=True, slots=True)
class FundingScenarioResult:
    name: str
    assumed_rate_per_settlement: Decimal
    settlements: int
    cost_usdt: Decimal
    pct_of_e0: Decimal
    pct_of_notional: Decimal
    rate_kind: FundingRateKind = FundingRateKind.ASSUMED


def project_funding_scenarios(
    *,
    notional_usdt: Decimal,
    e0_usdt: Decimal,
    holding_hours: Decimal,
    observed_interval_hours: Decimal | None,
    rates: Mapping[str, Decimal],
) -> tuple[FundingScenarioResult, ...]:
    """Project basic/adverse/extreme assumptions using the observed interval.

    A missing or invalid settlement interval is a hard error, so the historical
    four-hour listing announcement can never silently become a runtime default.
    """

    if notional_usdt <= ZERO or e0_usdt <= ZERO or holding_hours < ZERO:
        raise ValueError("funding projection inputs are invalid")
    if observed_interval_hours is None or observed_interval_hours <= ZERO:
        raise ValueError("current funding interval is unavailable")
    if set(rates) != {"base", "adverse", "extreme"}:
        raise ValueError("base, adverse, and extreme funding rates are required")
    settlements = int(
        (holding_hours / observed_interval_hours).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    results = []
    for name in ("base", "adverse", "extreme"):
        rate = rates[name]
        # Projected favorable income is shown as zero cost and cannot enlarge a
        # risk budget. Final settled negative funding remains a ledger event.
        cost = notional_usdt * max(rate, ZERO) * settlements
        results.append(
            FundingScenarioResult(
                name,
                rate,
                settlements,
                cost,
                cost / e0_usdt * Decimal("100"),
                cost / notional_usdt * Decimal("100"),
            )
        )
    return tuple(results)
