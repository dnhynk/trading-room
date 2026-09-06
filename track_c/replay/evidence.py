"""Immutable refinement artifacts and future evaluation, never live adoption."""
import argparse
import json
import math
from pathlib import Path
import time

from track_c.learning.config import digest, validate
from track_c.learning.benchmark import CashModel as LegacyCash, HazardModel as LegacyHazard
from track_c.replay.ledger import atomic_json
from track_c.learning import VERSION
from track_c.learning.model import CashModel, HazardModel
from track_c.replay.engine import sources


def read_model(path):
    doc=json.loads(Path(path).read_text())
    if doc.get('version')!=VERSION or doc.get('digest')!=digest({k:v for k,v in doc.items() if k!='digest'}):
        raise ValueError('refinement model identity mismatch')
    cfg=validate(doc['config'])
    if doc['identity']['source']!=sources() or doc['identity']['config']!=digest(cfg):
        raise ValueError('refinement source/config changed')
    models={}
    for name,hcls,ccls in [('legacy',LegacyHazard,LegacyCash),('refined',HazardModel,CashModel)]:
        h=hcls(doc['models'][name]['exit'],cfg);c=ccls(doc['models'][name]['cash'],cfg)
        if c.doc['exit_model']!=h.doc['digest'] or h.doc['trained_until']>=c.doc['trained_until']:
            raise ValueError('refinement training lineage mismatch')
        models[name]=(h,c)
    return doc,cfg,models


def register(model_path,start_ms,*,now_ms=None):
    now=int(time.time()*1000) if now_ms is None else now_ms
    doc,_,models=read_model(model_path)
    if start_ms<=max(now,doc['spec']['end_ms'],models['refined'][1].doc['trained_until']):
        raise ValueError('future window must follow registration and all consumed development data')
    registration=dict(version=VERSION,model=doc['digest'],source=doc['identity']['source'],
        config=doc['identity']['config'],registered_ms=now,start_ms=start_ms,end_ms=start_ms+10*86400000,
        comparisons=['legacy_estimator_same_execution','refined','one_structural_baseline'],
        stresses=['latency_1000','fee_1bp','depth_25pct','queue_double'],
        probability_target='structural_exit_conditional_on_fill',cash_target='net_cash_per_attempt',
        probability_calibration_fitted=False,orders_enabled=False,automatic_retraining=False,
        verdict='IMPROVEMENT_UNCONFIRMED',minimum_complete_24h_blocks=3,minimum_filled_episodes=10)
    registration['digest']=digest(registration)
    return registration


def assess(protocol,report,*,now_ms=None):
    now=int(time.time()*1000) if now_ms is None else now_ms
    if protocol.get('digest')!=digest({k:v for k,v in protocol.items() if k!='digest'}):
        raise ValueError('registration changed')
    reasons=[]
    if now<protocol['end_ms']:reasons.append('future_window_incomplete')
    if report.get('model')!=protocol['model']:reasons.append('model_mismatch')
    if report.get('identity',{}).get('source')!=protocol['source']:reasons.append('source_mismatch')
    if report.get('identity',{}).get('config')!=protocol['config']:reasons.append('config_mismatch')
    if (report.get('start_ms'),report.get('end_ms'))!=(protocol['start_ms'],protocol['end_ms']):reasons.append('window_mismatch')
    for name in ('legacy','refined','structural'):
        ledger=report.get('scenarios',{}).get('base',{}).get(name,{}).get('ledger',{})
        outcomes=ledger.get('outcomes',[])
        known=[r for r in outcomes if r['filled_qty']>0 and not r['censored']]
        episodes={r['episode_id'] for r in known}
        blocks={int((r['start_ms']-protocol['start_ms'])//86400000) for r in known}
        if len(episodes)<protocol['minimum_filled_episodes'] or len(blocks)<protocol['minimum_complete_24h_blocks']:
            reasons.append(name+'_insufficient_filled_episodes_or_blocks')
        error=ledger.get('accounting_error_krw',float('inf'))
        if any(r['censored'] for r in outcomes) or not isinstance(error,(int,float)) or not math.isfinite(error) or abs(error)>1e-6:
            reasons.append(name+'_unresolved_accounting_or_observation')
    if not report.get('exchange_fills'):reasons.append('live_fill_and_latency_calibration_unverified')
    # Adequate software evidence is still a human comparison, never a promotion.
    return dict(verdict='IMPROVEMENT_UNCONFIRMED' if reasons else 'REVIEW_REQUIRED',reasons=reasons,
                orders_enabled=False,automatic_retraining=False)


def evaluate(model_path,protocol,spec,output):
    """Frozen forward scoring only. This path cannot call fit or update a model."""
    from collections import Counter
    from track_c.replay.input import coinone_rows
    from track_c.replay.dataset import public_contracts, sha
    from track_c.market.leaders import rows as leader_rows
    from track_c.replay.stream import EventSpool
    from track_c.replay.engine import compare
    doc,cfg,models=read_model(model_path)
    if protocol.get('digest')!=digest({k:v for k,v in protocol.items() if k!='digest'}):raise ValueError('registration changed')
    if protocol['model']!=doc['digest'] or protocol['source']!=sources():raise ValueError('model differs from registration')
    if set(spec)!={'coinone','leaders','contracts','start_ms','end_ms'}:raise ValueError('explicit forward inputs required')
    if spec['start_ms']!=protocol['start_ms'] or not spec['start_ms']<spec['end_ms']<=protocol['end_ms']:
        raise ValueError('forward window differs from registration')
    paths=[Path(p).resolve() for p in spec['coinone']+spec['leaders']+[spec['contracts']]]
    if len(set(paths))!=len(paths):raise ValueError('duplicate forward input')
    identity=dict(source=sources(),config=digest(cfg),data={str(p):sha(p) for p in paths})
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    atomic_json(out/'opened.json',dict(model=doc['digest'],registration=protocol['digest'],spec=spec,identity=identity))
    contracts,units=public_contracts(spec['contracts']);quality=Counter()
    with EventSpool(coinone_rows(spec['coinone'],quality),(r for p in spec['leaders'] for r in leader_rows(p)),
                    cfg['coins'],cfg['book_max_age_ms'],quality) as spool:
        if spool.start>spec['start_ms'] or spool.end<spec['end_ms']:raise ValueError('incomplete forward coverage')
        result=compare(spool,cfg,contracts,units,spec['start_ms'],spec['end_ms'],models)
    if identity['source']!=sources() or identity['data']!={str(p):sha(p) for p in paths}:raise ValueError('forward inputs changed')
    result.update(model=doc['digest'],identity=identity,registration=protocol['digest'],quality=dict(quality))
    result['assessment']=assess(protocol,result)
    atomic_json(out/'report.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    r=sub.add_parser('register');r.add_argument('--model',required=True)
    r.add_argument('--start-ms',required=True,type=int);r.add_argument('--output',required=True)
    e=sub.add_parser('evaluate');e.add_argument('--model',required=True);e.add_argument('--registration',required=True)
    e.add_argument('--spec',required=True);e.add_argument('--output',required=True)
    a=p.parse_args()
    if a.command=='register':
        path=Path(a.output)
        if path.exists():raise FileExistsError('registration output already exists')
        result=register(a.model,a.start_ms);atomic_json(path,result)
        print(json.dumps(dict(model=result['model'],start_ms=result['start_ms'],end_ms=result['end_ms'],orders_enabled=False)))
    else:
        result=evaluate(a.model,json.loads(Path(a.registration).read_text()),json.loads(Path(a.spec).read_text()),a.output)
        print(json.dumps(result['assessment']))


if __name__=='__main__':main()
