"""C4 execution with explicit observation gaps and paired, delayed exit labels.

Order queue, fills, cancellation races, book consumption and accounting are reused.
This module has no order submission capability. The frozen C4 is not edited.
"""
from copy import deepcopy

from track_c.replay.queue import Attempt as OriginalAttempt
from track_c.market.microstructure import liquidate
from track_c.learning.features import vector


class Attempt(OriginalAttempt):
    def __init__(self,*args,queue_multiplier=1.,**kwargs):
        super().__init__(*args,**kwargs)
        if queue_multiplier<1:raise ValueError('queue stress cannot improve position')
        self.queue_multiplier=queue_multiplier
        self.observation_gap=False
        self.last_value=None
        self.landmarks_seen=set()

    def advance(self,now):
        if not self.done and self.book is not None:
            resting=not self.entry_done and now>=self.arrival
            exposed=self.qty>self.a['qty_step']*.1
            until=now if exposed else min(now,self.cancel_at if self.cancel_at is not None else
                                         self.deadline+self.cfg['cancel_latency_ms'])
            if (resting or exposed) and until-self.book['t']>self.cfg['book_max_age_ms']:
                self.censored=True
                self.observation_gap=True
        active=self.active
        pending=deepcopy(self.pending_exit)
        super().advance(now)
        # Original advance's final dust check also matched qty==0 and overwrote
        # completed recovery/stop/timeout causes with 'residual'. Preserve the exit.
        if pending and now>=pending['at'] and self.done and self.qty<=self.a['qty_step']*.1:
            self.reason,self.end=pending['reason'],pending['at']
        if not active and self.active:self.ahead*=self.queue_multiplier

    def sell(self,now,limit):
        # The quote can fall below the minimum between the decision and arrival.
        if self.fresh(now) and self.qty*self.book['bids'][0][0]<self.a['minimum']:
            self.finish(now,'residual')
            return
        super().sell(now,limit)

    def request_exit(self,now,reason,limit=0.):
        self.requested=reason
        self.cancel(now)
        if self.entry_done and self.qty and self.pending_exit is None:
            self.pending_exit=dict(at=now+self.cfg['latency_ms'],limit=limit,reason=reason)

    def liquidation_state(self,state):
        return dict(state,bids=self.book['bids'],asks=self.book['asks'],book_ms=self.book['t'],
                    bid=self.book['bids'][0][0],ask=self.book['asks'][0][0])

    def decide(self,now,state):
        self.advance(now)
        if self.done:return
        if state:self.last_ref=state['reference']
        ref=(state or {}).get('reference',{})
        fresh=bool(state and self.fresh(now))
        if not self.entry_done and (not fresh or not ref.get('ready') or ref.get('lower',0)<=self.a['price']):
            self.cancel(now)
        if self.qty and fresh and self.qty*min(state['bid'],self.a['stop_limit'])>=self.a['minimum']*1.05:
            self.cancel(now)
        if self.first_fill is None:return
        age=(now-self.first_fill)/1000
        reason=self.requested if self.requested!='recovery' else None
        if not fresh or not ref.get('ready'):reason='data_loss'
        elif state['bid']<=self.a['stop']:reason='stop'
        elif ref.get('m10',0)<=-self.cfg['common_drop_ticks'] or ref['upper']<self.a['price']:reason='collapse'
        if age>=self.a['hold_s'] and not reason:reason='timeout'
        bids=[(p,q*self.cfg['depth_haircut']) for p,q in self.book['bids']] if fresh else []
        vwap=liquidate(bids,self.qty) if self.qty else None
        limit=0.
        if not reason and vwap is not None:
            if vwap>=ref['lower']-self.a['tick']:
                reason,limit='recovery',max(0.,ref['lower']-self.a['tick'])
            elif self.exit_model and self.entry_done:
                if self.exit_model.doc['kind']=='c4-paired-exit-v2':
                    value=self.exit_model.continuation(self.a,age,now,vwap,state=self.liquidation_state(state),qty=self.qty)
                else:
                    value=self.exit_model.continuation(self.a,age,now,vwap)
                self.last_value=value
                if value.get('ready') and not value['hold']:reason='value'
        if reason:self.request_exit(now,reason,limit)
        elif self.requested=='recovery':self.requested=None

    def clone(self):
        copy=object.__new__(type(self))
        copy.__dict__.update({k:deepcopy(v) for k,v in self.__dict__.items() if k not in ('cfg','exit_model')})
        copy.cfg,copy.exit_model=self.cfg,self.exit_model
        return copy

    def result(self,now=None):
        result=super().result(now)
        if result['censored']:result['cause']=None
        result.update(observation_gap=self.observation_gap,label_kind='public_queue_counterfactual_v2',
                      gross_bp=result['gross_exit_krw']/self.a['notional']*10000,
                      spent_bp=(self.bought*self.a['price']+self.fees)/self.a['notional']*10000)
        return result


class ExitPair:
    """Both arms start with exactly the same remaining inventory and book."""
    def __init__(self,hold,now,state):
        if not hold.entry_done or hold.pending_exit or hold.done or not hold.qty:
            raise ValueError('landmark must have settled entry and unresolved inventory')
        self.hold=hold
        self.sell=hold.clone()
        self.sell.exit_model=None
        self.sell.request_exit(now,'landmark_sale')
        self.at=now
        self.gross,self.fees,self.qty=hold.gross,hold.fees,hold.qty
        self.x=vector(hold.a,hold.liquidation_state(state),hold.cfg,qty=hold.qty,age=(now-hold.first_fill)/1000)

    @property
    def done(self):return self.hold.done and self.sell.done

    def result(self,now):
        h,s=self.hold.result(now),self.sell.result(now)
        notional=self.qty*self.hold.a['price']
        hold=(self.hold.gross-self.gross)-(self.hold.fees-self.fees)
        sell=(self.sell.gross-self.gross)-(self.sell.fees-self.fees)
        return dict(action=self.hold.a,episode_id=self.hold.a['episode_id'],start_ms=self.hold.start,
                    landmark_ms=self.at,end_ms=max(h['end_ms'],s['end_ms']),x=self.x,
                    filled_qty=self.qty,censored=h['censored'] or s['censored'],
                    delta_bp=(hold-sell)/notional*10000,hold_bp=hold/notional*10000,
                    sell_bp=sell/notional*10000,hold_cash_krw=hold,sell_cash_krw=sell,
                    extra_occupied_s=(h['end_ms']-s['end_ms'])/1000,
                    hold_reason=h['reason'],sell_reason=s['reason'],
                    hold_residual_qty=h['residual_qty'],sell_residual_qty=s['residual_qty'],
                    policy='fixed_structural_until_end',execution='shared_delayed_attempt_v2')
