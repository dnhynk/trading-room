from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal, ROUND_DOWN

from ..contracts import CampaignBook, OrderStatus, Reservation, RiskState, utc

ZERO = Decimal("0")
ACTIVE = frozenset({OrderStatus.RESERVED, OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED, OrderStatus.RESULT_UNKNOWN, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED})

@dataclass(frozen=True, slots=True)
class RiskLimits:
    leverage: Decimal; e0: Decimal; aggregate_loss_cap: Decimal; gross_stop_cap: Decimal
    gross_notional_cap: Decimal; isolated_margin_cap: Decimal; liquidation_buffer_min: Decimal
    stage_notional_cap: Decimal; quantity_step: Decimal; min_quantity: Decimal
    daily_loss_cap: Decimal = Decimal("Infinity"); weekly_loss_cap: Decimal = Decimal("Infinity")
    campaign_loss_cap: Decimal = Decimal("Infinity"); drawdown_cap: Decimal = Decimal("Infinity")
    loss_streak_limit: int = 3

@dataclass(frozen=True, slots=True)
class EntryCandidate:
    quantity: Decimal; worst_fill_price: Decimal; stop_price: Decimal; mark_price: Decimal
    entry_fee: Decimal = ZERO; stressed_funding: Decimal = ZERO; liquidation_price: Decimal | None = None

@dataclass(frozen=True, slots=True)
class RiskAssessment:
    approved_quantity: Decimal; state: RiskState; reason_codes: tuple[str, ...]
    principal_loss_at_stop: Decimal; gross_stop_risk: Decimal; gross_notional: Decimal; isolated_margin: Decimal

@dataclass(slots=True)
class DurableRiskState:
    day: date | None = None; week_start: date | None = None; day_equity_start: Decimal = ZERO; week_equity_start: Decimal = ZERO
    contribution_adjusted_high_water: Decimal = ZERO; consecutive_losses: int = 0; state: RiskState = RiskState.NORMAL
    def roll(self, at: datetime, equity: Decimal, contributions: Decimal = ZERO) -> None:
        d = utc(at).date(); monday = d.fromordinal(d.toordinal() - d.weekday())
        adjusted = equity - contributions
        if self.day != d: self.day, self.day_equity_start = d, adjusted
        if self.week_start != monday: self.week_start, self.week_equity_start = monday, adjusted
        self.contribution_adjusted_high_water = max(self.contribution_adjusted_high_water, adjusted)
    def apply_cycle(self, realized_net: Decimal, limits: RiskLimits) -> None:
        self.consecutive_losses = self.consecutive_losses + 1 if realized_net < ZERO else 0
        if self.consecutive_losses >= limits.loss_streak_limit: self.state = RiskState.EXIT_ONLY

class RiskEngine:
    def assess(self, book: CampaignBook, candidate: EntryCandidate, limits: RiskLimits, durable: DurableRiskState | None = None) -> RiskAssessment:
        if min(candidate.quantity, candidate.worst_fill_price, candidate.mark_price) <= ZERO or candidate.stop_price >= candidate.worst_fill_price:
            return RiskAssessment(ZERO, RiskState.EXIT_ONLY, ("INVALID_OR_NON_LOSS_STOP",), ZERO, ZERO, ZERO, ZERO)
        if durable and durable.state is not RiskState.NORMAL:
            return RiskAssessment(ZERO, durable.state, ("DURABLE_EXIT_ONLY",), ZERO, ZERO, ZERO, ZERO)
        lots = book.lots
        reservations = [r for r in book.reservations if r.status in ACTIVE]
        def totals(q: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
            pnl = book.campaign_realized_net_pnl_usdt
            stoprisk = ZERO; notional = ZERO; margin = ZERO
            for l in lots:
                pnl += l.quantity_base * (candidate.stop_price-l.entry_price)
                stoprisk += l.quantity_base * max(l.entry_price-candidate.stop_price, ZERO)
                notional += l.quantity_base*candidate.mark_price; margin += l.quantity_base*candidate.mark_price/limits.leverage
            for r in reservations:
                pnl += r.quantity_base*(candidate.stop_price-r.worst_fill_price)-r.reserved_entry_fee_usdt-r.stressed_funding_usdt
                stoprisk += r.quantity_base*max(r.worst_fill_price-candidate.stop_price, ZERO)+r.reserved_entry_fee_usdt+r.stressed_funding_usdt
                notional += r.quantity_base*candidate.mark_price; margin += r.quantity_base*candidate.mark_price/limits.leverage
            pnl += q*(candidate.stop_price-candidate.worst_fill_price)-candidate.entry_fee-candidate.stressed_funding
            stoprisk += q*(candidate.worst_fill_price-candidate.stop_price)+candidate.entry_fee+candidate.stressed_funding
            notional += q*candidate.mark_price; margin += q*candidate.mark_price/limits.leverage
            return max(ZERO, -pnl), stoprisk, notional, margin
        q = candidate.quantity.quantize(limits.quantity_step, rounding=ROUND_DOWN)
        reasons=[]
        # decrement only against independent gates; no invented tighter stop.
        while q >= limits.min_quantity:
            principal, gross, notional, margin = totals(q)
            liquid_ok = candidate.liquidation_price is None or candidate.stop_price - candidate.liquidation_price >= limits.liquidation_buffer_min
            if principal <= limits.aggregate_loss_cap and gross <= limits.gross_stop_cap and notional <= limits.gross_notional_cap and margin <= limits.isolated_margin_cap and q*candidate.mark_price <= limits.stage_notional_cap and liquid_ok:
                return RiskAssessment(q, RiskState.NORMAL, tuple(reasons or ["ALL_GATES_OK"]), principal, gross, notional, margin)
            q -= limits.quantity_step
            reasons=["AGGREGATE_LIMITED"]
        principal, gross, notional, margin = totals(ZERO)
        return RiskAssessment(ZERO, RiskState.PAUSE_ENTRIES, tuple(reasons or ["MINIMUM_SIZE_FAILS"]), principal, gross, notional, margin)
