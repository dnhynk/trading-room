"""Read-only actual-fill, latency, episode and quantity-specific liquidation audit.

Public depth is an as-of displayed-book counterfactual, never an exchange fill.
No thresholds, orders, evaluation clocks or original ledger rows are changed.
"""
import argparse
from collections import Counter, defaultdict, deque
from contextlib import closing
from decimal import Decimal as D
import gzip
import json
from pathlib import Path
import sqlite3
import statistics
import time

from .microstructure import Micro, liquidate


def distribution(values):
    values=sorted(values)
    if not values: return dict(n=0)
    def percentile(q):
        at=(len(values)-1)*q; left=int(at); right=min(left+1,len(values)-1)
        return values[left]+(values[right]-values[left])*(at-left)
    return dict(n=len(values),mean=statistics.mean(values),median=percentile(.5),p95=percentile(.95),max=values[-1])


def rows(path,quality):
    """Keep complete lines from a growing gzip and explicitly label its missing tail."""
    try:
        with gzip.open(path,'rt',encoding='utf-8') as stream:
            for line in stream:
                if not line.endswith('\n'):
                    quality['incomplete_lines']+=1; continue
                try: yield json.loads(line)
                except ValueError: quality['malformed_lines']+=1
    except (EOFError,OSError,UnicodeError):
        quality['incomplete_or_unreadable_files']+=1


def books(paths,coins,quality):
    micros={c:Micro(c,legacy_features=False) for c in coins}
    last=0
    for path in sorted(paths):
        for row in rows(path,quality):
            if not isinstance(row,dict): continue
            msg=row.get('message',row.get('msg',{})); at=row.get('received_ms',row.get('recv_ms',row.get('t_ms')))
            if isinstance(msg,str):
                try: msg=json.loads(msg)
                except ValueError: quality['malformed_messages']+=1; continue
            if not isinstance(msg,dict): continue
            if msg.get('response_type')!='DATA' or msg.get('channel')!='ORDERBOOK': continue
            data=msg.get('data',{}); coin=data.get('target_currency')
            if coin not in micros or at is None: continue
            if at<last:
                quality['backwards_public_rows']+=1; continue
            if at-last>120000:
                for micro in micros.values(): micro.reset()
            last=at
            event=micros[coin].feed('ORDERBOOK',data,at)
            if event: yield dict(t_ms=at,coin=coin,book_ms=at,exchange_ms=event['exchange_t'],bids=event['bids'])


def asof_queries(stream,queries,*,age_field='t_ms',max_age=1500):
    """O(number of queries + active coins) memory, never reads a later row as known."""
    iterator=iter(stream); upcoming=next(iterator,None); current={}; answers={}
    for at,coin,key in sorted(queries,key=lambda x:(x[0],x[1],str(x[2]))):
        while upcoming is not None and upcoming['t_ms']<=at:
            current[upcoming['coin']]=upcoming
            upcoming=next(iterator,None)
        row=current.get(coin)
        fields=(age_field,) if isinstance(age_field,str) else age_field
        fresh=bool(row) and all(row.get(field) is not None and 0<=at-row[field]<=max_age for field in fields)
        answers[key]=row if fresh else None
    return answers


def decompose(entry,exit,first,last):
    """Exact price identity; basis adaptation is not called external-market PnL."""
    a=(first or {}).get('fair') or {}; b=(last or {}).get('fair') or {}
    if a.get('fair') is None or b.get('fair') is None: return None
    if any(f.get('evaluated_ms') is None or not 0<=frame['t_ms']-f['evaluated_ms']<=1500 for frame,f in ((first,a),(last,b))): return None
    change=b['fair']-a['fair']
    result=dict(price_change=exit-entry,fair_change=change,relative_change=exit-entry-change)
    left=a.get('reference_components') or {}; right=b.get('reference_components') or {}
    stable=bool(left) and left.keys()==right.keys() and all(abs(left[v]['weight']-right[v]['weight'])<1e-12 for v in left)
    if stable:
        result['external_change']=sum(left[v]['weight']*(left[v]['basis_ratio']+right[v]['basis_ratio'])/2*(right[v]['price']-left[v]['price']) for v in left)
        result['basis_change']=sum(left[v]['weight']*(left[v]['price']+right[v]['price'])/2*(right[v]['basis_ratio']-left[v]['basis_ratio']) for v in left)
        result['identity_error']=change-result['external_change']-result['basis_change']
    else:
        result['composition_changed']=True
    return result


def audit(directory,*,start_ms,end_ms=None):
    directory=Path(directory); end_ms=int(time.time()*1000) if end_ms is None else end_ms
    quality=Counter(); campaigns={}; orders={}; fills=[]; timings=defaultdict(list)
    path=(directory/'ledger.sqlite').resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        state=json.loads(db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
        ledger_end=db.execute('SELECT MAX(t_ms) FROM events').fetchone()[0] or 0
        query="SELECT seq,t_ms,kind,body FROM events WHERE t_ms>=? AND t_ms<=? AND kind IN ('CAMPAIGN_INTENT','ORDER_INTENT','FILL','CLOSE','NO_FILL','ORDER_SUBMITTED','EXECUTION_TIMING') ORDER BY seq"
        for seq,at,kind,body in db.execute(query,(start_ms,end_ms)):
            body=json.loads(body); cid=body.get('campaign_id')
            if kind=='CAMPAIGN_INTENT':
                campaigns[cid]=dict(id=cid,coin=body['coin'],start_ms=at,plan=body['plan'],residual=body.get('residual'),fills=[],closed=False)
            elif kind=='ORDER_INTENT':
                orders[body['order']['cid']]=body['order']
            elif kind=='FILL':
                order=orders.get(body['cid']); campaign=campaigns.get(cid)
                if not order or not campaign:
                    quality['fills_from_prior_or_unknown_campaign']+=1; continue
                row=dict(seq=seq,t_ms=at,campaign_id=cid,coin=campaign['coin'],side=order['side'],role=order['role'],
                         qty=body['qty'],gross=body['gross'],fee=body['fee'],pnl=body['pnl'])
                fills.append(row); campaign['fills'].append(row)
            elif kind in ('CLOSE','NO_FILL') and cid in campaigns:
                campaigns[cid].update(closed=True,outcome=kind,campaign=body['campaign'],end_ms=at)
            for name in ('rtt_ms','exit_request_to_ack_ms','exit_request_to_fill_seen_ms'):
                if body.get(name) is not None: timings[name].append(body[name])
            if kind=='EXECUTION_TIMING': timings[body['phase']+'_ms'].append(body['elapsed_ms'])
    queries=[]; evaluations={}; owned=[]
    if ledger_end<=end_ms:
        for coin,c in {**state.get('residuals',{}),**state.get('campaigns',{})}.items():
            if D(c['qty'])>0:
                owned.append(dict(coin=coin,qty=c['qty'],cost=c['cost'],mark=c.get('mark'),mark_at=c.get('mark_at'),minimum=c.get('minimum')))
                queries.append((end_ms,coin,('inventory',coin)))
    for c in campaigns.values():
        buys=[f for f in c['fills'] if f['side']=='BUY' and D(f['qty'])>0]
        points=buys if buys else [dict(t_ms=c['start_ms'],qty=c['plan']['qty'],gross=str(D(c['plan']['qty'])*D(c['plan']['entry'])),seq='attempt-'+str(c['id']))]
        for point in points:
            for horizon in (0,250,1000,5000,30000):
                target=point['t_ms']+horizon; key=(point['seq'],horizon)
                evaluations[key]=dict(coin=c['coin'],campaign_id=c['id'],cohort='filled' if buys else 'unfilled' if c['closed'] else 'pending',horizon_ms=horizon,
                                      qty=float(point['qty']),price=float(D(point['gross'])/D(point['qty'])))
                if target<=end_ms: queries.append((target,c['coin'],key))
                else: quality['right_censored_markouts']+=1
    public=asof_queries(books((directory/'public').glob('*.jsonl.gz'),{c['coin'] for c in campaigns.values()}|{c['coin'] for c in owned},quality),queries,age_field=('book_ms','exchange_ms'))
    for item in owned:
        book=public.get(('inventory',item['coin'])); quantity=float(item['qty'])
        px=liquidate(book['bids'],quantity) if book else None
        item['displayed_value_krw']=px*quantity if px is not None else None
        item['unrealized_at_displayed_bid_krw']=px*quantity-float(item['cost']) if px is not None else None
        item['minimum_confirmed']=item['minimum'] is not None
        item['sale_notional_sufficient']=px is not None and item['minimum'] is not None and px*quantity>=float(item['minimum'])
    markouts=defaultdict(list); missing=Counter()
    for key,case in evaluations.items():
        book=public.get(key); px=liquidate(book['bids'],case['qty']) if book else None
        label=case['cohort']+'_'+str(case['horizon_ms'])+'ms'
        if px is None: missing[label]+=1
        else: markouts[label].append((px/case['price']-1)*10000)
    frame_queries=[(f['t_ms'],f['coin'],f['seq']) for f in fills]
    counts=Counter(); episodes=set(); last_frame_time=0
    def frames():
        nonlocal last_frame_time
        for path in sorted((directory/'observations').glob('*.jsonl.gz')):
            for row in rows(path,quality):
                if not isinstance(row,dict) or row.get('t_ms',0)>end_ms: continue
                if start_ms<=row.get('t_ms',0):
                    if row.get('kind')=='FRAME':
                        counts['frames']+=1; counts['reason:'+str((row.get('selection') or {}).get('reason'))]+=1
                    if row.get('kind')=='EPISODE_START': episodes.add(row['episode_id'])
                if row.get('kind')=='FRAME':
                    if row['t_ms']<last_frame_time: quality['backwards_observation_frames']+=1; continue
                    last_frame_time=row['t_ms']
                    row['reference_ms']=(row.get('fair') or {}).get('evaluated_ms')
                    yield row
    # A sentinel consumes the remaining observations, even with no actual fills.
    frame_queries.append((end_ms,'__end__','__end__'))
    observed=asof_queries(frames(),frame_queries,age_field=('t_ms','reference_ms'))
    attribution=defaultdict(float); matched=0
    for c in campaigns.values():
        if c.get('residual'):
            quality['carried_inventory_attribution_excluded']+=1; continue
        inventory=deque()
        for fill in c['fills']:
            q=float(fill['qty'])
            if q<=0: continue
            px=float(fill['gross'])/q
            if fill['side']=='BUY': inventory.append([q,px,observed.get(fill['seq'])]); continue
            while q>1e-12 and inventory:
                part,entry,first=inventory[0]; matched_qty=min(q,part)
                result=decompose(entry,px,first,observed.get(fill['seq']))
                if result:
                    matched+=1
                    for k in ('price_change','fair_change','relative_change','external_change','basis_change'):
                        if k in result: attribution[k+'_krw']+=matched_qty*result[k]
                    if result.get('composition_changed'): quality['attribution_composition_changes']+=1
                else: quality['missing_reference_at_fill']+=1
                q-=matched_qty; inventory[0][0]-=matched_qty
                if inventory[0][0]<=1e-12: inventory.popleft()
    turnover=sum(float(f['gross']) for f in fills)
    realized=sum(D(f['pnl']) for f in fills)
    closed=[c for c in campaigns.values() if c['closed'] and c.get('outcome')=='CLOSE']
    completed=[c for c in closed if D(c['campaign']['qty'])==0]
    actual_buys=sum(float(f['gross']) for f in fills if f['side']=='BUY')
    costs=sum(float(f['fee']) for f in fills)
    return dict(schema=1,start_ms=start_ms,end_ms=end_ms,ledger_end_ms=ledger_end,
                attempts=len(campaigns),filled_attempts=sum(any(f['side']=='BUY' and D(f['qty'])>0 for f in c['fills']) for c in campaigns.values()),
                no_fill=sum(c.get('outcome')=='NO_FILL' for c in campaigns.values()),closed=len(completed),
                closed_with_residual=len(closed)-len(completed),pending_campaigns=sum(not c['closed'] for c in campaigns.values()),
                wins=sum(D(c['campaign']['net'])>0 for c in completed),realized_krw=str(realized),
                actual_buys_krw=actual_buys,turnover_krw=turnover,actual_fees_krw=costs,
                realized_per_hour_krw=float(realized)/max((end_ms-start_ms)/3600000,1e-9),
                extra_cost_stress_realized_krw={str(bp):float(realized)-turnover*bp/10000 for bp in (.5,1,2)},
                exit_reasons=dict(Counter(c['campaign']['exit_reason'] for c in completed)),latency_ms={k:distribution(v) for k,v in timings.items()},
                displayed_liquidation_markout_bp={k:distribution(v) for k,v in markouts.items()},missing_depth_or_coverage=dict(missing),
                reference_attribution=dict(matched_lots=matched,**attribution),episode_count=len(episodes),observations=dict(counts),
                current_owned_inventory=owned,quality=dict(quality),
                limitations=['Realized PnL alone excludes unsold inventory; current_owned_inventory is retained and not declared a completed trade.',
                    'Public displayed depth after fill-observation time is not an actual sale or an own-impact model; 1500ms as-of freshness enforced.',
                    'Unfilled markouts are hypothetical same-order-size outcomes, not executable wins; open cases are right censored.',
                    'Reference attribution uses past 1Hz frames, has up to1500ms measurement lag, and excludes carried inventory.',
                    'Episodes are observational sell-pressure labels; they do not prove a temporary non-informational shock.',
                    'Extra cost stress is additional to actual fills/fees; observed spread/slippage are not subtracted twice.',
                    'Campaigns and episodes share market paths and are not independent trials.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True); parser.add_argument('--start-ms',type=int,required=True)
    parser.add_argument('--end-ms',type=int); parser.add_argument('--output',required=True)
    args=parser.parse_args()
    report=audit(args.data_dir,start_ms=args.start_ms,end_ms=args.end_ms)
    Path(args.output).write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('attempts','filled_attempts','closed','realized_krw','actual_buys_krw','episode_count','quality')}))


if __name__=='__main__': main()
