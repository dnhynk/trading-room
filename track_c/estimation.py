"""Regularized probabilistic models with vectorized deterministic JSON inference."""
import hashlib
import json
import math
from pathlib import Path
import statistics

from .microstructure import FEATURES, SCHEMA
from .outcomes import ACTION_FEATURES

NAMES=FEATURES+ACTION_FEATURES
VERSION=1


def digest(document):
    value={k:v for k,v in document.items() if k!='digest'}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def logistic(value):
    return 1/(1+math.exp(-max(-35,min(35,value))))


def fit(rows, target, *, penalty=10., binary=False, block_ms=60000):
    import numpy as np
    if len(rows)<2: return None
    raw=np.array([[r['x'][k] for k in NAMES] for r in rows],dtype=float)
    y=np.array([r[target] for r in rows],dtype=float)
    if not np.isfinite(raw).all() or not np.isfinite(y).all(): raise ValueError('nonfinite training data')
    # Action-grid expansions and overlapping anchors do not multiply evidence.
    groups={}
    for row in rows:
        key=int(row['t']//block_ms); groups[key]=groups.get(key,0)+1
    weight=np.array([1/groups[int(r['t']//block_ms)] for r in rows]); weight*=len(groups)/weight.sum()
    center=np.average(raw,axis=0,weights=weight)
    scale=np.sqrt(np.average((raw-center)**2,axis=0,weights=weight)); scale=np.maximum(scale,1e-8)
    z=(raw-center)/scale; X=np.column_stack((np.ones(len(raw)),z))
    reg=np.eye(X.shape[1])*penalty; reg[0,0]=1e-6
    if binary:
        beta=np.zeros(X.shape[1]); prior=float(np.clip(np.average(y,weights=weight),1e-4,1-1e-4)); beta[0]=math.log(prior/(1-prior))
        for _ in range(40):
            p=1/(1+np.exp(-np.clip(X@beta,-35,35)))
            variance=np.maximum(p*(1-p),1e-6)*weight
            H=X.T@(X*variance[:,None])+reg
            delta=np.linalg.solve(H,X.T@((y-p)*weight)-reg@beta)
            beta+=delta
            if np.max(np.abs(delta))<1e-7: break
        pred=1/(1+np.exp(-np.clip(X@beta,-35,35)))
        variance_scale=1.
    else:
        H=X.T@(X*weight[:,None])+reg
        beta=np.linalg.solve(H,X.T@(y*weight)); pred=X@beta
        variance_scale=float(np.average((y-pred)**2,weights=weight))
    # Cluster residuals by time across all symbols; effective sample size is blocks.
    residual=y-pred
    block_residual=[float(np.mean(residual[[int(r['t']//block_ms)==key for r in rows]])) for key in groups]
    cluster_se=statistics.stdev(block_residual)/math.sqrt(len(block_residual)) if len(block_residual)>1 else math.inf
    quantiles=np.quantile(residual,np.linspace(.01,.99,99)).tolist()
    return dict(binary=binary,penalty=penalty,center=center.tolist(),scale=scale.tolist(),coef=beta.tolist(),
                inverse=np.linalg.inv(H).tolist(),variance=max(variance_scale,1e-10),
                residuals=quantiles,cluster_se=cluster_se if math.isfinite(cluster_se) else 1e9,
                rows=len(rows),blocks=len(groups),support_low=np.min(raw,axis=0).tolist(),support_high=np.max(raw,axis=0).tolist(),
                support_distance=float(np.quantile(np.sum(z*z,axis=1),.995)),target=target,
                calibration=[],validation_error=None)


def predict_one(model, values):
    if model is None: return None
    raw=[float(values[k]) for k in NAMES]
    if not all(math.isfinite(v) for v in raw): raise ValueError('nonfinite inference')
    z=[(v-c)/s for v,c,s in zip(raw,model['center'],model['scale'])]
    x=[1.]+z; eta=sum(a*b for a,b in zip(x,model['coef']))
    leverage=max(0,sum(x[i]*sum(v*x[j] for j,v in enumerate(row)) for i,row in enumerate(model['inverse'])))
    uncertainty=math.sqrt(leverage*model['variance']+model['cluster_se']**2)
    value=logistic(eta) if model['binary'] else eta
    if model['binary'] and model.get('calibration'):
        # Bins are learned only from calibration data; no outcome-dependent test bins.
        cell=min(model['calibration'],key=lambda cell:abs(cell['p']-value))
        value=cell['rate']
        lower,upper=cell['lower'],cell['upper']
    elif model['binary']:
        lower,upper=logistic(eta-1.96*uncertainty),logistic(eta+1.96*uncertainty)
    else: lower,upper=value-1.96*uncertainty,value+1.96*uncertainty
    distance=sum(v*v for v in z)
    return dict(mean=value,lower=lower,upper=upper,se=uncertainty,
                distance=distance,supported=distance<=max(model['support_distance'],1e-8),
                residuals=model['residuals'])


def predict_many(model, values):
    import numpy as np
    if not values: return []
    raw=np.asarray([[v[k] for k in NAMES] for v in values],dtype=float)
    if not np.isfinite(raw).all(): raise ValueError('nonfinite inference')
    z=(raw-np.asarray(model['center']))/np.asarray(model['scale'])
    X=np.column_stack([np.ones(len(z)),z]); eta=X@np.asarray(model['coef'])
    leverage=np.maximum(0,np.sum((X@np.asarray(model['inverse']))*X,axis=1))
    uncertainty=np.sqrt(leverage*model['variance']+model['cluster_se']**2)
    val=1/(1+np.exp(-np.clip(eta,-35,35))) if model['binary'] else eta
    distance=np.sum(z*z,axis=1); result=[]
    for i,v in enumerate(val):
        se=float(uncertainty[i]); v=float(v)
        if model['binary'] and model.get('calibration'):
            cell=min(model['calibration'],key=lambda cell:abs(cell['p']-v)); v=cell['rate']; lower,upper=cell['lower'],cell['upper']
        elif model['binary']: lower,upper=logistic(float(eta[i])-1.96*se),logistic(float(eta[i])+1.96*se)
        else: lower,upper=v-1.96*se,v+1.96*se
        result.append(dict(mean=v,lower=lower,upper=upper,se=se,distance=float(distance[i]),
                           supported=bool(distance[i]<=max(model['support_distance'],1e-8)),residuals=model['residuals']))
    return result


def predict(model, values):
    return predict_many(model,[values])[0] if model is not None else None


def calibrate(model, rows, *, bins=6):
    if model is None or not rows: return
    # Equal-mass calibration bins retain resolution for rare fills; fixed [0,1]
    # bins collapsed every candidate TTL into the same probability bucket.
    ordered=sorted((p['mean'],row[model['target']],row['t']//60000)
                   for row,p in zip(rows,predict_many(model,[r['x'] for r in rows])))
    cells=[ordered[i*len(ordered)//bins:(i+1)*len(ordered)//bins] for i in range(bins)]
    result=[]
    for cell in cells:
        if not cell: continue
        # Binomial evidence is aggregated by time block, not action-grid count.
        blocks={}
        for p,y,b in cell: blocks.setdefault(b,[]).append(y)
        rates=[statistics.mean(values) for values in blocks.values()]
        n=len(rates); rate=(sum(rates)+statistics.mean(p for p,_,_ in cell))/(n+1)
        z=1.96; denom=1+z*z/n; center=(rate+z*z/(2*n))/denom
        radius=z*math.sqrt(rate*(1-rate)/n+z*z/(4*n*n))/denom
        result.append(dict(p=statistics.mean(p for p,_,_ in cell),rate=rate,lower=max(0,center-radius),upper=min(1,center+radius),blocks=n))
    model['calibration']=result


def loss(model, rows):
    if not rows or model is None: return None
    values=[]
    for row,p in zip(rows,predict_many(model,[r['x'] for r in rows])):
        prediction=p['mean']; y=row[model['target']]
        values.append((prediction-y)**2)
    return statistics.mean(values)


def purged(rows, start, end, embargo_ms):
    train=[r for r in rows if r['end']<start-embargo_ms]
    test=[r for r in rows if start<=r['t']<end and r['end']<end]
    return train,test


def validate_artifact(doc, *, now_ms=None):
    if doc.get('version')!=VERSION or doc.get('feature_schema')!=SCHEMA or doc.get('features')!=list(NAMES):
        raise ValueError('model schema mismatch')
    if doc.get('digest')!=digest(doc): raise ValueError('model digest mismatch')
    if now_ms is not None and doc['trained_until']>=now_ms: raise ValueError('future-trained model')
    if doc.get('state') not in ('research','validated'): raise ValueError('unsupported model state')
    for name in ('fill','net','adverse','continuation'):
        m=doc['models'].get(name)
        if m is None: continue
        d=len(NAMES)
        if len(m['coef'])!=d+1 or len(m['center'])!=d or len(m['scale'])!=d or len(m['inverse'])!=d+1 or any(len(row)!=d+1 for row in m['inverse']):
            raise ValueError('model dimensions')
        nums=m['coef']+m['center']+m['scale']+m['residuals']+[v for row in m['inverse'] for v in row]+[m['variance'],m['cluster_se'],m['support_distance']]
        if not all(math.isfinite(v) for v in nums) or any(s<=0 for s in m['scale']): raise ValueError('invalid model numbers')
        if min(m['variance'],m['cluster_se'],m['support_distance'])<0 or not m['residuals']: raise ValueError('invalid uncertainty')
        for cell in m.get('calibration',[]):
            if not all(math.isfinite(cell[k]) for k in ('p','rate','lower','upper')) or not 0<=cell['lower']<=cell['rate']<=cell['upper']<=1:
                raise ValueError('invalid calibration')
    return doc


def read(path, now_ms):
    return validate_artifact(json.loads(Path(path).read_text(encoding='utf-8')),now_ms=now_ms)
