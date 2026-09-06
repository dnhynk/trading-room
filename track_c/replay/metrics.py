"""Predictions versus later outcomes, distinct from a shared-capital ledger."""
from collections import defaultdict


def _score(rows):
    """Each episode has total weight one across its counterfactual candidates."""
    groups=defaultdict(list)
    for r in rows:groups[r['episode_id']].append(r)
    weighted=[(r,1/len(group)) for group in groups.values() for r in group]
    n=len(groups)
    if not n:return dict(episodes=0,observations=0,mean_prediction=None,mean_observed=None,bias=None,mae=None,mse=None)
    return dict(episodes=n,observations=len(rows),
                mean_prediction=sum(w*r['prediction'] for r,w in weighted)/n,
                mean_observed=sum(w*r['observed'] for r,w in weighted)/n,
                bias=sum(w*(r['prediction']-r['observed']) for r,w in weighted)/n,
                mae=sum(w*abs(r['prediction']-r['observed']) for r,w in weighted)/n,
                mse=sum(w*(r['prediction']-r['observed'])**2 for r,w in weighted)/n)


def score_predictions(records):
    cash=[];ready_cash=[];fill=[];hazards=defaultdict(list)
    missing=defaultdict(int)
    for item in records:
        row=item['outcome'];pred=item['prediction'];ep=row['episode_id']
        if row['censored']:
            missing['censored_outcomes']+=1
        else:
            mean=pred.get('expected_net_krw')
            if mean is not None:
                point=dict(episode_id=ep,prediction=mean,observed=row['cash_net_krw'])
                cash.append(point)
                if pred.get('ready'):ready_cash.append(point)
            else:missing['cash_prediction_unavailable']+=1
            if pred.get('p_fill') is not None:
                fill.append(dict(episode_id=ep,prediction=pred['p_fill'],observed=float(row['filled_qty']>0)))
        # These probabilities target structural exits conditional on a fill.
        # Censored paths can contribute only horizons observed before the gap.
        if not row['filled_qty']:continue
        for point in item.get('probabilities',[]):
            horizon=point['seconds']
            if point.get('incidence') is None:continue
            if row['censored'] and (row.get('observation_gap') or row['duration_s']<horizon):
                missing['unobserved_probability_horizons']+=1
                continue
            for cause,p in point['incidence'].items():
                y=float(not row['censored'] and row['duration_s']<=horizon and row['cause']==cause)
                hazards[(horizon,cause)].append(dict(episode_id=ep,prediction=p,observed=y))
    reliability=[]
    for (seconds,cause),rows in sorted(hazards.items()):
        bins=[]
        for left,right in ((0.,.2),(.2,.4),(.4,.6),(.6,.8),(.8,1.0000001)):
            data=[r for r in rows if left<=r['prediction']<right]
            if data:bins.append(dict(lower=left,upper=min(1.,right),**_score(data)))
        reliability.append(dict(seconds=seconds,cause=cause,brier=_score(rows),bins=bins))
    return dict(order_cash_all_supported_means=_score(cash),order_cash_decision_ready=_score(ready_cash),
                fill_brier=_score(fill),structural_exit_probabilities=reliability,missing=dict(missing),
                probability_calibration_fitted=False,unit='one_episode; actions_share_weight',
                realization='public_counterfactual; not_exchange_fills')


def fill_selection(base,stress):
    """Changing queue assumptions can change who fills, not monotonically PnL."""
    def key(row):return row['episode_id'],row['action']['id']
    left={key(r):r for r in base};right={key(r):r for r in stress}
    a={k for k,r in left.items() if r['filled_qty']>0}
    b={k for k,r in right.items() if r['filled_qty']>0}
    def summary(keys,mapping):
        rows=[mapping[k] for k in keys]
        return dict(actions=len(rows),episodes=len({r['episode_id'] for r in rows}),
                    known_actions=sum(not r['censored'] for r in rows),
                    # This is a population diagnostic, never a portfolio total.
                    mean_known_cash_krw=sum(r['cash_net_krw'] for r in rows if not r['censored'])/
                    max(1,sum(not r['censored'] for r in rows)))
    return dict(common_fills=summary(a&b,left),base_only=summary(a-b,left),stress_only=summary(b-a,right),
                common_under_stress=summary(a&b,right),candidate_keys_equal=set(left)==set(right),
                interpretation='fill_selection_diagnostic_not_a_PnL_lower_bound')
