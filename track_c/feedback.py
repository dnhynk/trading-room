"""Mature exchange attempts and prequential calibration, separate from tape labels."""
from collections import Counter
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import statistics

from .outcomes import action_features


def extract(directory,cutoff_ms):
    path=(Path(directory)/'ledger.sqlite').resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        records=db.execute('SELECT seq,t_ms,kind,body FROM events WHERE t_ms<=? ORDER BY seq',(cutoff_ms,)).fetchall()
    decisions={}; active={}; latency=[]; rows=[]; seen=set(); predicted=[]
    for seq,t,kind,text in records:
        b=json.loads(text); coin=b.get('coin')
        if kind=='MODEL_DECISION': decisions[coin]=b
        elif kind=='CAMPAIGN_INTENT' and b.get('plan',{}).get('policy')=='quantitative':
            if coin in decisions: active[coin]=dict(decision=decisions[coin],worst=0.,rtts=[])
        elif kind=='ORDER_SUBMITTED' and b.get('rtt_ms') is not None:
            rtt=float(b['rtt_ms'])
            if math.isfinite(rtt) and rtt>=0: latency.append(rtt)
        elif kind=='MARK' and coin in active:
            entry=float(active[coin]['decision']['plan']['entry'])
            active[coin]['worst']=max(active[coin]['worst'],max(0,(entry-float(b['bid']))/entry*10000))
        elif kind in ('CLOSE','NO_FILL'):
            camp=b['campaign']; coin=camp.get('coin',coin); key=camp.get('id')
            context=active.pop(coin,None)
            if not context or not key or key in seen: continue
            seen.add(key); decision=context['decision']; plan=decision['plan']; snap=decision['snapshot']
            q=float(plan['qty']); entry=float(plan['entry']); fraction=float(camp['bought'])/q
            if t<=snap['t'] or t>cutoff_ms: continue
            row=dict(t=snap['t'],end=t,coin=coin,entry=entry,quantity=q,ttl=plan['entry_ttl_s'],horizon=plan['horizon_s'],
                     target_ticks=plan.get('target_ticks',0),x=action_features(snap,q,plan['entry_ttl_s'],plan['horizon_s'],plan.get('target_ticks',0)),
                     filled=float(fraction>0),fill_fraction=fraction,net_bp=float(camp['net'])/(entry*q)*10000,
                     adverse_bp=context['worst'],source='exchange_completed',campaign_id=key,model=plan['model'],
                     predicted_fill=float(plan['p_fill']),predicted_net=float(plan['expected_net_bp']))
            rows.append(row)
    blocks=len({r['t']//60000 for r in rows})
    report=dict(cutoff_ms=cutoff_ms,completed_attempts=len(rows),filled_attempts=sum(bool(r['filled']) for r in rows),effective_time_blocks=blocks,
                fill_brier=statistics.mean((r['filled']-r['predicted_fill'])**2 for r in rows) if rows else None,
                conditional_bias_bp=statistics.mean(r['net_bp']-r['predicted_net'] for r in rows if r['filled']) if any(r['filled'] for r in rows) else None,
                submit_rtt_p50_ms=statistics.median(latency) if latency else None,
                submit_rtt_p95_ms=sorted(latency)[min(len(latency)-1,math.ceil(.95*len(latency))-1)] if latency else None,
                source='exchange_completed_only',open_attempts_excluded=len(active),by_model=dict(Counter(r['model'] for r in rows)))
    # Sequential residual alarm: estimated bias interval, not a profitability test.
    residuals=[r['net_bp']-r['predicted_net'] for r in rows if r['filled']]
    groups={}
    for r in rows:
        if r['filled']: groups.setdefault(r['t']//60000,[]).append(r['net_bp']-r['predicted_net'])
    means=[statistics.mean(v) for v in groups.values()]
    upper=statistics.mean(means)+3*statistics.stdev(means)/math.sqrt(len(means)) if len(means)>1 else None
    report.update(negative_drift=upper is not None and upper<0,net_bias_upper_3se=upper,drift_model=rows[-1]['model'] if rows else None)
    return rows,report
