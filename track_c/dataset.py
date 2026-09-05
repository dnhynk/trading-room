"""Frozen Coinone source -> shared causal states -> executable action outcomes."""
from bisect import bisect_right
from collections import Counter
from decimal import Decimal as D, ROUND_CEILING
import gzip
import hashlib
import json
from pathlib import Path

from .microstructure import Micro
from .outcomes import Path as MarketPath
from .universe import asset_reason


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def public_contracts(path):
    records=json.loads(Path(path).read_text(encoding='utf-8'))
    contracts={}; units={}
    if isinstance(records,dict) and 'captures' in records:
        for r in sorted(records['captures'],key=lambda r:r['available_ms']):
            coin=r['coin']; at=r['available_ms']; contract=dict(r['contract'],available_ms=at)
            ladder=dict(available_ms=at,rows=r['units'])
            if coin not in contracts: contracts[coin]=dict(contract,history=[]); units[coin]=dict(ladder,history=[])
            contracts[coin]['history'].append(contract); units[coin]['history'].append(ladder)
        return contracts,units
    for row in records:
        response=row['response']; available=row['available_ms']
        if 'markets' in response:
            contracts.update({m['target_currency']:dict(m,available_ms=available) for m in response['markets']})
        if 'range_price_units' in response:
            coin=row['url'].split('?')[0].rsplit('/',1)[-1]
            units[coin]=dict(available_ms=available,rows=response['range_price_units'])
    return contracts,units


def asof(mapping,coin,t):
    row=mapping.get(coin)
    if not row: return None
    history=row.get('history',[row]); candidates=[r for r in history if r['available_ms']<=t]
    return candidates[-1] if candidates else None


def build(spec, output):
    folder=Path(output); folder.mkdir(parents=True,exist_ok=True)
    contracts,units=public_contracts(spec['contracts'])
    inputs=[Path(p).resolve() for p in spec['sources']]
    if len(set(inputs))!=len(inputs): raise ValueError('duplicate source file')
    manifest=[dict(path=str(p),sha256=sha(p),bytes=p.stat().st_size) for p in inputs]
    streams={}; event_rows={}; states=[]; quality=Counter(); last=-1; next_sample=None
    def sample(t):
        if t%spec['anchor_ms']: return
        for coin,micro in streams.items():
            contract=asof(contracts,coin,t)
            if not contract or contract['available_ms']>t or not micro.fresh(t): continue
            ladder=asof(units,coin,t)
            if ladder and ladder['available_ms']<=t:
                options=[float(r['price_unit']) for r in sorted(ladder['rows'],key=lambda r:float(r['range_min'])) if float(r['range_min'])<=micro.bids[0][0]]
                tick=options[-1] if options else 0
            else: tick=float(contract.get('price_unit') or 0)
            snap=micro.snapshot(t,tick)
            if snap and snap['history_s']>=120:
                snap['minimum']=float(contract['min_order_amount']); snap['qty_step']=contract['qty_unit']
                states.append(snap)
    for path,record in zip(inputs,manifest):
        op=gzip.open if path.suffix=='.gz' else open
        with op(path,'rt',encoding='utf-8') as stream:
            for line in stream:
                try:
                    row=json.loads(line); recv=int(row.get('received_ms',row.get('recv_ms')))
                    msg=row['message']; msg=json.loads(msg) if isinstance(msg,str) else msg
                    if msg.get('response_type')!='DATA': continue
                    if recv<last: quality['global_receive_regression']+=1; continue
                    if last>=0 and recv-last>120000:
                        streams={}; next_sample=None; quality['session_gap']+=1
                    if next_sample is None: next_sample=(recv//1000+1)*1000
                    while next_sample<recv:
                        sample(next_sample); next_sample+=1000
                    last=recv
                    data=msg.get('data') or {}; coin=data.get('target_currency','')
                    if asset_reason(coin): quality['excluded_asset_events']+=1; continue
                    if coin not in contracts: quality['missing_contract']+=1; continue
                    micro=streams.setdefault(coin,Micro(coin,stale_ms=spec['stale_ms']))
                    event=micro.feed(msg.get('channel'),data,recv)
                    if event: event_rows.setdefault(coin,[]).append(event)
                    else: quality['rejected_event']+=1
                except (KeyError,TypeError,ValueError): quality['malformed']+=1
        if sha(path)!=record['sha256']: raise ValueError('source changed during replay')
    paths={coin:MarketPath(rows,stale_ms=spec['stale_ms']) for coin,rows in event_rows.items()}
    if len(states)>spec.get('max_states',len(states)):
        # Outcome-independent deterministic sampling avoids minute-boundary
        # aliasing while bounding memory on the shared production host.
        limit=spec['max_states']; quality['resource_sampled_out']=len(states)-limit
        states=sorted(sorted(states,key=lambda s:hashlib.sha256(f"{s['coin']}:{s['t']}".encode()).digest())[:limit],key=lambda s:(s['t'],s['coin']))
    labels=[]; continuations=[]
    for snap in states:
        path=paths[snap['coin']]; step=D(snap['qty_step'])
        for factor in spec['size_multiples']:
            quantity=float((D(str(snap['minimum']*factor/snap['bid']))/step).to_integral_value(rounding=ROUND_CEILING)*step)
            for horizon in spec['horizons']:
                continuation=path.continuation(snap,quantity,horizon,latency_ms=spec['latency_ms'],taker=spec['taker'])
                if continuation: continuations.append(continuation)
                for ttl in spec['ttls']:
                    for target in spec.get('target_ticks',[0]):
                        result=path.label(snap,quantity,ttl,horizon,latency_ms=spec['latency_ms'],maker=spec['maker'],taker=spec['taker'],target_ticks=target)
                        if result['censored']: quality['censored_'+result['censored']]+=1
                        else: labels.append(result)
    report=dict(spec=spec,sources=manifest,contract_source=dict(path=spec['contracts'],sha256=sha(spec['contracts'])),
                quality=dict(quality),states=len(states),labels=len(labels),filled=sum(r['filled'] for r in labels),
                partials=sum(0<r['fill_fraction']<1-1e-8 for r in labels),dust=sum(r.get('dust',False) for r in labels),
                continuation_labels=len(continuations),coins=sorted(paths),start=min((r['t'] for r in states),default=None),end=last,
                label_kind='public_queue_counterfactual',exchange_fills=0)
    for name,rows in (('states',states),('labels',labels),('continuations',continuations)):
        with gzip.open(folder/(name+'.jsonl.gz'),'wt',encoding='utf-8') as stream:
            for row in rows: stream.write(json.dumps(row,separators=(',',':'),allow_nan=False)+'\n')
    with gzip.open(folder/'paths.json.gz','wt',encoding='utf-8') as stream: json.dump(event_rows,stream,separators=(',',':'))
    (folder/'dataset.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


def rows(path):
    with gzip.open(path,'rt',encoding='utf-8') as stream:
        return [json.loads(line) for line in stream]
