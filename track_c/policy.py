"""Joint symbol/size/time decisions from execution-conditioned wealth outcomes."""
from decimal import Decimal as D, ROUND_CEILING
from itertools import product
import math
import statistics

from .estimation import predict, predict_many
from .microstructure import liquidate
from .outcomes import action_features
from .sizing import floor, price_floor
from .universe import asset_reason


class Policy:
    def __init__(self, artifact, config):
        self.artifact,self.config=artifact,config

    def assess(self, snapshot, contract, units, fees, *, equity, cash, risk_remaining, learning_spent=0):
        doc=self.artifact; coin=snapshot['coin']; f=snapshot['features']
        common=dict(coin=coin,t=snapshot['t'],model=doc['digest'],model_state=doc['state'],accepted=False)
        why=asset_reason(coin)
        if why: return dict(common,reason=why,actions=[])
        if snapshot['t']<=doc['trained_until']: return dict(common,reason='future_model',actions=[])
        if snapshot['history_s']<max(doc['feature_windows']): return dict(common,reason='history_warmup',actions=[])
        if any(float(v) for v in fees.values()): return dict(common,reason='unreconciled_fee_currency',actions=[])
        if not all(doc['models'].get(k) for k in ('fill','net','adverse','continuation')):
            return dict(common,reason='model_evidence_unavailable',actions=[])
        W=float(equity); cash=float(cash); risk=min(float(risk_remaining),W*float(self.config['risk_fraction']))
        if W<=0 or risk<=0: return dict(common,reason='risk_budget',actions=[])
        entry=snapshot['bid']; qstep=D(contract['qty_unit']); minimum=float(contract['min_order_amount'])
        min_q=float((D(str(minimum/entry))/qstep).to_integral_value(rounding=ROUND_CEILING)*qstep)
        max_q=min(cash*float(self.config['cash_fraction'])/entry,float(contract['max_qty']),float(contract['max_order_amount'])/entry,
                  sum(q for _,q in snapshot['bids']),doc['quantity_support_krw'][1]/entry)
        quantities=[]; q=min_q
        while q<=max_q*(1+1e-10):
            quantities.append(q); q*=2
        if max_q>=min_q: quantities.append(float(floor(D(str(max_q)),qstep)))
        actions=[]; best=None
        grid=list(product(sorted(set(quantities)),doc['ttl_grid'],doc['horizon_grid'],doc.get('target_grid',[0])))
        candidates=max(1,len(grid))
        z=statistics.NormalDist().inv_cdf(1-doc['confidence_alpha']/candidates)
        features=[action_features(snapshot,*a) for a in grid]
        predictions=[predict_many(doc['models'][name],features) for name in ('fill','net','adverse')]
        for (quantity,ttl,horizon,target_ticks),pf,net,tail in zip(grid,*predictions):
                    supported=all(p['supported'] for p in (pf,net,tail))
                    cost=liquidate(snapshot['bids'],quantity)
                    if cost is None: continue
                    slip=max(0,(entry-cost)/entry*10000)
                    # Protection combines a learned adverse-tail estimate with current
                    # executable spread/depth, never a volatility-multiple entry rule.
                    loss_bp=max(f['spread_bp']+slip,math.expm1(min(math.log1p(10000),max(0,tail['mean']+z*tail['se']+max(tail['residuals'])))))
                    if loss_bp>=10000 or not math.isfinite(loss_bp): continue
                    stop=price_floor(units,D(str(entry*(1-loss_bp/10000))))
                    raw_limit=stop-D(str(snapshot['tick']+entry*slip/10000))
                    if raw_limit<=0: continue
                    limit=price_floor(units,raw_limit)
                    if limit<=0 or stop>=D(str(entry)): continue
                    unit_loss=D(str(entry))-limit
                    min_exit_q=float((D(str(minimum))/limit/qstep).to_integral_value(rounding=ROUND_CEILING)*qstep)
                    if quantity<min_exit_q or quantity*float(unit_loss)>risk: continue
                    nominal=entry*quantity; returns=[(net['mean']-z*net['se']+r)/10000 for r in net['residuals']]
                    logs=[math.log1p(max(-.999999,nominal/W*r)) for r in returns]
                    growth=statistics.mean((pf['lower'] if v>=0 else pf['upper'])*v for v in logs)
                    occupied=max(.001,(1-pf['mean'])*ttl+pf['mean']*(ttl+horizon))
                    tail_losses=sorted([max(0,-r*nominal) for r in returns],reverse=True)
                    cvar=statistics.mean(tail_losses[:max(1,math.ceil(len(tail_losses)*doc['confidence_alpha']))])
                    valid=supported and growth>0 and cvar<=risk
                    # Explicit live-learning state: exchange-minimum feasible size,
                    # positive modeled mean, no unsupported extrapolation. Its total
                    # realized loss uses the existing per-attempt risk allowance.
                    research=(doc['state']=='research' and self.config.get('learning_enabled',False)
                              and supported and net['mean']>0 and quantity<2*min_exit_q
                              and learning_spent+quantity*float(unit_loss)<=W*float(self.config['risk_fraction']) and pf['lower']>0)
                    permit=valid and doc['state']=='validated' or research
                    score=(growth if valid else pf['mean']*net['mean']/10000*nominal/W)/occupied
                    action=dict(quantity=quantity,ttl=ttl,horizon=horizon,target_ticks=target_ticks,p_fill=pf['mean'],p_fill_low=pf['lower'],p_fill_high=pf['upper'],
                                net_bp=net['mean'],net_low_bp=net['mean']-z*net['se'],growth_lower=growth,score=score,
                                tail_loss_krw=cvar,supported=supported,permitted=permit,research=research,
                                stop=str(stop),stop_limit=str(limit),nominal_loss_krw=str(quantity*float(unit_loss)))
                    actions.append(action)
                    if permit and (best is None or score>best['score']): best=action
        if best is None: return dict(common,reason='no_supported_positive_action',actions=actions)
        plan=dict(reason=None,qty=str(floor(D(str(best['quantity'])),qstep)),entry=str(entry),stop=best['stop'],stop_limit=best['stop_limit'],
                  notional_krw=str(best['quantity']*entry),nominal_loss_krw=best['nominal_loss_krw'],maker=str(fees['maker']),taker=str(fees['taker']),
                  policy='quantitative',model=doc['digest'],entry_ttl_s=best['ttl'],hold_limit_s=best['ttl']+best['horizon'],
                  take_profit=str(price_floor(units,D(str(entry+best['target_ticks']*snapshot['tick'])))) if best['target_ticks'] else None,
                  target_ticks=best['target_ticks'],
                  horizon_s=best['horizon'],research=best['research'],score=best['score'],expected_net_bp=best['net_bp'],p_fill=best['p_fill'])
        return dict(common,accepted=True,reason='research' if best['research'] else 'positive_growth',best=best,plan=plan,actions=actions)

    def continuation(self, snapshot, quantity):
        model=self.artifact['models'].get('continuation')
        if not model: return dict(hold=False,reason='model_unavailable')
        rows=[]
        for horizon in self.artifact['horizon_grid']:
            p=predict(model,action_features(snapshot,quantity,0,horizon))
            rows.append(dict(horizon=horizon,net_bp=p['mean'],lower=p['lower'],supported=p['supported']))
        best=max(rows,key=lambda r:r['lower']/r['horizon'])
        return dict(hold=best['supported'] and best['lower']>0,reason='continuation_value',best=best,alternatives=rows)
