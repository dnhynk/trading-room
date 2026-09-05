"""C3 rule policy: fair-value gated passive bid, resting +k tick sell, defensive exits.

Defaults are declared before outcome comparisons. Adaptive risk uses causal price
history; its uncertainty buffer is not proof of alpha. Live and replay share this code.
"""
from decimal import Decimal as D, ROUND_CEILING
import math

from .sizing import floor, price_floor, price_unit
from .exit_model import DEFAULTS as EXIT_DEFAULTS

VERSION = 'c3-rule-v4'
MINIMUM_EXIT_BUFFER = D('1.05')  # Same sale-notional buffer used by the OMS.
PARAMS = ('entry_ticks', 'cancel_ticks', 'defend_ticks', 'stop_ticks', 'hold_s', 'target_ticks', 'max_spread_ticks', 'notional_krw', 'entry_ttl_s',
          'momentum_window_s', 'momentum_recent_s', 'momentum_veto_ticks', 'momentum_decel_share', 'flow_gate') + tuple(EXIT_DEFAULTS)


def momentum_available(m30, m10):
    return all(x is not None and math.isfinite(x) for x in (m30, m10))


def leader_falling(cfg, m30, m10):
    return (momentum_available(m30, m10) and m30 <= -float(cfg['momentum_veto_ticks'])
            and m10 <= m30 * float(cfg['momentum_decel_share']))


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


def assess(cfg, *, coin, bid, ask, tick, dev, contract, units, cash, risk_remaining,
           flow32=None, m30=None, m10=None, risk=None, unconditional=False):
    """Entry plan or a refusal reason. `dev` is the fair-value deviation in ticks (None = unavailable)."""
    common = dict(coin=coin, accepted=False, dev_ticks=dev, policy=VERSION, flow32=flow32, m30=m30, m10=m10)
    if dev is None or not math.isfinite(dev):
        return dict(common, reason='fair_unavailable')
    if not all(math.isfinite(x) for x in (bid, ask, tick)) or not (0 < bid < ask) or tick <= 0:
        return dict(common, reason='invalid_book')
    if (ask - bid) / tick > float(cfg['max_spread_ticks']) + 1e-9:
        return dict(common, reason='wide_spread')
    if dev < float(cfg['entry_ticks']):
        return dict(common, reason='rich_vs_fair')
    if not unconditional:
        if dev + (ask-bid)/(2*tick) < float(cfg['target_ticks']) - 1e-12:
            return dict(common, reason='fair_below_take')
        if not momentum_available(m30, m10):
            return dict(common, reason='momentum_unavailable')
        if leader_falling(cfg, m30, m10):
            return dict(common, reason='leader_falling')
        if cfg['flow_gate']:
            if flow32 is None or not math.isfinite(flow32) or not -1 <= flow32 <= 1:
                return dict(common, reason='flow_unavailable')
            if flow32 > 0:
                return dict(common, reason='buy_dominant_flow')
    entry = D(str(bid)); step = D(contract['qty_unit']); minimum = D(contract['min_order_amount'])
    if entry != price_floor(units, entry):
        return dict(common, reason='price_ladder')
    qty = (D(str(cfg['notional_krw'])) / entry / step).to_integral_value(rounding=ROUND_CEILING) * step
    qty = min(qty, floor(D(contract['max_qty']), step))
    if qty < D(contract.get('min_qty', '0')) or qty*entry > D(contract.get('max_order_amount', 'Infinity')):
        return dict(common, reason='exchange_size_limit')
    base_qty = qty
    base_stop = price_down(units, entry, int(cfg['stop_ticks']))
    base_limit = price_down(units, base_stop, 1)
    base_loss = base_qty * (entry-base_limit)
    stop = base_stop
    adaptive = cfg.get('stop_mode', EXIT_DEFAULTS['stop_mode']) == 'volatility'
    if adaptive:
        distance = (risk or {}).get('distance_price')
        if not (risk or {}).get('ready') or distance is None or not math.isfinite(distance) or distance <= 0:
            return dict(common, reason='volatility_unavailable')
        distance = max(D(str(distance)), D(str(tick)), D(str(ask))-entry)
        if distance >= entry:
            return dict(common, reason='invalid_stop_distance')
        stop = price_floor(units, entry-distance)
    stop_limit = price_down(units, stop, 1)
    take = price_up(units, entry, int(cfg['target_ticks']))
    if not (0 < stop_limit < stop < entry < take):
        return dict(common, reason='price_ladder')
    target_qty = base_qty
    minimum_exit_qty = D(0)
    if adaptive:
        target_qty = min(base_qty, floor(base_loss/(entry-stop_limit), step))
        minimum_exit_qty = (max(D(contract.get('min_qty', '0')), minimum*MINIMUM_EXIT_BUFFER/stop_limit)
                            / step).to_integral_value(rounding=ROUND_CEILING)*step
        # The user authorized a legal-size floor on 2026-09-06. The old fixed-stop
        # loss is a sizing target, while cash and portfolio risk stay hard limits.
        qty = max(target_qty, minimum_exit_qty)
        if qty > base_qty:
            return dict(common, reason='minimum_exceeds_order_budget', required_qty=str(qty),
                        base_qty=str(base_qty), risk_target_qty=str(target_qty),
                        minimum_exit_qty=str(minimum_exit_qty))
    # Multi-tick targets crossing a ladder boundary may cost more than k*current_tick.
    if not unconditional and dev + (ask-bid)/(2*tick) < float((take-entry)/D(str(tick))) - 1e-12:
        return dict(common, reason='fair_below_take')
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
                target_ticks=int(cfg['target_ticks']), tick=str(tick), dev_ticks=dev, flow32=flow32, m30=m30, m10=m10,
                research=True, score=dev, expected_net_bp=None, p_fill=None,
                stop_mode='volatility' if adaptive else 'fixed', entry_risk=dict(risk or {}),
                base_qty=str(base_qty), base_nominal_loss_krw=str(base_loss),
                risk_target_qty=str(target_qty), minimum_exit_qty=str(minimum_exit_qty),
                minimum_exit_buffer=str(MINIMUM_EXIT_BUFFER) if adaptive else None,
                minimum_size_uplift_qty=str(qty-target_qty), minimum_size_uplift_krw=str((qty-target_qty)*entry),
                minimum_risk_excess_krw=str(max(D(0), loss-base_loss)))
    return dict(common, accepted=True, reason='rule', plan=plan, best=dict(score=dev))


def hold(cfg, *, dev, bid, entry, tick, age_s, stop=None, m30=None, m10=None, flow32=None, continuation=None, unconditional=False):
    """Holding decision after a fill. Unknown fair value keeps stop/time protection only."""
    floor = float(stop) if stop is not None else entry - float(tick) * int(cfg['stop_ticks'])
    if bid is not None and bid <= floor + 1e-12:
        return dict(hold=False, reason='stop')
    if not unconditional and leader_falling(cfg, m30, m10):
        return dict(hold=False, reason='brake')
    if dev is not None and math.isfinite(dev) and dev < float(cfg['defend_ticks']):
        return dict(hold=False, reason='defend')
    if age_s is not None and age_s >= float(cfg['hold_s']):
        return dict(hold=False, reason='time')
    if (not unconditional and cfg.get('value_exit', EXIT_DEFAULTS['value_exit']) and age_s is not None
            and (continuation or {}).get('ready') and (continuation or {}).get('exit')):
        upper = continuation.get('upper_ticks')
        if upper is not None and math.isfinite(upper) and upper <= 0:
            return dict(hold=False, reason='value', continuation=continuation)
    return dict(hold=True, reason=None, **({'continuation': continuation} if continuation is not None else {}))


def cancel_entry(cfg, *, dev, m30=None, m10=None, flow32=None, risk=None, unconditional=False):
    """A resting bid is withdrawn when Coinone turns rich versus fair or the fair value is unknown."""
    return (dev is None or not math.isfinite(dev) or dev < float(cfg['cancel_ticks'])
            or (cfg.get('stop_mode', EXIT_DEFAULTS['stop_mode']) == 'volatility' and not (risk or {}).get('ready'))
            or (not unconditional and (not momentum_available(m30, m10) or leader_falling(cfg, m30, m10))))
