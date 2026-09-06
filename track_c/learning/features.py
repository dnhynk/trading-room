"""Existing observable state and order geometry; no new market data or gates."""
import math

from track_c.market.state import candidates as original_candidates
from track_c.market.microstructure import liquidate

# Outcome-independent neighborhood widths. They are hypotheses, not tuned values.
# A joint squared distance <=1 is required; marginal min/max is insufficient.
WIDTHS = dict(edge_ticks=2., log_pressure=math.log(2), external_10_ticks=1.,
              flow=1., spread_ticks=1., log_queue=math.log(4),
              liquidation_ticks=2., log_notional=math.log(2), risk_ticks=2.)
HOLD_WIDTHS = {k:v for k,v in WIDTHS.items() if k != 'log_queue'}
HOLD_WIDTHS.update(age_fraction=.2, remaining_fraction=.5)


def vector(action, state, cfg, *, qty=None, age=None):
    """Quantities refer to this candidate (or this remaining inventory)."""
    if not state or not state['reference'].get('ready'):
        return None
    ref=state['reference']
    if ref.get('m10') is None: return None
    q=action['qty'] if qty is None else qty
    if q <= 0: return None
    tick=state['tick']
    bids=[(p,v*cfg['depth_haircut']) for p,v in state['bids']]
    vwap=liquidate(bids,q)
    if vwap is None: return None
    ahead=sum(v for p,v in state['bids'] if p==action['price'])
    x=dict(edge_ticks=(ref['lower']-action['price'])/tick,
           log_pressure=math.log1p(state['pressure']),external_10_ticks=ref['m10'],
           flow=state['flow'],spread_ticks=(state['ask']-state['bid'])/tick,
           liquidation_ticks=(action['price']-vwap)/tick,
           log_notional=math.log(q*action['price']),
           risk_ticks=(action['price']-action['stop_limit'])/tick)
    if age is None:
        x['log_queue']=math.log1p(ahead/q)
    else:
        x.update(age_fraction=age/action['hold_s'],remaining_fraction=q/action['qty'])
    return x if all(math.isfinite(v) for v in x.values()) else None


def enrich(action, state, cfg):
    """Attach predictors without changing original price, size or eligibility."""
    result=dict(action,x=vector(action,state,cfg),
                admissible=bool(state.get('entry_eligible') and state.get('entry_fresh') and
                                state['reference'].get('lower',0)>action['price']),
                observation=dict(book_age_ms=state['t_ms']-state['book_ms'],
                                 external_30_ticks=state['reference'].get('m30')))
    return result


def candidates(state,cfg,cash,risk,*,research=False):
    return [enrich(a,state,cfg) for a in original_candidates(state,cfg,cash,risk,research=research)]


def parent(action):
    return action['coin'],bool(action.get('admissible'))


def distance(left,right,widths=WIDTHS):
    if not left or not right or set(left)!=set(widths) or set(right)!=set(widths): return math.inf
    if not all(type(v) in (int,float) and math.isfinite(v) for v in list(left.values())+list(right.values())):
        return math.inf
    return sum(((left[k]-right[k])/widths[k])**2 for k in widths)
