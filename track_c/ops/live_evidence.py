"""Read-only actual C4 order evidence. No exchange client, fitting or adoption."""
import argparse
from collections import Counter,defaultdict
from decimal import Decimal as D
import json
import math
from pathlib import Path
import sqlite3

from track_c.replay.metrics import score_predictions
from track_c.ops.store import encoded


def summarize(rows,start_ms,end_ms,model):
    campaigns={};orders={};decisions=Counter();latencies=defaultdict(list)
    timing_owners=defaultdict(set);timing_campaigns=defaultdict(set);procedure=Counter();episodes=set()
    def timing(key,value,owner,campaign):
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:
            procedure['invalid_timing_values']+=1;return
        latencies[key].append(value);timing_owners[key].add(owner);timing_campaigns[key].add(campaign)
    cash=D(0);peak=D(0);drawdown=D(0);turnover=D(0);realized=D(0)
    for seq,at,kind,body in rows:
        if not start_ms<=at<=end_ms:continue
        b=json.loads(body) if isinstance(body,str) else body
        if kind=='C4_DECISION' and b.get('model')==model:
            decisions['episodes']+=1;decisions['candidate_actions']+=len(b['decision']['candidates'])
            decisions['decision_'+b['decision']['reason']]+=1
            episodes.add(b.get('ep'))
        if kind=='C4_DECISION_TIMING' and b.get('model')==model:
            for key in ('first_choose_ms','second_choose_ms','survival_ms','first_choose_queue_ms',
                        'second_choose_queue_ms','survival_queue_ms','worker_queue_ms','total_ms'):
                if key in b:timing('decision_'+key,b[key],b['episode_id'],b['episode_id'])
        if kind=='C4_ENTRY_REJECTED' and b.get('episode_id') in episodes:
            decisions['rejected_'+b['reason']]+=1
        cid=b.get('campaign_id');c=campaigns.get(cid)
        if kind=='CAMPAIGN_INTENT' and b.get('plan',{}).get('model')==model:
            p=b['plan']
            if 'c4_action' not in p:continue
            c=campaigns[cid]=dict(plan=p,start_ms=at,cash=D(0),realized=D(0),bought=D(0),sold=D(0),
                cost=D(0),qty=D(0),mark=D(0),mark_at=0,first_fill=None,closed=False,end_ms=end_ms,
                entry_terminal=False,reason=None,pending=set(),filled_events=0,not_sent=False,exit_requests=0,
                protective_filled=False,protective_fill_after_request=False)
        if kind=='EXIT_REQUEST' and c:
            c['exit_requests']+=1;procedure['exit_requests']+=1
        if kind=='ORDER_INTENT' and c:
            o=b['order'];orders[o['cid']]=(cid,o);c['pending'].add(o['cid'])
        order_key=b.get('cid')
        if order_key in orders:
            owner,o=orders[order_key];c=campaigns[owner]
            if kind=='ORDER_SUBMITTED':
                timing(o['role']+'_submit_roundtrip_ms',b['rtt_ms'],order_key,owner)
                if b.get('exit_request_to_ack_ms') is not None:
                    timing('exit_request_to_ack_ms',b['exit_request_to_ack_ms'],order_key,owner)
            if kind=='EXECUTION_TIMING' and b.get('phase')=='cancel_and_reconcile':
                timing('cancel_and_reconcile_roundtrip_ms',b['elapsed_ms'],order_key,owner)
                timing(o['role']+'_cancel_and_reconcile_ms',b['elapsed_ms'],order_key,owner)
                procedure[o['role']+'_cancel_'+('terminal' if b.get('terminal') else 'unresolved')]+=1
            if kind=='ORDER_REJECTED' and b.get('transmitted') is False and o['role']=='entry':
                c['not_sent']=True;procedure['entry_not_sent']+=1
            if kind in ('FILL','ORDER_STATUS','ORDER_REJECTED'):
                from track_c.execution.oms import TERMINAL
                status=b.get('status','REJECTED' if kind=='ORDER_REJECTED' else None)
                if status in TERMINAL:
                    c['pending'].discard(order_key)
                    if o['role']=='entry':c['entry_terminal']=True
            if kind=='FILL':
                if b.get('exit_request_to_fill_seen_ms') is not None:
                    timing('exit_request_to_fill_seen_ms',b['exit_request_to_fill_seen_ms'],order_key,owner)
                    timing(o['role']+'_fill_seen_after_exit_request_ms',b['exit_request_to_fill_seen_ms'],order_key,owner)
                if o['role']=='protect':
                    c['protective_filled']=True
                    if c['exit_requests']:c['protective_fill_after_request']=True
                q,g,fee,pnl=(D(b[k]) for k in ('qty','gross','fee','pnl'))
                delta=(g if o['side']=='SELL' else -g)-fee
                c['cash']+=delta;cash+=delta;turnover+=g
                c['realized']+=pnl;realized+=pnl;c['filled_events']+=1
                if o['side']=='BUY':
                    c['bought']+=q;c['qty']+=q;c['cost']+=g
                    if c['first_fill'] is None:c['first_fill']=b.get('observed_ms',at)
                else:
                    basis=c['cost']*q/c['qty'] if c['qty'] else D(0)
                    c['sold']+=q;c['qty']-=q;c['cost']-=basis
                c['mark']=g/q if q else c['mark'];c['mark_at']=at
        if c and kind=='MARK':c.update(mark=D(b['bid']),mark_at=at)
        if c and kind in ('CLOSE','NO_FILL'):
            end=b['campaign'];c.update(closed=True,end_ms=at,reason=end.get('exit_reason'))
            if c['qty']!=D(end['qty']) or abs(c['cost']-D(end['cost']))>D('.000001'):
                raise ValueError('actual C4 campaign inventory accounting mismatch')
        if kind=='RESIDUAL_MARK':
            # The event has no coin prices. It cannot provide a new liquidation mark.
            pass
        equity=cash+sum((x['qty']*x['mark'] if 0<=at-x['mark_at']<=1500 else D(0) for x in campaigns.values()),D(0))
        peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
    records=[];outcomes=[]
    for cid,c in campaigns.items():
        a=c['plan']['c4_action'];known=c['closed'] and c['entry_terminal'] and not c['pending']
        cause={'one_tick_profit':'recovery','exchange_stop':'collapse','stop':'collapse',
               'premise':'collapse','time':'timeout'}.get(c['reason'],'residual')
        outcome=dict(episode_id=a['episode_id'],action=a,start_ms=c['start_ms'],end_ms=c['end_ms'],
            censored=not known,observation_gap=not known,cause=cause if known else None,
            duration_s=(c['end_ms']-c['first_fill'])/1000 if c['first_fill'] is not None else 0,
            cash_net_krw=float(c['cash']),filled_qty=float(c['bought']),residual_qty=float(c['qty']))
        probs=c['plan'].get('c4_probabilities',[]) if c['plan']['c4_mode']=='structural_sampling' else []
        # An intent rejected locally never reached the exchange. Keep it in the
        # cash ledger and admission counts, but not in exchange fill calibration.
        if not c['not_sent']:
            records.append(dict(prediction=c['plan'].get('c4_prediction',{}),probabilities=probs,outcome=outcome))
        outcomes.append(dict(campaign_id=cid,**outcome,entry_terminal=c['entry_terminal'],
                             entry_not_sent=c['not_sent'],
                             pending_orders=len(c['pending']),realized_pnl_krw=float(c['realized']),
                             remaining_cost_krw=float(c['cost']),fill_events=c['filled_events']))
    residual_value=sum((c['qty']*c['mark'] if 0<=end_ms-c['mark_at']<=1500 else D(0) for c in campaigns.values()),D(0))
    metric=score_predictions(records)
    metric.update(realization='actual_exchange_reconciled_orders',prediction_basis='public_replay_execution_hypothesis',
                  execution_procedure_calibrated=False)
    def distribution(key,values):
        ordered=sorted(values)
        return dict(n=len(values),orders_or_episodes=len(timing_owners[key]),campaigns_or_episodes=len(timing_campaigns[key]),
                    mean_ms=sum(values)/len(values),p50_ms=ordered[math.ceil(.5*len(values))-1],
                    p95_ms=ordered[math.ceil(.95*len(values))-1],max_ms=ordered[-1])
    return dict(model=model,start_ms=start_ms,end_ms=end_ms,source='C4_only_actual_ledger_events',
        decision_counts=dict(decisions),attempts=len(outcomes),filled_attempts=sum(r['filled_qty']>0 for r in outcomes),
        partial_entry_attempts=sum(0<r['filled_qty']<r['action']['qty']-1e-12 for r in outcomes),
        unknown_attempts=sum(r['censored'] for r in outcomes),outcomes=outcomes,predictions=metric,
        cash_change_krw=float(cash),realized_pnl_krw=float(realized),turnover_krw=float(turnover),
        remaining_inventory_cost_krw=float(sum((c['cost'] for c in campaigns.values()),D(0))),
        fresh_marked_inventory_krw=float(residual_value),marked_net_krw=float(cash+residual_value),
        observed_mark_drawdown_krw=float(drawdown),
        latency={k:distribution(k,v) for k,v in latencies.items()},
        exit_procedure=dict(procedure,request_campaigns=sum(c['exit_requests']>0 for c in campaigns.values()),
                            protective_filled_campaigns=sum(c['protective_filled'] for c in campaigns.values()),
                            protective_fill_after_request_campaigns=sum(c['protective_fill_after_request'] for c in campaigns.values())),
        execution_calibration=dict(status='UNVERIFIED',fitted=False,
                                   timing_basis='client_roundtrip_and_fill_observation; not_exchange_matching'),
        limitations=['roundtrip_and_fill_seen_times_are_not_exchange_matching_latency',
                    'stale_or_unavailable_inventory_marks_use_zero; not_a_profit_lower_bound',
                    'actual_selected_orders_do_not_identify_unsubmitted_candidate_fill_probabilities',
                    'structural_exit_probabilities_scored_only_in_structural_sampling_mode'],
        verdict='IMPROVEMENT_UNCONFIRMED',automatic_retraining=False)


def read(path,start_ms,end_ms,model):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        rows=db.execute('SELECT seq,t_ms,kind,body FROM events WHERE t_ms BETWEEN ? AND ? ORDER BY seq',(start_ms,end_ms))
        return summarize(rows,start_ms,end_ms,model)
    finally:db.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger',required=True);p.add_argument('--model',required=True)
    p.add_argument('--start-ms',type=int,required=True);p.add_argument('--end-ms',type=int,required=True)
    p.add_argument('--output',required=True);a=p.parse_args()
    result=read(a.ledger,a.start_ms,a.end_ms,a.model)
    with Path(a.output).open('x',encoding='utf-8') as f:f.write(encoded(result)+'\n')
    print(encoded({k:result[k] for k in ('attempts','filled_attempts','unknown_attempts','verdict')}))


if __name__=='__main__':main()
