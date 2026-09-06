"""Fail-closed aggregate risk calculation for the ARX campaign."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, date
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo
import json
from ..contracts import CampaignBook, OrderStatus, RiskState, utc

ZERO = Decimal("0")
ACTIVE = frozenset({OrderStatus.RESERVED, OrderStatus.SUBMITTING, OrderStatus.ACKNOWLEDGED, OrderStatus.RESULT_UNKNOWN, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCEL_PENDING})

@dataclass(frozen=True, slots=True)
class RiskLimits:
    leverage: Decimal; e0: Decimal; aggregate_loss_cap: Decimal; gross_stop_cap: Decimal; gross_notional_cap: Decimal; isolated_margin_cap: Decimal; liquidation_buffer_min: Decimal; stage_notional_cap: Decimal; quantity_step: Decimal; min_quantity: Decimal
    daily_loss_cap: Decimal = Decimal("Infinity"); weekly_loss_cap: Decimal = Decimal("Infinity"); campaign_loss_cap: Decimal = Decimal("Infinity"); drawdown_cap: Decimal = Decimal("Infinity"); loss_streak_limit: int = 3
    first_entry_loss_cap: Decimal = Decimal("Infinity"); max_quantity: Decimal | None = None; min_notional: Decimal = ZERO; price_tick: Decimal | None = None; timezone_name: str = "UTC"

@dataclass(frozen=True, slots=True)
class EntryCandidate:
    quantity: Decimal; worst_fill_price: Decimal; stop_price: Decimal; mark_price: Decimal
    entry_fee: Decimal = ZERO; stressed_funding: Decimal = ZERO; liquidation_price: Decimal | None = None
    future_exit_fee: Decimal = ZERO; future_exit_fee_rate: Decimal = ZERO; liquidity_max_quantity: Decimal | None = None
    tier_verified: bool | None = None; account_verified: bool | None = None; account_stale: bool = False; liquidation_verified: bool | None = None
    stage: int | None = None; at: datetime | None = None; equity_usdt: Decimal | None = None; available_usdt: Decimal | None = None; volatility_gap_slippage: Decimal = ZERO

@dataclass(frozen=True, slots=True)
class RiskAssessment:
    approved_quantity: Decimal; state: RiskState; reason_codes: tuple[str, ...]; principal_loss_at_stop: Decimal; gross_stop_risk: Decimal; gross_notional: Decimal; isolated_margin: Decimal
    giveback_at_stop: Decimal = ZERO; effective_leverage: Decimal | None = None; available_funds_after_margin: Decimal | None = None

@dataclass(slots=True)
class DurableRiskState:
    day: date | None = None; week_start: date | None = None; day_equity_start: Decimal = ZERO; week_equity_start: Decimal = ZERO; contribution_adjusted_high_water: Decimal = ZERO; consecutive_losses: int = 0; state: RiskState = RiskState.NORMAL; campaign_e0: Decimal = ZERO; contributions: Decimal = ZERO; timezone_name: str = "UTC"
    def roll(self, at: datetime, equity: Decimal, contributions: Decimal = ZERO) -> None:
        local=utc(at).astimezone(ZoneInfo(self.timezone_name)); d=local.date(); monday=d.fromordinal(d.toordinal()-d.weekday()); self.contributions += contributions; adjusted=equity-self.contributions
        if self.day != d: self.day,self.day_equity_start=d,adjusted
        if self.week_start != monday: self.week_start,self.week_equity_start=monday,adjusted
        self.contribution_adjusted_high_water=max(self.contribution_adjusted_high_water,adjusted)
    def apply_cycle(self, realized_net: Decimal, limits: RiskLimits) -> None:
        self.consecutive_losses=self.consecutive_losses+1 if realized_net < ZERO else 0
        if self.consecutive_losses >= limits.loss_streak_limit: self.state=RiskState.EXIT_ONLY
    def gate(self, at: datetime, equity: Decimal, book: CampaignBook, limits: RiskLimits) -> tuple[RiskState,tuple[str,...]]:
        self.roll(at,equity); adjusted=equity-self.contributions; r=[]
        if self.day_equity_start-adjusted >= limits.daily_loss_cap:r.append("DAILY_LOSS_LIMIT")
        if self.week_equity_start-adjusted >= limits.weekly_loss_cap:r.append("WEEKLY_LOSS_LIMIT")
        if book.current_campaign_net_pnl_usdt <= -limits.campaign_loss_cap:r.append("CAMPAIGN_LOSS_LIMIT")
        if self.contribution_adjusted_high_water-adjusted >= limits.drawdown_cap:r.append("DRAWDOWN_LIMIT")
        if self.state is not RiskState.NORMAL:r.append("DURABLE_EXIT_ONLY")
        return (RiskState.EXIT_ONLY if r else RiskState.NORMAL,tuple(r))
    def save(self,path:str|Path)->None:
        p=asdict(self); p["day"]=self.day.isoformat() if self.day else None; p["week_start"]=self.week_start.isoformat() if self.week_start else None; p["state"]=self.state.value
        for k,v in list(p.items()):
            if isinstance(v,Decimal):p[k]=str(v)
        Path(path).write_text(json.dumps(p),encoding="utf-8")
    @classmethod
    def load(cls,path:str|Path)->"DurableRiskState":
        p=json.loads(Path(path).read_text(encoding="utf-8")); p["day"]=date.fromisoformat(p["day"]) if p.get("day") else None; p["week_start"]=date.fromisoformat(p["week_start"]) if p.get("week_start") else None
        for k in ("day_equity_start","week_equity_start","contribution_adjusted_high_water","campaign_e0","contributions"):p[k]=Decimal(p[k])
        p["state"]=RiskState(p["state"]); return cls(**p)

class RiskEngine:
    @staticmethod
    def _floor(value:Decimal,step:Decimal)->Decimal:
        if step<=ZERO:raise ValueError("quantity step must be positive")
        return (value/step).to_integral_value(rounding=ROUND_DOWN)*step
    def assess(self,book:CampaignBook,candidate:EntryCandidate,limits:RiskLimits,durable:DurableRiskState|None=None)->RiskAssessment:
        if min(candidate.quantity,candidate.worst_fill_price,candidate.mark_price)<=ZERO or candidate.stop_price>=candidate.worst_fill_price:return RiskAssessment(ZERO,RiskState.EXIT_ONLY,("INVALID_OR_NON_LOSS_STOP",),ZERO,ZERO,ZERO,ZERO)
        reasons=[]
        # Explicitly unknown/stale private observations are entry blockers.  None is a replay-compatible omitted observation.
        if candidate.account_verified is False or candidate.account_stale:reasons.append("ACCOUNT_UNKNOWN_OR_STALE")
        if candidate.tier_verified is False:reasons.append("POSITION_TIER_UNVERIFIED")
        if candidate.liquidation_verified is False or (candidate.liquidation_verified is True and candidate.liquidation_price is None):reasons.append("LIQUIDATION_UNVERIFIED")
        if durable and durable.state is not RiskState.NORMAL:reasons.append("DURABLE_EXIT_ONLY")
        if durable and candidate.at and candidate.equity_usdt is not None: _,dr=durable.gate(candidate.at,candidate.equity_usdt,book,limits); reasons.extend(dr)
        if reasons:return RiskAssessment(ZERO,RiskState.EXIT_ONLY,tuple(dict.fromkeys(reasons)),ZERO,ZERO,ZERO,ZERO)
        rs=[r for r in book.reservations if r.status in ACTIVE]; stop=candidate.stop_price-candidate.volatility_gap_slippage
        if stop<=ZERO:return RiskAssessment(ZERO,RiskState.EXIT_ONLY,("STRESSED_STOP_INVALID",),ZERO,ZERO,ZERO,ZERO)
        def exit_cost(q:Decimal,p:Decimal)->Decimal:return q*p*candidate.future_exit_fee_rate
        def totals(q:Decimal):
            # Existing booked fees/funding are in campaign_realized_net; only future exit costs and unfilled reservation costs appear here.
            pnl=book.campaign_realized_net_pnl_usdt; gross=notional=margin=ZERO
            for l in book.lots:
                c=exit_cost(l.quantity_base,stop); pnl+=l.quantity_base*(stop-l.entry_price)-c; gross+=l.quantity_base*max(l.entry_price-stop,ZERO)+c; notional+=l.quantity_base*candidate.mark_price; margin+=l.quantity_base*candidate.mark_price/limits.leverage
            for r in rs:
                c=exit_cost(r.quantity_base,stop); pnl+=r.quantity_base*(stop-r.worst_fill_price)-r.reserved_entry_fee_usdt-r.stressed_funding_usdt-c; gross+=r.quantity_base*max(r.worst_fill_price-stop,ZERO)+r.reserved_entry_fee_usdt+r.stressed_funding_usdt+c; notional+=r.quantity_base*candidate.mark_price; margin+=r.quantity_base*candidate.mark_price/limits.leverage
            c=exit_cost(q,stop); pnl+=q*(stop-candidate.worst_fill_price)-candidate.entry_fee-candidate.stressed_funding-c; gross+=q*max(candidate.worst_fill_price-stop,ZERO)+candidate.entry_fee+candidate.stressed_funding+c; notional+=q*candidate.mark_price; margin+=q*candidate.mark_price/limits.leverage
            # A separately supplied stressed exit charge is campaign-level and must
            # be charged once, not once per lot/reservation.
            if q or book.lots or rs:
                pnl-=candidate.future_exit_fee; gross+=candidate.future_exit_fee
            return max(ZERO,-pnl),gross,notional,margin,max(ZERO,book.current_campaign_net_pnl_usdt+pnl)
        q=self._floor(candidate.quantity,limits.quantity_step)
        for maximum in (limits.max_quantity,candidate.liquidity_max_quantity):
            if maximum is not None:q=min(q,self._floor(maximum,limits.quantity_step))
        while q>=limits.min_quantity:
            principal,gross,notional,margin,giveback=totals(q); stage_used=book.stage_filled_notional_usdt.get(candidate.stage or 1,ZERO)+sum(r.quantity_base*candidate.mark_price for r in rs)+q*candidate.mark_price
            liquid_ok=candidate.liquidation_price is None or stop-candidate.liquidation_price>=limits.liquidation_buffer_min; funds_ok=candidate.available_usdt is None or margin<=candidate.available_usdt; first_ok=bool(book.lots or rs) or principal<=limits.first_entry_loss_cap
            if principal<=limits.aggregate_loss_cap and gross<=limits.gross_stop_cap and notional<=limits.gross_notional_cap and margin<=limits.isolated_margin_cap and stage_used<=limits.stage_notional_cap and liquid_ok and funds_ok and first_ok and q*candidate.mark_price>=limits.min_notional:
                lev=notional/candidate.equity_usdt if candidate.equity_usdt and candidate.equity_usdt>ZERO else None; available=candidate.available_usdt-margin if candidate.available_usdt is not None else None
                return RiskAssessment(q,RiskState.NORMAL,("ALL_GATES_OK",),principal,gross,notional,margin,giveback,lev,available)
            q-=limits.quantity_step
        principal,gross,notional,margin,giveback=totals(ZERO); return RiskAssessment(ZERO,RiskState.PAUSE_ENTRIES,("MINIMUM_SIZE_FAILS",),principal,gross,notional,margin,giveback)
