"""Sparse local-price risk from accepted books, independent of reference readiness.

Missing reference quotes cannot erase observed local volatility. Missing local
10-second endpoints are omitted and explicitly inflate the risk proxy; no prices
are forward-filled across an outage. This is not a guaranteed stop-loss bound.
"""
from bisect import bisect_right
from collections import deque
import math
from statistics import NormalDist, fmean


class LocalRisk:
    def __init__(self, cfg):
        self.cfg=cfg
        self.points=deque()
        self.cached=None
        self.cached_end=None

    def observe(self, event):
        if event['kind']!='book':return
        t=event['t'];mid=(event['bids'][0][0]+event['asks'][0][0])/2
        if self.points and t<self.points[-1][0]:raise ValueError('backward local risk input')
        self.points.append((t,mid))
        cutoff=t-310000
        while len(self.points)>1 and self.points[1][0]<cutoff:self.points.popleft()

    def evaluate(self, now, tick):
        absent=dict(ready=False,reason='local_risk_warmup',n_returns=0,distance_price=None)
        if not self.points or not 0<=now-self.points[-1][0]<=self.cfg['sampling_book_age_ms']:return absent
        end=now//10000*10000
        if self.cached_end==end:return dict(self.cached)
        if self.points[0][0]>end-300000:return absent
        times=[t for t,_ in self.points];values=[p for _,p in self.points]
        samples=[]
        for target in range(end-300000,end+1,10000):
            i=bisect_right(times,target)-1
            samples.append((times[i],values[i]) if i>=0 and target-times[i]<=self.cfg['sampling_book_age_ms'] else None)
        returns=[((right[1]-left[1])**2/(right[0]-left[0])*1000) for left,right in zip(samples,samples[1:])
                 if left and right and right[0]>left[0]]
        n=len(returns)
        if n<20:return dict(absent,n_returns=n)
        sigma=math.sqrt(fmean(returns)*30/n)
        distance=max(tick,NormalDist().inv_cdf(.95)*sigma*math.sqrt(self.cfg['hold_s']))
        self.cached_end=end
        self.cached=dict(ready=True,reason=None,n_returns=n,missing_intervals=30-n,
                         sigma_price_sqrt_s=sigma,distance_price=distance,
                         sample_end_ms=end,source='accepted_local_books_sparse_10s',
                         interpretation='risk_proxy_not_guaranteed_loss_limit')
        return dict(self.cached)
