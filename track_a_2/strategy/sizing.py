"""Spot unit sizing bounded by equity, cash, depth, and exchange minima."""
from decimal import Decimal as D

from track_c.execution.coinone import decimal
from track_a_2.market.units import floor_step


def size(config, contract, fees, book, *, equity, cash, portfolio_notional):
    bids = [(decimal(row["price"], positive=True), decimal(row["qty"])) for row in book["bids"]]
    asks = [(decimal(row["price"], positive=True), decimal(row["qty"])) for row in book["asks"]]
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return dict(reason="invalid_book")
    bid, ask = bids[0][0], asks[0][0]
    maker, taker = decimal(fees["maker"]), decimal(fees["taker"])
    step = decimal(contract["qty_unit"], positive=True)
    equity, cash, portfolio_notional = decimal(equity), decimal(cash), decimal(portfolio_notional)
    portfolio_room = max(D(0), equity * decimal(config["portfolio_notional_fraction"]) - portfolio_notional)
    top_depth = sum(qty for _, qty in asks[:5]) * decimal(config["depth_fraction"])
    caps = {
        "target": equity * decimal(config["unit_fraction"]) / (bid * (D(1) + maker)),
        "cash": cash * decimal(config["cash_fraction"]) / (bid * (D(1) + maker)),
        "portfolio": portfolio_room / bid,
        "depth": top_depth,
        "exchange_qty": decimal(contract["max_qty"]),
        "exchange_amount": decimal(contract["max_order_amount"]) / (bid * (D(1) + maker)),
    }
    qty = floor_step(min(caps.values()), step)
    minimum = decimal(contract["min_order_amount"])
    required = minimum * decimal(config["minimum_exit_multiple"])
    notional = qty * bid
    max_notional = equity * decimal(config["book_notional_fraction"])
    position_ceiling = min(
        max_notional,
        notional * decimal(config["strategy"]["max_units"]),
    )
    fee_budget = position_ceiling * (maker + taker)
    price_risk_budget = equity * decimal(config["book_risk_fraction"]) - fee_budget
    protectable_room = notional - required
    cap = min(price_risk_budget, protectable_room)
    if qty < decimal(contract["min_qty"]) or notional <= required or cap <= 0:
        return dict(reason="minimum_order_exceeds_unit", caps={key: str(value) for key, value in caps.items()})
    return dict(
        reason=None,
        qty=str(qty),
        bid=str(bid),
        ask=str(ask),
        cap_krw=str(cap),
        max_notional=str(max_notional),
        fee_budget_krw=str(fee_budget),
        price_risk_budget_krw=str(price_risk_budget),
        fee_rt_pct=str((maker + taker) * D(100)),
        maker=str(maker),
        taker=str(taker),
        binding=min(caps, key=caps.get),
        caps={key: str(value) for key, value in caps.items()},
    )
