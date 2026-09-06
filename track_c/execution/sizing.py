"""Cash, loss-distance and executable liquidity jointly determine order size."""
from decimal import Decimal as D, ROUND_FLOOR
from track_c.execution.coinone import CoinoneError, decimal


def floor(value, step):
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def price_unit(units, price):
    rows = sorted((decimal(u["range_min"]), decimal(u["price_unit"], positive=True)) for u in units)
    candidates = [step for start, step in rows if start <= price]
    if not candidates:
        raise CoinoneError("price unit unavailable")
    return candidates[-1]


def price_floor(units, price):
    for _ in range(4):
        step = price_unit(units, price)
        rounded = floor(price, step)
        if price_unit(units, rounded) == step:
            return rounded
        price = rounded
    raise CoinoneError("price unit boundary unresolved")


def size_order(config, *, market, units, book, feature, fees, equity, cash, daily_remaining, volume_10s):
    """Returns a frozen risk plan or a refusal; never rounds up to exchange minimums."""
    bids = sorted(((decimal(r["price"], positive=True), decimal(r["qty"])) for r in book["bids"]), reverse=True)
    asks = sorted((decimal(r["price"], positive=True), decimal(r["qty"])) for r in book["asks"])
    if not bids or not asks or asks[0][0] <= bids[0][0]:
        return dict(reason="invalid_book")
    entry, ask = bids[0][0], asks[0][0]
    atr = decimal(feature.get("atr") or 0)
    if not atr:
        return dict(reason="atr_unavailable")
    tick = price_unit(units, entry)
    spread = ask - entry
    maker, taker = (decimal(fees[k]) for k in ("maker", "taker"))
    # At 0% fees this is conservative: a resting bid entry need not pay the
    # spread, but crossing both sides is the opportunity's execution stress.
    if spread + entry * (maker + taker) > atr * decimal(config["max_cost_atr"]):
        return dict(reason="cost_exceeds_movement")
    noise = max(2*tick, 2*spread, atr*decimal(config["stop_atr"]), 3*entry*decimal(feature.get("sigma") or 0))
    low = decimal(feature.get("dip_low") or entry, positive=True)
    distance = max(noise, entry - low + 2*tick)
    if distance >= entry:
        return dict(reason="invalid_stop_distance")
    stop = price_floor(units, entry-distance)
    cushion = max(2*spread, atr/2)
    limit = price_floor(units, stop-cushion)
    if limit <= 0:
        return dict(reason="invalid_stop_limit")
    unit_loss = entry-limit + entry*maker + limit*taker
    risk = min(decimal(equity)*decimal(config["risk_fraction"]), decimal(daily_remaining))
    band = decimal(config["depth_ticks"])*tick
    near_bid = sum((q for p, q in bids if p >= entry-band), D(0))
    near_ask = sum((q for p, q in asks if p <= ask+band), D(0))
    caps = dict(cash=decimal(cash)*decimal(config["cash_fraction"])/(entry*(1+maker)),
                risk=risk/unit_loss,
                depth=min(near_bid, near_ask)*decimal(config["depth_fraction"]),
                volume=decimal(volume_10s)*decimal(config["volume_fraction"]),
                exchange_qty=decimal(market["max_qty"]),
                exchange_amount=decimal(market["max_order_amount"])/(entry*(1+maker)))
    qty = floor(min(caps.values()), decimal(market["qty_unit"], positive=True))
    minimum = decimal(market["min_order_amount"])
    if qty < decimal(market["min_qty"]) or qty*limit < minimum or qty*entry < minimum:
        return dict(reason="minimum_order_exceeds_size", caps={k:str(v) for k,v in caps.items()})
    return dict(reason=None, qty=str(qty), entry=str(entry), stop=str(stop), stop_limit=str(limit),
                notional_krw=str(qty*entry), nominal_loss_krw=str(qty*unit_loss), risk_budget_krw=str(risk),
                binding=min(caps, key=caps.get), caps={k:str(v) for k,v in caps.items()},
                maker=str(maker), taker=str(taker), atr=str(atr), tick=str(tick))
