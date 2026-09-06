"""Coinone quantity and variable price-unit arithmetic."""
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR

from track_c.execution.coinone import CoinoneError, decimal


def floor_step(value, step):
    value, step = decimal(value), decimal(step, positive=True)
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_step(value, step):
    value, step = decimal(value), decimal(step, positive=True)
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def price_unit(units, price):
    price = decimal(price)
    rows = sorted((decimal(row["range_min"]), decimal(row["price_unit"], positive=True)) for row in units)
    choices = [step for start, step in rows if start <= price]
    if not choices:
        raise CoinoneError("price unit unavailable")
    return choices[-1]


def price_floor(units, value):
    value = decimal(value, positive=True)
    for _ in range(5):
        step = price_unit(units, value)
        result = floor_step(value, step)
        if result > 0 and price_unit(units, result) == step:
            return result
        value = result
    raise CoinoneError("price floor boundary unresolved")


def price_ceil(units, value):
    value = decimal(value, positive=True)
    for _ in range(5):
        step = price_unit(units, value)
        result = ceil_step(value, step)
        if price_unit(units, result) == step:
            return result
        value = result
    raise CoinoneError("price ceiling boundary unresolved")


def price_down(units, value, ticks=1):
    if type(ticks) is not int or ticks < 1:
        raise ValueError("ticks must be a positive integer")
    value = price_floor(units, value)
    rows = sorted(
        (decimal(row["range_min"]), decimal(row["price_unit"], positive=True))
        for row in units
    )
    for _ in range(int(ticks)):
        index = max(i for i, (start, _) in enumerate(rows) if start <= value)
        step = rows[index - 1][1] if index and value == rows[index][0] else rows[index][1]
        probe = value - step
        if probe <= 0:
            raise CoinoneError("price ladder exhausted")
        value = price_floor(units, probe)
    return value


def stop_prices(units, raw_trigger, buffer_ticks, buffer_bp):
    trigger = price_floor(units, raw_trigger)
    by_ticks = price_down(units, trigger, buffer_ticks)
    by_bp = price_floor(units, trigger * (D(1) - decimal(buffer_bp) / D(10000)))
    limit = min(by_ticks, by_bp)
    if not 0 < limit < trigger:
        raise CoinoneError("invalid stop-limit ladder")
    return trigger, limit
