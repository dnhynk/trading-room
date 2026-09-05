"""C3 rule policy: fair-value gated passive bid, resting +k tick sell, defensive exits.

Every number here is a pre-registered structural parameter (track_c/AUDIT-20260905.md
section 7), not a coefficient fitted to a tape. The same functions run in replay and live.
"""
from decimal import Decimal as D, ROUND_CEILING
import math

from .sizing import floor, price_floor, price_unit

VERSION = 'c3-rule-v2'
PARAMS = ('entry_ticks', 'cancel_ticks', 'defend_ticks', 'stop_ticks', 'hold_s', 'target_ticks', 'max_spread_ticks', 'notional_krw', 'entry_ttl_s')


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
        value = price_floor(units, value - price_unit(units, value.next_minus()))
    return value


def assess(cfg, *, coin, bid, ask, tick, dev, contract, units, cash, risk_remaining):
    """Entry plan or a refusal reason. `dev` is the fair-value deviation in ticks (None = unavailable)."""
    common = dict(coin=coin, accepted=False, dev_ticks=dev, policy=VERSION)
    if dev is None or not math.isfinite(dev):
        return dict(common, reason='fair_unavailable')
    if not all(math.isfinite(x) for x in (bid, ask, tick)) or not (0 < bid < ask) or tick <= 0:
        return dict(common, reason='invalid_book')
    if (ask - bid) / tick > float(cfg['max_spread_ticks']) + 1e-9:
        return dict(common, reason='wide_spread')
    if dev < float(cfg['entry_ticks']):
        return dict(common, reason='rich_vs_fair')
    entry = D(str(bid)); step = D(contract['qty_unit']); minimum = D(contract['min_order_amount'])
    if entry != price_floor(units, entry):
        return dict(common, reason='price_ladder')
    qty = (D(str(cfg['notional_krw'])) / entry / step).to_integral_value(rounding=ROUND_CEILING) * step
    qty = min(qty, floor(D(contract['max_qty']), step))
    if qty < D(contract.get('min_qty', '0')) or qty*entry > D(contract.get('max_order_amount', 'Infinity')):
        return dict(common, reason='exchange_size_limit')
    stop = price_down(units, entry, int(cfg['stop_ticks']))
    stop_limit = price_down(units, stop, 1)
    take = price_up(units, entry, int(cfg['target_ticks']))
    if not (0 < stop_limit < stop < entry < take):
        return dict(common, reason='price_ladder')
    if qty * stop_limit < minimum:
        return dict(common, reason='minimum_at_stop')
    if qty * entry > D(str(cash)) * D(str(cfg['cash_fraction'])):
        return dict(common, reason='cash')
    loss = qty * (entry - stop_limit)
    if loss > D(str(risk_remaining)):
        return dict(common, reason='risk_budget')
    plan = dict(reason=None, qty=str(qty), entry=str(entry), stop=str(stop), stop_limit=str(stop_limit), take_profit=str(take),
                notional_krw=str(qty * entry), nominal_loss_krw=str(loss), maker='0', taker='0', policy='rule', model=VERSION,
                take_mode='resting', entry_ttl_s=int(cfg['entry_ttl_s']), hold_limit_s=int(cfg['hold_s']), horizon_s=int(cfg['hold_s']),
                target_ticks=int(cfg['target_ticks']), tick=str(tick), dev_ticks=dev, research=True, score=dev, expected_net_bp=None, p_fill=None)
    return dict(common, accepted=True, reason='rule', plan=plan, best=dict(score=dev))


def hold(cfg, *, dev, bid, entry, tick, age_s, stop=None):
    """Holding decision after a fill. Unknown fair value keeps stop/time protection only."""
    floor = float(stop) if stop is not None else entry - float(tick) * int(cfg['stop_ticks'])
    if bid is not None and bid <= floor + 1e-12:
        return dict(hold=False, reason='stop')
    if dev is not None and math.isfinite(dev) and dev < float(cfg['defend_ticks']):
        return dict(hold=False, reason='defend')
    if age_s is not None and age_s >= float(cfg['hold_s']):
        return dict(hold=False, reason='time')
    return dict(hold=True, reason=None)


def cancel_entry(cfg, *, dev):
    """A resting bid is withdrawn when Coinone turns rich versus fair or the fair value is unknown."""
    return dev is None or not math.isfinite(dev) or dev < float(cfg['cancel_ticks'])
