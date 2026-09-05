"""Chronological, purged model fitting and reproducible artifact publication."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics
import time

from .dataset import build, rows, sha
from .estimation import NAMES, VERSION, calibrate, digest, fit, loss, predict, purged, validate_artifact
from .microstructure import SCHEMA, WINDOWS


def live_evidence(real, prior_scale_bp, trials):
    """Conservative prequential block estimate; a criterion, never a guarantee.

    Zero fills cannot certify profitability. Include all no-fill attempts in the
    opportunity distribution and add a zero-mean dispersion prior estimated from
    training outcomes, so identical tiny samples do not claim zero uncertainty.
    """
    groups=defaultdict(list)
    for r in real: groups[r['t']//60000].append(r['net_bp'])
    values=[statistics.mean(v) for v in groups.values()]
    if len(values)<2 or not any(r['filled'] for r in real):
        return dict(eligible=False,blocks=len(values),lower_bp=None,reason='live_outcomes_insufficient')
    n=len(values); mean=sum(values)/(n+1)
    variance=(sum((v-mean)**2 for v in values)+prior_scale_bp**2+mean**2)/n
    z=statistics.NormalDist().inv_cdf(1-.05/max(1,trials))
    # Small-sample t expansion widens the interval; label it approximate.
    z+=(z**3+z)/(4*n)+(5*z**5+16*z**3+3*z)/(96*n*n)
    lower=mean-z*math.sqrt(variance/(n+1))
    return dict(eligible=lower>0,blocks=n,mean_bp=mean,lower_bp=lower,prior_scale_bp=prior_scale_bp,
                method='time-block zero-mean shrinkage, approximate t interval, model-count correction',reason='positive_live_interval' if lower>0 else 'live_interval_nonpositive')


def choose(training, calibration, target, binary, penalties):
    choices=[]
    for penalty in penalties:
        model=fit(training,target,penalty=penalty,binary=binary)
        score=loss(model,calibration)
        choices.append((float('inf') if score is None else score,penalty,model))
    score,penalty,model=min(choices,key=lambda row:(row[0],-row[1]))
    return model,[dict(penalty=p,loss=s if math.isfinite(s) else None) for s,p,_ in choices]


def block_interval(outcomes, confidence=.95):
    groups=defaultdict(list)
    for t,value in outcomes: groups[t//60000].append(value)
    values=[statistics.mean(v) for v in groups.values()]
    if len(values)<2: return dict(blocks=len(values),mean=statistics.mean(values) if values else None,lower=None,upper=None)
    # Small-block normal intervals are descriptive, not an alpha certification.
    mean=statistics.mean(values); se=statistics.stdev(values)/math.sqrt(len(values)); z=statistics.NormalDist().inv_cdf((1+confidence)/2)
    return dict(blocks=len(values),mean=mean,lower=mean-z*se,upper=mean+z*se)


def train(directory, output=None):
    folder=Path(directory); meta=json.loads((folder/'dataset.json').read_text())
    labels=rows(folder/'labels.jsonl.gz'); continuations=rows(folder/'continuations.jsonl.gz')
    real_path=folder/'exchange-labels.json'
    real=json.loads(real_path.read_text()) if real_path.exists() else []
    labels.extend(real)
    if not labels: raise ValueError('no uncensored labels; do not invent a model')
    for row in labels: row['adverse_log']=math.log1p(row['adverse_bp'])
    timeline=sorted({r['t'] for r in labels}); start,end=timeline[0],max(r['end'] for r in labels)
    embargo=math.ceil((max(meta['spec']['ttls'])+max(meta['spec']['horizons']))*1000+2*meta['spec']['latency_ms'])
    # Common time boundaries across all symbols. The final slice remains unseen
    # during normalization, regularization search and probability calibration.
    inner_start=timeline[int(len(timeline)*.50)]; outer_start=timeline[int(len(timeline)*.75)]
    sets={}; evaluations={}; selected={}; trials={}
    for name,source,target,binary in (('fill',labels,'filled',True),('net',[r for r in labels if r['filled']],'net_bp',False),
                                       ('adverse',[r for r in labels if r['filled']],'adverse_log',False),('continuation',continuations,'net_bp',False)):
        first,validation=purged(source,inner_start,outer_start,embargo)
        model,search=choose(first,validation,target,binary,(1.,10.,100.))
        trials[name]=search
        # Inner holdout is used for calibration only; outer is never fitted.
        if binary: calibrate(model,validation)
        _,test=purged(source,outer_start,end+1,embargo)
        if model:
            model['validation_error']=loss(model,validation)
        evaluations[name]=dict(train_rows=len(first),calibration_rows=len(validation),test_rows=len(test),
                               outer_loss=loss(model,test),penalty=model['penalty'] if model else None)
        if not binary and model and test:
            errors=[(row['t'],row[target]-predict(model,row['x'])['mean']) for row in test]
            evaluations[name]['outer_bias']=block_interval(errors)
        selected[name]=model; sets[name]=dict(train_end=max((r['end'] for r in first),default=None),calibration_start=inner_start,test_start=outer_start)
    interval=block_interval([(r['t'],r['net_bp']) for r in labels if r['t']>=outer_start and r['filled']])
    prior=max(1e-8,statistics.pstdev(selected['net']['residuals'])) if selected.get('net') else 1e9
    evidence=live_evidence(real,prior,len({r['model'] for r in real})*9 or 9)
    # This artifact is research: public queues have not calibrated actual fills.
    # Technical validation cannot silently promote it to a profitable live model.
    document=dict(version=VERSION,feature_schema=SCHEMA,features=list(NAMES),feature_windows=list(WINDOWS),
                  trained_until=max(outer_start-1,max((r['end'] for r in real),default=0)) if evidence['eligible'] else outer_start-1,
                  created_ms=int(time.time()*1000),state='validated' if evidence['eligible'] else 'research',
                  ttl_grid=meta['spec']['ttls'],horizon_grid=meta['spec']['horizons'],target_grid=meta['spec'].get('target_ticks',[0]),confidence_alpha=.05,
                  quantity_support_krw=[min(r['entry']*r['quantity'] for r in labels),max(r['entry']*r['quantity'] for r in labels)],
                  models=selected,validation=dict(split=sets,embargo_ms=embargo,models=evaluations,regularization_trials=trials,
                                                 filled_outer_net_bp=interval,real_execution_calibrated=any(r['end']<outer_start for r in real),exchange_rows=len(real),live_evidence=evidence,
                                                 limitations=['queue labels are synthetic','one-day development data is not independent-day evidence',
                                                              'net model includes conservative dust writeoffs','normal block intervals are descriptive']),
                  data=dict(manifest_sha256=sha(folder/'dataset.json'),sources=meta['sources'],label_kind=meta['label_kind'],quality=meta['quality']),
                  training_policy='train-first-half_inner-calibration-next-quarter_outer-last-quarter',
                  code_sha256={name:sha(Path(__file__).with_name(name)) for name in ('microstructure.py','outcomes.py','estimation.py','policy.py','oms.py','portfolio.py','train.py')})
    document['digest']=digest(document); validate_artifact(document)
    target=Path(output) if output else folder/'model.json'
    target.parent.mkdir(parents=True,exist_ok=True); tmp=target.with_suffix('.tmp')
    tmp.write_text(json.dumps(document,indent=2,allow_nan=False)+'\n',encoding='utf-8'); tmp.replace(target)
    version=target.parent/(document['digest']+'.json')
    if version.exists() and json.loads(version.read_text())!=document: raise ValueError('immutable model collision')
    if not version.exists(): version.write_text(json.dumps(document,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    (folder/'validation.json').write_text(json.dumps(document['validation'],indent=2)+'\n',encoding='utf-8')
    return dict(model=str(target),digest=document['digest'],state=document['state'],validation=evaluations,
                labels=len(labels),filled=sum(r['filled'] for r in labels),effective_time_blocks=len({r['t']//60000 for r in labels}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec',type=Path); parser.add_argument('--dataset',required=True,type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.spec:
        report=build(json.loads(args.spec.read_text()),args.dataset)
        print(json.dumps(dict(stage='dataset',states=report['states'],labels=report['labels'],filled=report['filled'],quality=report['quality'])),flush=True)
    print(json.dumps(train(args.dataset,args.output)),flush=True)


if __name__=='__main__': main()
