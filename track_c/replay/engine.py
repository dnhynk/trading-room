"""Shared candidates/execution, chronological learning and paired diagnostics."""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from track_c.replay.input import coinone_rows
from track_c.replay.dataset import public_contracts, sha
from track_c.market.leaders import rows as leader_rows
from track_c.replay.stream import EventSpool
from track_c.learning.config import load, digest, sources as frozen_sources
from track_c.market.state import candidates as old_candidates
from track_c.learning.benchmark import CashModel as LegacyCash, HazardModel as LegacyHazard
from track_c.replay.ledger import frames, Book as OriginalBook, atomic_json
from track_c.learning import VERSION
from track_c.replay.execution import Attempt, ExitPair
from track_c.learning.features import candidates
from track_c.learning.model import CashModel, HazardModel
from track_c.replay.metrics import score_predictions, fill_selection


def sources():
    return frozen_sources()


def embargo(cfg):
    return (cfg['ttl_s']+cfg['hold_s'])*1000+cfg['cancel_latency_ms']+3*cfg['latency_ms']+2*cfg['decision_ms']


def stage(state,cfg):
    if not state:return 'missing_local_data'
    if not state.get('entry_fresh'):return 'stale_local_book'
    if not state['reference']['ready']:return state['reference']['reason'] or 'reference_unavailable'
    if state['reference']['dev_ticks']<cfg['entry_ticks']:return 'discount_below_floor'
    if state['reference']['m10']<=-cfg['common_drop_ticks']:return 'common_market_fall'
    if not state['risk']['ready']:return 'local_risk_unavailable'
    if (state['ask']-state['bid'])/state['tick']>2+1e-8:return 'spread_too_wide'
    return None


class Book(OriginalBook):
    def equity(self,snaps):
        # Use the same active-inventory valuation as the result/accounting identity.
        active=self.active.result(self.active.last_t)['net_krw'] if self.active else 0.
        return self.cash+self.residual_value(snaps)+active


def collect(spool,cfg,contracts,units,start,end,*,exit_model=None,cash=594574.,pairs=False):
    pending=[];labels=[];paired=[];finished_pairs=[];counts=Counter();now=start
    for now,snaps,events in frames(spool,cfg,contracts,units,end):
        if now<=start:continue
        active=pending+[p.sell for p in paired if not p.sell.done]
        for coin,event in events:
            if event['t']<=start:continue
            for a in active:
                if a.a['coin']==coin:a.event(event)
        for a in active:a.decide(now,snaps.get(a.a['coin']))
        if pairs:
            for a in pending:
                if a.done or not a.entry_done or not a.qty or a.pending_exit or a.first_fill is None:continue
                age=(now-a.first_fill)/1000
                bucket=max([v for v in [0]+cfg['hazard_seconds'][:-1] if v<=age],default=None)
                s=snaps.get(a.a['coin'])
                if bucket not in a.landmarks_seen and s and s['reference']['ready'] and a.fresh(now):
                    a.landmarks_seen.add(bucket)
                    p=ExitPair(a,now,s)
                    if p.x is not None:paired.append(p)
        labels.extend(a.result() for a in pending if a.done)
        pending=[a for a in pending if not a.done]
        finished_pairs.extend(p.result(now) for p in paired if p.done)
        paired=[p for p in paired if not p.done]
        if not start<now<end-embargo(cfg):continue
        for coin,s in snaps.items():
            counts['frames']+=1
            if not s or not s['new_episode']:continue
            counts['episodes']+=1
            reason=stage(s,cfg)
            if reason:counts['rejected_'+reason]+=1
            actions=candidates(s,cfg,cash,cash*cfg['risk_fraction'],research=True)
            allowed={a['id'] for a in old_candidates(s,cfg,cash,cash*cfg['risk_fraction'])}
            counts['research_actions']+=len(actions)
            counts['admissible_actions']+=len(allowed)
            if allowed:counts['admissible_episodes']+=1
            elif not reason:counts['rejected_price_size_or_capacity']+=1
            pending.extend(Attempt(a,cfg,s,exit_model) for a in actions)
    labels.extend(a.result(now) for a in pending)
    finished_pairs.extend(p.result(now) for p in paired)
    counts.update(labels=len(labels),filled_labels=sum(r['filled_qty']>0 for r in labels),
                  filled_episodes=len({r['episode_id'] for r in labels if r['filled_qty']>0}),
                  censored_labels=sum(r['censored'] for r in labels),pairs=len(finished_pairs))
    return labels,finished_pairs,dict(counts)


def prediction(model,action,now,n):
    if model is None:return dict(ready=True,reason='fixed_structural',expected_net_krw=None,p_fill=None)
    p=model.predict(action,now,n)
    if isinstance(model,LegacyCash):
        p=dict(p,expected_net_krw=p.get('mean')*action['notional']/10000 if p.get('mean') is not None else None)
    return p


def compare(spool,cfg,contracts,units,start,end,models,*,cash=594574.,stress=True):
    variants={'base':({},1.)}
    if stress:variants.update(latency_1000=({'latency_ms':1000,'cancel_latency_ms':1500},1.),
        fee_1bp=({'fee_bp':1.},1.),depth_25pct=({'depth_haircut':.25},1.),queue_double=({},2.))
    models={**models,'structural':(None,None)}
    books={(v,m):Book({**cfg,**change},cash) for v,(change,_) in variants.items() for m in models}
    probes={key:[] for key in books};records={key:[] for key in books};counts=Counter();now=start;last={}
    probability_cache={}
    # Exactly the same candidate window in every execution scenario.
    guard=max(embargo({**cfg,**change}) for change,_ in variants.values())
    for book in books.values():book.started_ms=start
    for now,snaps,events in frames(spool,cfg,contracts,units,end):
        last=snaps
        if now<=start:continue
        for key,book in books.items():
            group=probes[key]+([book.active] if book.active else [])
            for coin,event in events:
                if event['t']<=start:continue
                for a in group:
                    if a.a['coin']==coin:a.event(event)
            for a in group:a.decide(now,snaps.get(a.a['coin']))
            for a in probes[key]:
                if a.done:records[key].append(dict(prediction=a.prediction,probabilities=a.probabilities,outcome=a.result()))
            probes[key]=[a for a in probes[key] if not a.done]
            book.settle(now,snaps)
        if not start<now<end-guard:continue
        for coin,s in snaps.items():
            counts['frames']+=1
            if s and s.get('entry_fresh'):counts['fresh_local_frames']+=1
            if s and s['reference']['ready']:counts['reference_ready_frames']+=1
            if not s or not s['new_episode']:continue
            counts['episodes']+=1
            why=stage(s,cfg)
            if why:counts['rejected_'+why]+=1
            actions=candidates(s,cfg,cash,cash*cfg['risk_fraction'])
            counts['candidates']+=len(actions)
            if actions:counts['candidate_episodes']+=1
            elif not why:counts['rejected_price_size_or_capacity']+=1
            predictions={name:[prediction(model,a,now,len(actions)) for a in actions] for name,(_,model) in models.items()}
            for name,(hazard,_) in models.items():
                if hazard is None:continue
                for a in actions:
                    probs=hazard.survival(a)
                    if isinstance(hazard,LegacyHazard) and not hazard.doc['groups'].get(a['key']+'|'+a['id']):probs=[]
                    probability_cache[(name,a['episode_id'],a['id'])]=probs
            for key,book in books.items():
                variant,name=key;hazard,_=models[name];change,queue=variants[variant]
                values=predictions[name]
                for a,p in zip(actions,values):
                    probe=Attempt(a,{**cfg,**change},s,hazard,queue_multiplier=queue)
                    probe.prediction=p
                    # Probability targets always use the structural policy's outcomes below.
                    probe.probabilities=[]
                    probes[key].append(probe)
                if book.active:
                    book.decisions['occupied']+=bool(actions)
                    continue
                available,risk=book.capacity(snaps)
                feasible=[]
                for a,p in zip(actions,values):
                    if a['notional']>available*cfg['cash_fraction']+1e-8 or a['nominal_loss']>risk+1e-8:
                        book.decisions['portfolio_capacity']+=1;continue
                    book.decisions[p['reason']]+=1
                    if name=='structural':
                        if a['offset']==0 and a['size']=='minimum':feasible.append((0.,a,p))
                    elif p.get('ready') and p.get('score_krw',0)>0:feasible.append((p['score_krw'],a,p))
                if feasible:
                    _,a,p=max(feasible,key=lambda row:row[0])
                    book.active=Attempt(a,{**cfg,**change},s,hazard,queue_multiplier=queue)
                    book.active.prediction=p
    for key in probes:
        records[key].extend(dict(prediction=a.prediction,probabilities=[],outcome=a.result(now)) for a in probes[key])
    # Pair the frozen structural probability with the structural-policy outcome.
    forecasts={}
    structural=records[('base','structural')]
    for name,(hazard,_) in models.items():
        if hazard is None:continue
        diag=[]
        for item in structural:
            a=item['outcome']['action']
            probabilities=probability_cache[(name,a['episode_id'],a['id'])]
            diag.append(dict(prediction={},probabilities=probabilities,outcome=item['outcome']))
        forecasts[name]=score_predictions(diag)['structural_exit_probabilities']
    report={}
    for (variant,name),book in books.items():
        ledger=book.report(now,last)
        ledger.pop('continuation_state',None)
        rows=records[(variant,name)]
        report.setdefault(variant,{})[name]=dict(ledger=ledger,predictions=score_predictions(rows),records=rows)
    selection={v:{name:fill_selection([r['outcome'] for r in records[('base',name)]],
                                     [r['outcome'] for r in records[(v,name)]]) for name in models}
               for v in variants if v!='base'}
    return dict(start_ms=start,end_ms=now,coverage=dict(counts),scenarios=report,
                structural_probability_diagnostics=forecasts,stress_fill_selection=selection,
                comparison='legacy_estimator_refitted_vs_refined; shared_candidates_and_execution',
                verdict='IMPROVEMENT_UNCONFIRMED',exchange_fills=0)


def build(spec,output,cfg=None):
    cfg=load() if cfg is None else cfg
    required={'coinone','leaders','contracts','start_ms','exit_end_ms','entry_end_ms','test_start_ms','end_ms'}
    if set(spec)!=required:raise ValueError('explicit exit/entry/test windows required')
    times=[spec[k] for k in ('start_ms','exit_end_ms','entry_end_ms','test_start_ms','end_ms')]
    if any(type(v)is not int for v in times) or not times[0]<times[1]<times[2]<=times[3]<times[4]:
        raise ValueError('invalid time boundaries')
    paths=[Path(p).resolve() for p in spec['coinone']+spec['leaders']+[spec['contracts']]]
    if len(paths)!=len(set(paths)):raise ValueError('duplicate input file')
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    identity=dict(data={str(p):sha(p) for p in paths},source=sources(),config=digest(cfg),spec=digest(spec))
    atomic_json(out/'frozen-before-learning.json',identity)
    contracts,units=public_contracts(spec['contracts']);quality=Counter()
    with EventSpool(coinone_rows(spec['coinone'],quality),(r for p in spec['leaders'] for r in leader_rows(p)),
                    cfg['coins'],cfg['book_max_age_ms'],quality) as spool:
        if spool.start>times[0] or spool.end<times[-1]:raise ValueError('insufficient input coverage')
        train,pairs,train_counts=collect(spool,cfg,contracts,units,times[0],times[1],pairs=True)
        exits=dict(legacy=LegacyHazard.fit(train,times[1],cfg),refined=HazardModel.fit(train,pairs,times[1],cfg))
        models={};entry_counts={}
        for name,hazard in exits.items():
            labels,_,counts=collect(spool,cfg,contracts,units,times[1],times[2],exit_model=hazard)
            cls=LegacyCash if name=='legacy' else CashModel
            model=cls.fit(labels,times[2],cfg,hazard.doc['digest'])
            models[name]=(hazard,model);entry_counts[name]=counts
            atomic_json(out/(name+'-entry-labels.json'),labels)
        doc=dict(version=VERSION,mode='offline_observation',config=cfg,identity=identity,spec=spec,
                 models={name:dict(exit=h.doc,cash=c.doc) for name,(h,c) in models.items()},
                 probability_calibration_fitted=False,orders_enabled=False)
        doc['digest']=digest(doc)
        atomic_json(out/'model.json',doc)
        atomic_json(out/'exit-labels.json',train);atomic_json(out/'exit-pairs.json',pairs)
        # This immutable receipt precedes any test outcome calculation.
        atomic_json(out/'test-opening.json',dict(model=doc['digest'],source=sources(),start_ms=times[3],end_ms=times[4]))
        result=compare(spool,cfg,contracts,units,times[3],times[4],models)
    if identity['source']!=sources() or identity['data']!={str(p):sha(p) for p in paths}:
        raise ValueError('source or input changed during evaluation')
    result.update(version=VERSION,model=doc['digest'],identity=identity,spec=spec,
                  exit_training=train_counts,entry_learning=entry_counts,quality=dict(quality))
    atomic_json(out/'report.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--spec',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();r=build(json.loads(Path(a.spec).read_text()),a.output)
    print(json.dumps(dict(verdict=r['verdict'],exit_training=r['exit_training'],entry_learning=r['entry_learning'],
                         coverage=r['coverage'],ledgers={k:v['ledger']['attempts'] for k,v in r['scenarios']['base'].items()})))


if __name__=='__main__':main()
