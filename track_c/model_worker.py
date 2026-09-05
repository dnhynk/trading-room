"""Read-only market/ledger research worker; publishes immutable model versions."""
import argparse
import gzip
import json
from pathlib import Path
import sqlite3
import time

from .dataset import build
from .estimation import read
from .feedback import extract
from .settings import load
from .train import train


def atomic(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp'); temporary.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n',encoding='utf-8'); temporary.replace(path)


def refresh(config):
    base=Path(config['data_directory']); models=base/'models'; models.mkdir(exist_ok=True)
    lock=sqlite3.connect(models/'worker.lock.sqlite',timeout=0)
    try: lock.execute('BEGIN EXCLUSIVE')
    except sqlite3.OperationalError: lock.close(); raise RuntimeError('model worker already running') from None
    cutoff=int(time.time()*1000)-5000
    try:
        feedback,execution=extract(base,cutoff)
        atomic(models/'execution.json',execution)
        folder=base/'research'/str(cutoff); folder.mkdir(parents=True,exist_ok=False)
        # Preserve complete source rows up to a fixed receive-time cutoff. A live
        # gzip tail may not yet have its footer; only complete JSON records survive.
        frozen=folder/'public.jsonl.gz'; first=cutoff-2*3600000; count=0
        with gzip.open(frozen,'wt',encoding='utf-8') as out:
            for source in sorted((base/'public').glob('*.jsonl.gz'))[-4:]:
                try:
                    with gzip.open(source,'rt',encoding='utf-8') as stream:
                        for line in stream:
                            try: row=json.loads(line); t=int(row['received_ms'])
                            except (ValueError,KeyError): continue
                            if first<=t<=cutoff: out.write(line); count+=1
                except (EOFError,OSError): continue
        captures=[]
        # An as-of metadata timeline is retained; no later price-band policy is
        # substituted for a past contract. Dataset reader chooses by decision time.
        for source in sorted((base/'contracts').glob('*.json')):
            doc=json.loads(source.read_text())
            for m in doc['markets']:
                if m['available_ms']<=cutoff: captures.append(m)
        if not captures or not count: raise ValueError('insufficient source data')
        contract_path=folder/'contracts.json'; atomic(contract_path,dict(captures=captures))
        latency=execution['submit_rtt_p95_ms'] or 250
        spec=dict(contracts=str(contract_path),sources=[str(frozen)],anchor_ms=4000,max_states=2400,stale_ms=config['quote_max_age_ms'],
                  latency_ms=max(250,min(5000,round(latency))),maker=0.,taker=0.,ttls=[1,4,12],horizons=[2,8,32],
                  size_multiples=[1.05,2,8,32],target_ticks=[1,2])
        dataset=folder/'dataset'; result=build(spec,dataset)
        matured=[r for r in feedback if first<=r['t'] and r['end']<=cutoff]
        atomic(dataset/'exchange-labels.json',matured)
        learned=train(dataset)
        document=read(dataset/'model.json',cutoff+1)
        version=models/(document['digest']+'.json')
        if not version.exists(): atomic(version,document)
        elif read(version,cutoff+1)!=document: raise ValueError('immutable version collision')
        # Sparse/negative evidence remains research. Promotion requires the
        # separately recorded, model-count-corrected live evidence criterion.
        if not all(document['models'].values()): raise ValueError('incomplete fitted components')
        atomic(models/'current.json',document)
        report=dict(t_ms=int(time.time()*1000),status='published',digest=document['digest'],state=document['state'],dataset=str(dataset),
                    states=result['states'],labels=result['labels'],execution=execution,validation=learned['validation'])
        atomic(models/'worker-status.json',report)
        return report
    except Exception as exc:
        # No raw exception text: paths, credentials and network payloads do not leak.
        atomic(models/'worker-status.json',dict(t_ms=int(time.time()*1000),status='failed',error_type=type(exc).__name__,retained_previous=True))
        raise
    finally: lock.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--config',required=True); args=parser.parse_args()
    try:
        r=refresh(load(args.config)); print(json.dumps({k:r[k] for k in ('t_ms','status','digest','states','labels')}))
    except Exception as exc:
        print(json.dumps(dict(status='failed',error_type=type(exc).__name__)),flush=True); raise SystemExit(1) from None


if __name__=='__main__': main()
