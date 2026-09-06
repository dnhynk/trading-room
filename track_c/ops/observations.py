"""Non-decision observations: local sell-pressure episodes and executable depth.

An episode is an audit label, not an identified non-informational shock. Its fixed
trigger is one aggressive sale at least as large as the preceding top bid, while
Coinone is below its contemporaneously available fair reference. A 32s quiet gap
ends the episode. Every 1Hz frame is kept, including rejected and unfilled states.
"""
from collections import Counter
import gzip
import json
from pathlib import Path
import time

VERSION='c3-observations-v1'
FAIR_FIELDS=('evaluated_ms','coinone_mid','tick','fair','reference_price','basis_ratio','reference_components','dev_ticks','m30','m10','leader_disagreement_ticks','risk')


class Observations:
    def __init__(self,directory):
        self.folder=Path(directory)/'observations'; self.folder.mkdir(exist_ok=True)
        self.raw=None; self.hour=None; self.last_flush=0
        self.episodes={}; self.last_frame={}; self.counts=Counter()

    def write(self,row):
        hour=time.strftime('%Y%m%d-%H',time.gmtime(row['t_ms']/1000))
        if hour != self.hour:
            if self.raw: self.raw.close()
            self.raw=gzip.open(self.folder/(hour+'.jsonl.gz'),'at',encoding='utf-8'); self.hour=hour
        self.raw.write(json.dumps(row,separators=(',',':'),allow_nan=False)+'\n')
        self.counts[row['kind']]+=1
        if time.monotonic()-self.last_flush>=5:
            self.raw.flush(); self.last_flush=time.monotonic()

    def expire(self,coin,now):
        episode=self.episodes.get(coin)
        if episode and now-episode['last_sell_ms']>32000:
            self.write(dict(kind='EPISODE_END',t_ms=now,coin=coin,**episode))
            self.episodes.pop(coin)

    def public(self,coin,data,recv,prior,fair):
        self.expire(coin,recv)
        if data.get('is_seller_maker') is not False or not prior or not prior['bids']:
            return
        age=recv-prior['t_ms']
        if not 0<=age<=1500:
            self.counts['missing_pretrade_book']+=1; return
        bid,qty=prior['bids'][0]
        gross=float(data['price'])*float(data['qty']); depth=bid*qty
        ratio=gross/depth if depth>0 else None
        evaluated=(fair or {}).get('evaluated_ms')
        fresh_fair=evaluated is not None and 0<=recv-evaluated<=1500
        mid=(bid+prior['asks'][0][0])/2 if prior.get('asks') else None
        dev=(fair['fair']-mid)/prior['tick'] if fresh_fair and mid and prior.get('tick') else None
        trigger=ratio is not None and ratio>=1 and dev is not None and dev>0
        if trigger and coin not in self.episodes:
            self.episodes[coin]=dict(episode_id=coin+'-'+str(recv),start_ms=recv,last_sell_ms=recv)
            self.write(dict(kind='EPISODE_START',t_ms=recv,coin=coin,**self.episodes[coin]))
        episode=self.episodes.get(coin)
        if episode: episode['last_sell_ms']=recv
        self.write(dict(kind='SELL_PRESSURE',t_ms=recv,exchange_ms=int(data['timestamp']),coin=coin,
                        episode_id=episode['episode_id'] if episode else None,trigger=trigger,
                        prior_book_ms=prior['t_ms'],prior_top_bid_krw=depth,sell_gross_krw=gross,sell_to_prior_bid=ratio,
                        prior_book_exchange_ms=prior.get('exchange_ms'),current_discount_ticks=dev,
                        fair={k:(fair or {}).get(k) for k in FAIR_FIELDS if k!='risk'}))

    def frame(self,coin,now,micro,fair,campaign,selection,paused):
        if self.last_frame.get(coin)==now//1000: return
        self.last_frame[coin]=now//1000; self.expire(coin,now)
        episode=self.episodes.get(coin)
        self.write(dict(kind='FRAME',schema=VERSION,t_ms=now,coin=coin,
                        book_ms=micro.book_ms if micro else None,
                        book_exchange_ms=micro.book_exchange if micro else None,
                        bids=list(micro.bids) if micro else [],asks=list(micro.asks) if micro else [],
                        fair={k:(fair or {}).get(k) for k in FAIR_FIELDS},
                        episode_id=episode['episode_id'] if episode else None,
                        campaign_id=campaign['id'] if campaign else None,
                        qty=campaign['qty'] if campaign else '0',cost=campaign['cost'] if campaign else '0',
                        selection={k:(selection or {}).get(k) for k in ('reason','accepted','dev_ticks','m30','m10','flow32')},paused=paused))

    def close(self):
        if self.raw: self.raw.close(); self.raw=None
