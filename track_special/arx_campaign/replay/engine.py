"""Conservative deterministic synthetic replay; it is not execution evidence."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

Z=Decimal("0"); BPS=Decimal("10000")
@dataclass(frozen=True)
class ReplayBar:
    open: Decimal; high: Decimal; low: Decimal; close: Decimal; bid: Decimal; ask: Decimal
    funding_rate: Decimal=Z; available_quantity: Decimal=Decimal("999999"); delay_bps: Decimal=Z; maintenance_rate: Decimal=Z
    timestamp: datetime|None=None; mark: Decimal|None=None; last: Decimal|None=None; funding_settlement: bool=False; bid_depth: Decimal|None=None; ask_depth: Decimal|None=None; liquidation_price: Decimal|None=None
@dataclass(frozen=True)
class ReplayResult:
    filled_quantity: Decimal; unfilled_quantity: Decimal; entry_cost: Decimal; exit_value: Decimal; fees: Decimal; funding: Decimal; net_pnl: Decimal; liquidated: bool; notes: tuple[str,...]
    current_value: Decimal=Z; final_value: Decimal=Z; unprotected_quantity: Decimal=Z

def replay(bars: Iterable[ReplayBar], quantity: Decimal, fee_rate: Decimal=Decimal("0.0006"), stop: Decimal|None=None, take_profit: Decimal|None=None) -> ReplayResult:
    bars=list(bars)
    if quantity <= Z or not bars: return ReplayResult(Z,quantity,Z,Z,Z,Z,Z,False,("no_fill",))
    first=bars[0]; q=min(quantity, first.ask_depth if first.ask_depth is not None else first.available_quantity)
    # Entry crosses the ask plus adverse delay/spread; never grant a same-bar favorable fill.
    entry=first.ask*(Decimal("1")+first.delay_bps/BPS); fees=q*entry*fee_rate; funding=Z; exit_price=None; notes=[]
    for bar in bars:
        # Funding is charged only on explicit settlement events; old fixtures preserve one-per-bar behavior.
        if bar.funding_settlement or all(not x.funding_settlement for x in bars): funding += q*(bar.mark or bar.open)*max(bar.funding_rate,Z)
        adverse_bid=bar.bid*(Decimal("1")-bar.delay_bps/BPS)
        # Same-bar range is adverse: protective stop wins over a profit target.
        if stop is not None and bar.low <= stop: exit_price=min(stop, adverse_bid); notes.append("stop_before_profit_same_bar"); break
        if take_profit is not None and bar.high >= take_profit: exit_price=min(take_profit, adverse_bid); notes.append("take_profit") ; break
        if (bar.liquidation_price is not None and adverse_bid <= bar.liquidation_price) or (bar.maintenance_rate > Z and adverse_bid <= entry*bar.maintenance_rate): exit_price=adverse_bid; notes.append("synthetic_liquidation"); break
    if exit_price is None: exit_price=bars[-1].bid*(Decimal("1")-bars[-1].delay_bps/BPS); notes.append("mark_to_conservative_bid")
    exit_fee=q*exit_price*fee_rate; fees+=exit_fee; net=q*(exit_price-entry)-fees-funding
    return ReplayResult(q,quantity-q,q*entry,q*exit_price,fees,funding,net,"synthetic_liquidation" in notes,tuple(notes),q*bars[-1].bid,q*exit_price,quantity-q)

def compare(actual: ReplayResult, baseline: ReplayResult) -> dict[str, Decimal | bool]:
    return {"net_pnl_delta":actual.net_pnl-baseline.net_pnl,"fill_delta":actual.filled_quantity-baseline.filled_quantity,"worse_than_baseline":actual.net_pnl < baseline.net_pnl}

def comparisons(*, futures: ReplayResult, cash: ReplayResult, spot: ReplayResult, no_pyramid: ReplayResult, pyramid: ReplayResult) -> dict[str, dict[str, Decimal|bool]]:
    """Scenario comparisons, not profitability claims."""
    return {"cash":compare(futures,cash),"spot":compare(futures,spot),"no_pyramid":compare(pyramid,no_pyramid)}
