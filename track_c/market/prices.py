"""Exchange price ladder helpers and execution-report parameter names."""
from decimal import Decimal as D
from track_c.execution.sizing import price_floor, price_unit
from track_c.market.exit_settings import DEFAULTS as EXIT_DEFAULTS
VERSION = 'c4-runtime-layout1'
PARAMS = ('entry_ticks', 'cancel_ticks', 'defend_ticks', 'stop_ticks', 'hold_s', 'target_ticks', 'max_spread_ticks', 'notional_krw', 'entry_ttl_s',
          'momentum_window_s', 'momentum_recent_s', 'momentum_veto_ticks', 'momentum_decel_share', 'flow_gate') + tuple(EXIT_DEFAULTS)

def price_up(units, price, ticks):
    """Walk `ticks` valid price units upward from an on-ladder price."""
    value = D(str(price))
    for _ in range(int(ticks)):
        value += price_unit(units, value)
    return price_floor(units, value)

def price_down(units, price, ticks):
    """Use the step immediately below a boundary, not the step above it."""
    value = D(str(price))
    for _ in range(int(ticks)):
        if value <= 0:
            return D(0)
        below = value - price_unit(units, value.next_minus())
        if below <= 0:
            return D(0)
        value = price_floor(units, below)
    return value
