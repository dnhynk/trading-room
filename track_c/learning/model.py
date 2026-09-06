"""Local empirical outcomes, independent episode support and paired exit value.

Cash targets already include no fills: never multiply them by fill probability.
Intervals describe sampling uncertainty conditional on this replay, not PnL bounds.
"""
from collections import defaultdict
import math
from statistics import NormalDist

from track_c.learning.config import digest
from track_c.learning.features import WIDTHS, HOLD_WIDTHS, distance, parent, vector


def unique_rows(rows,until,*,paired=False):
    found=set()
    for row in rows:
        if row['start_ms']>=until: continue
        key=(row['episode_id'],row['action']['id'],row.get('landmark_ms') if paired else None)
        if key in found: raise ValueError('duplicate episode/action/landmark')
        found.add(key)
        r=dict(row)
        if r['end_ms']>=until: r['censored']=True
        yield r


def neighborhood(rows,action,*,x=None,paired=False):
    """One nearest real observation per episode, never six independent actions."""
    chosen={}
    query=action.get('x') if x is None else x
    for r in rows:
        if parent(r['action'])!=parent(action): continue
        d=distance(query,r.get('x') if paired else r['action'].get('x'),HOLD_WIDTHS if paired else WIDTHS)
        if d>1+1e-12: continue
        rank=(d,r['action']['id'],r.get('landmark_ms',0))
        ep=r['episode_id']
        if ep not in chosen or rank<chosen[ep][0]: chosen[ep]=(rank,r,math.exp(-2*d))
    return [(r,w) for _,r,w in chosen.values()]


def weighted(rows,field):
    total=sum(w for _,w in rows)
    return sum(w*r[field] for r,w in rows)/total if total else None


def interval(rows,field,cfg,comparisons=6):
    rows=[(r,w) for r,w in rows if not r['censored']]
    mean=weighted(rows,field)
    total=sum(w for _,w in rows)
    ess=total*total/sum(w*w for _,w in rows) if total else 0.
    blocks=defaultdict(float)
    for r,w in rows: blocks[r['start_ms']//cfg['block_ms']]+=w*(r[field]-mean)
    n=len(blocks)
    out=dict(mean=mean,lower=None,upper=None,episodes=len(rows),effective_episodes=ess,blocks=n)
    if n<cfg['min_blocks'] or not total: return out
    se=math.sqrt(n/(n-1)*sum(v*v for v in blocks.values()))/total
    z=NormalDist().inv_cdf(1-cfg['alpha']/max(2,comparisons+1))
    width=z*math.sqrt(n/max(1,n-2))*se
    return dict(out,lower=mean-width,upper=mean+width,se=se)


def support(rows,cfg,*,fills=True):
    known=[(r,w) for r,w in rows if not r['censored']]
    filled=[(r,w) for r,w in known if r.get('filled_qty',0)>0]
    weights=[w for _,w in known]
    ess=sum(weights)**2/sum(w*w for w in weights) if weights else 0.
    blocks=len({r['start_ms']//cfg['block_ms'] for r,_ in known})
    unknown=sum(r['censored'] for r,_ in rows)
    # Missing outcomes can be informative. Do not silently fit successful survivors.
    reason=('unresolved_neighbor_outcomes' if unknown else 'joint_support'
            if len(known)<(cfg['min_attempts'] if fills else cfg['min_fills'])
            or ess+1e-8<(cfg['min_attempts'] if fills else cfg['min_fills'])
            or blocks<cfg['min_blocks'] or (fills and len(filled)<cfg['min_fills']) else None)
    return dict(ready=reason is None,reason=reason,episodes=len(known),filled_episodes=len(filled),
                censored_episodes=unknown,blocks=blocks,effective_episodes=ess)


def lower_tail(rows,fraction=.1):
    total=sum(w for _,w in rows)*fraction
    left=total; value=0.
    for r,w in sorted(rows,key=lambda rw:rw[0]['net_bp']):
        take=min(w,left);value+=take*r['net_bp'];left-=take
        if left<=1e-12:break
    return max(0.,-value/total) if total else 0.


class CashModel:
    def __init__(self,doc,cfg):
        if doc['digest']!=digest({k:v for k,v in doc.items() if k!='digest'}) or doc['config']!=digest(cfg):
            raise ValueError('refined cash identity mismatch')
        if doc.get('widths')!=WIDTHS:raise ValueError('refined feature specification mismatch')
        self.doc,self.cfg=doc,cfg

    @classmethod
    def fit(cls,rows,until,cfg,exit_digest):
        saved=[]
        for r in unique_rows(rows,until):
            if r.get('exit_protocol')!=cfg['exit_protocol']:raise ValueError('entry label exit protocol mismatch')
            if r['exit_model']!=exit_digest:raise ValueError('entry labels used another exit policy')
            if r['start_ms']<=r.get('exit_trained_until',-1):raise ValueError('exit training leaked into entry labels')
            saved.append(r)
        doc=dict(kind='c4-local-cash-v2',trained_until=until,config=digest(cfg),exit_model=exit_digest,
                 widths=WIDTHS,rows=saved)
        doc['digest']=digest(doc)
        return cls(doc,cfg)

    def predict(self,action,now,comparisons=6):
        if now<=self.doc['trained_until']:return dict(ready=False,reason='future_model',score_krw=None)
        local=neighborhood(self.doc['rows'],action)
        s=support(local,self.cfg)
        rows=[(r,w) for r,w in local if not r['censored']]
        filled=[(r,w) for r,w in rows if r['filled_qty']>0]
        bounds=interval(local,'net_bp',self.cfg,comparisons)
        total=sum(w for _,w in rows);fw=sum(w for _,w in filled)
        scale=action['notional']/10000
        empirical=fw/total if total else None
        expected=None if bounds['mean'] is None else bounds['mean']*scale
        conditional=weighted(filled,'net_bp')
        out=dict(s,score_krw=None,expected_net_krw=expected,net_interval_bp=bounds,
                 value_basis='public_replay_execution_hypothesis',live_execution_verified=False,
                 exit_protocol=self.cfg['exit_protocol'],
                 p_fill=(fw+.5)/(total+1) if total else None,p_fill_empirical=empirical,
                 expected_fill_fraction=weighted(rows,'fill_fraction'),
                 fill_conditioned_net_krw=conditional*scale if conditional is not None else None,
                 expected_gross_recovery_krw=weighted(rows,'gross_bp')*scale if rows else None,
                 expected_spend_krw=weighted(rows,'spent_bp')*scale if rows else None,
                 probability_correction='none; Beta smoothing is not held-out calibration',
                 target='net_cash_recovered_per_attempt; unsold_inventory_not_liquidated')
        if not s['ready'] or bounds['lower'] is None:return out
        tail=lower_tail(filled)
        time_cost=self.cfg['capital_cost_bp_hour']*weighted(rows,'occupied_s')/3600
        penalty=self.cfg['risk_aversion']*tail
        raw=bounds['lower']*scale
        score=raw-(penalty+time_cost)*scale
        return dict(out,reason='positive_value' if score>0 else 'nonpositive_value',score_krw=score,
                    cash_lower_krw=raw,inventory_penalty_krw=penalty*scale,time_penalty_krw=time_cost*scale)


class HazardModel:
    def __init__(self,doc,cfg):
        if doc['digest']!=digest({k:v for k,v in doc.items() if k!='digest'}) or doc['config']!=digest(cfg):
            raise ValueError('refined exit identity mismatch')
        if doc.get('widths')!=WIDTHS or doc.get('hold_widths')!=HOLD_WIDTHS:
            raise ValueError('refined feature specification mismatch')
        self.doc,self.cfg=doc,cfg

    @classmethod
    def fit(cls,rows,pairs,until,cfg):
        rows=list(unique_rows(rows,until));pairs=list(unique_rows(pairs,until,paired=True))
        if any(r.get('exit_protocol')!=cfg['exit_protocol'] for r in rows+pairs):
            raise ValueError('exit label protocol mismatch')
        if any(r['exit_model']!='structural' for r in rows):raise ValueError('fixed structural exit labels required')
        doc=dict(kind='c4-paired-exit-v2',trained_until=until,config=digest(cfg),widths=WIDTHS,
                 hold_widths=HOLD_WIDTHS,rows=[r for r in rows if r['filled_qty']>0 or r['censored']],pairs=pairs,
                 continuation_policy='fixed_structural_until_end')
        doc['digest']=digest(doc)
        return cls(doc,cfg)

    def survival(self,action):
        local=neighborhood(self.doc['rows'],action)
        status=support(local,self.cfg,fills=False)
        status.update(probability_basis='public_replay_execution_hypothesis',live_execution_verified=False)
        incidence=dict(recovery=0.,collapse=0.,timeout=0.)
        survival=1.;left=-1.;out=[]
        for right in self.cfg['hazard_seconds']:
            # Retain censored subjects until last observed time, never as no-event endings.
            risk=[(r,w) for r,w in local if r['filled_qty']>0 and r['duration_s']>left]
            exposure=sum(w for _,w in risk)
            events={cause:sum(w for r,w in risk if not r['censored'] and r['duration_s']<=right and r['cause']==cause)
                    for cause in incidence}
            if exposure:
                prior=self.cfg['hazard_prior']
                probs={cause:(n+prior/4)/(exposure+prior) for cause,n in events.items()}
                for cause,p in probs.items():incidence[cause]+=survival*p
                survival*=1-sum(probs.values())
                out.append(dict(seconds=right,**status,at_risk_episodes=len(risk),hazard=probs,
                                survival=survival,incidence=dict(incidence)))
            else:
                # With no surviving observations, prior pseudo-observations cannot invent a forecast.
                out.append(dict(seconds=right,**status,at_risk_episodes=0,hazard=None,
                                survival=survival if local else None,incidence=dict(incidence) if local else None))
            left=right
        return out

    def continuation(self,action,age,now,vwap=None,*,state=None,qty=None):
        if now<=self.doc['trained_until']:return dict(ready=False,reason='future_model')
        x=vector(action,state,self.cfg,qty=qty,age=age)
        local=neighborhood(self.doc['pairs'],action,x=x,paired=True)
        s=support(local,self.cfg,fills=False)
        bounds=interval(local,'delta_bp',self.cfg)
        known=[rw for rw in local if not rw[0]['censored']]
        scale=(qty or action['qty'])*action['price']/10000
        out=dict(s,hold=None,paired_interval_bp=bounds,
                 value_basis='public_replay_execution_hypothesis',live_execution_verified=False,
                 incremental_mean_krw=bounds['mean']*scale if bounds['mean'] is not None else None,
                 expected_hold_recovery_krw=weighted(known,'hold_bp')*scale if known else None,
                 expected_sell_recovery_krw=weighted(known,'sell_bp')*scale if known else None,
                 target=self.doc['continuation_policy'])
        if not s['ready'] or bounds['lower'] is None:return out
        extra_time=max(0.,weighted(known,'extra_occupied_s'))
        cost=self.cfg['capital_cost_bp_hour']*extra_time/3600
        lower=bounds['lower']-cost
        return dict(out,hold=lower>0,incremental_lower_krw=lower*scale,time_penalty_krw=cost*scale)
