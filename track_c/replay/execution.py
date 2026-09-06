"""C4 execution with explicit observation gaps and paired, delayed exit labels.

Order queue, fills, cancellation races, book consumption and accounting are reused.
This module has no order submission capability. The frozen C4 is not edited.
"""
from copy import deepcopy
import math

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
        self.protection=None
        self.protect_sold=0.
        self.exit_phases=[]

    def arm_protection(self,now):
        if (self.cfg['exit_protocol']=='protect_cancel_reconcile' and self.entry_done and self.qty
                and not self.done and not self.requested and not self.pending_exit and self.protection is None
                and self.fresh(now) and self.qty*self.a['stop_limit']>=self.a['minimum']):
            self.protection=dict(arrival=now+self.cfg['latency_ms'],cancel_at=None,triggered=False,ahead=0.)

    def protective_fill(self,now,price,qty):
        q=math.floor((min(self.qty,qty)+1e-14)/self.a['qty_step'])*self.a['qty_step']
        if q<=0:return
        self.sold+=q;self.protect_sold+=q;self.gross+=q*price
        self.fees+=q*price*self.cfg['fee_bp']/10000
        fair_in=self.fair_at_fill/self.bought
        fair_out=self.last_ref.get('fair',fair_in)
        if now-self.last_ref.get('t_ms',0)>self.cfg['leader_max_age_ms']:self.attribution_complete=False
        self.external_pnl+=q*(fair_out-fair_in)
        self.relative_pnl+=q*((price-fair_out)-(self.a['price']-fair_in))

    def event(self,event):
        super().event(event)
        if self.done:return
        self.arm_protection(event['t'])
        p=self.protection
        if not p or event['t']<p['arrival'] or event['kind']!='trade':return
        before=self.sold
        if not p['triggered'] and event['price']<=self.a['stop']:
            # Receive-time public trades are a trigger hypothesis, not verified
            # exchange trigger/matching timestamps. The limit still constrains fills.
            p['triggered']=True
            p['ahead']=sum(q for px,q in self.book['asks'] if px==self.a['stop_limit'])*self.queue_multiplier
            OriginalAttempt.sell(self,event['t'],self.a['stop_limit'])
            self.protect_sold+=self.sold-before
        elif p['triggered'] and event['buy'] and event['price']>=self.a['stop_limit']:
            if event['price']>self.a['stop_limit']:p['ahead']=0.
            ahead=min(p['ahead'],event['qty']);p['ahead']-=ahead
            self.protective_fill(event['t'],self.a['stop_limit'],event['qty']-ahead)
        if self.sold>before:
            self.requested='exchange_stop'
            if self.pending_exit:self.pending_exit.update(reason='exchange_stop',limit=0.)
            if self.qty<=self.a['qty_step']*.1:
                self.protection=self.pending_exit=None
                self.finish(event['t'],'exchange_stop')

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
        if self.protection and self.protection['cancel_at'] is not None and now>=self.protection['cancel_at']:
            self.protection=None
        super().advance(now)
        # Original advance's final dust check also matched qty==0 and overwrote
        # completed recovery/stop/timeout causes with 'residual'. Preserve the exit.
        if pending and now>=pending['at'] and self.done and self.qty<=self.a['qty_step']*.1:
            self.reason,self.end=pending['reason'],pending['at']
        if not active and self.active:self.ahead*=self.queue_multiplier
        # A residual may still be reserved by a protective order. Settle that order
        # before transferring the remainder; it can fill during cancellation.
        if self.done and self.reason=='residual' and self.protection:
            self.done=False;self.end=self.reason=None
            self.request_exit(now,'residual')
        self.arm_protection(now)

    def sell(self,now,limit):
        # The quote can fall below the minimum between the decision and arrival.
        if self.fresh(now) and self.qty*self.book['bids'][0][0]<self.a['minimum']:
            self.finish(now,'residual')
            return
        super().sell(now,limit)

    def request_exit(self,now,reason,limit=0.):
        self.requested=reason
        self.cancel(now)
        if self.pending_exit and reason!='recovery':
            self.pending_exit.update(reason=reason,limit=limit)
        if self.entry_done and self.qty and self.pending_exit is None:
            release=now
            if self.protection:
                if self.protection['cancel_at'] is None:
                    self.protection['cancel_at']=max(now,self.protection['arrival'])+self.cfg['cancel_latency_ms']
                release=self.protection['cancel_at']
            at=release+self.cfg['latency_ms']
            self.pending_exit=dict(at=at,limit=limit,reason=reason)
            self.exit_phases.append(dict(request_ms=now,protection_settled_ms=release,market_arrival_ms=at,
                                         protection_present=self.protection is not None))

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
        self.arm_protection(now)

    def clone(self):
        copy=object.__new__(type(self))
        copy.__dict__.update({k:deepcopy(v) for k,v in self.__dict__.items() if k not in ('cfg','exit_model')})
        copy.cfg,copy.exit_model=self.cfg,self.exit_model
        return copy

    def result(self,now=None):
        result=super().result(now)
        if result['censored']:result['cause']=None
        result.update(observation_gap=self.observation_gap,label_kind='public_queue_counterfactual_v3',
                      exit_protocol=self.cfg['exit_protocol'],protective_sold_qty=self.protect_sold,
                      exit_phases=self.exit_phases,live_execution_verified=False,
                      timing_basis='fixed_scenario; cancellation_settlement_then_balance_and_submission',
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
                    policy='fixed_structural_until_end',execution='shared_delayed_attempt_v3',
                    exit_protocol=self.hold.cfg['exit_protocol'],live_execution_verified=False)
