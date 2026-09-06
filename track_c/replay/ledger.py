"""Chronological, purged development pipeline over frozen public recordings."""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from track_c.replay.input import coinone_rows
from track_c.replay.dataset import public_contracts, asof, sha
from track_c.market.leaders import rows as leader_rows
from track_c.replay.stream import EventSpool
from track_c.learning.config import load, digest, sources
from track_c.replay.queue import Attempt
from track_c.market.state import Market, candidates


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    tmp.replace(path)


def frames(spool, cfg, contracts, units, until=None, *, markets=None, resume_ms=None):
    markets = markets if markets is not None else {c:Market(c,cfg) for c in cfg['coins']}
    stream = iter(spool)
    row = next(stream, None)
    if resume_ms is not None:
        while row and row[0]<=resume_ms:row=next(stream,None)
    stop = min(spool.end, until) if until is not None else spool.end
    first=resume_ms if resume_ms is not None else spool.start
    for now in range((first//cfg['decision_ms']+1)*cfg['decision_ms'], stop+1, cfg['decision_ms']):
        batch = []
        while row and row[0] <= now:
            at, _, kind, body = row
            if kind == 'coinone':
                coin, channel, data, _ = body
                event = markets[coin].feed(channel, data, at)
                if event: batch.append((coin, event))
            elif kind == 'leader': markets[body[3]].reference.quote(body)
            else:
                for m in markets.values(): m.reference.quote(body)
            row = next(stream, None)
        snaps = {}
        for coin, market in markets.items():
            contract, ladder = asof(contracts, coin, now), asof(units, coin, now)
            ladder = ladder['rows'] if ladder else ([dict(range_min=0, price_unit=contract['price_unit'])]
                                                   if contract and contract.get('price_unit') else None)
            snaps[coin] = market.snapshot(now, contract, ladder)
        yield now, snaps, batch


class Book:
    """Selected counterfactuals share one cash/risk account. Dust stays visible."""
    def __init__(self, cfg, cash):
        self.cfg, self.initial, self.cash = cfg, float(cash), float(cash)
        self.active = None
        self.rows, self.residuals, self.curve = [], [], []
        self.peak = float(cash)
        self.drawdown = 0.
        self.day = None
        self.loss_day = self.realized_day = 0.
        self.decisions = Counter()
        self.risk_peak = 0.
        self.started_ms = None

    def residual_value(self, snaps):
        return sum(r['qty']*((snaps.get(r['coin']) or {}).get('bid', r['mark'])) for r in self.residuals)

    def equity(self, snaps):
        active_value = 0.
        if self.active:
            a = self.active
            active_value = a.gross-a.bought*a.a['price']-a.fees+a.qty*((snaps.get(a.a['coin']) or {}).get('bid',0.))
        return self.cash+self.residual_value(snaps)+active_value

    def capacity(self, snaps):
        equity = max(0., self.equity(snaps))
        # Unsellable residual capital is fully reserved; never silently reused.
        stranded = self.residual_value(snaps)
        unrealized = sum(min(0., r['qty']*((snaps.get(r['coin']) or {}).get('bid',r['mark']))-r['cost']) for r in self.residuals)
        daily = max(0., equity*self.cfg['daily_loss_fraction']+self.realized_day+unrealized-stranded)
        research = max(0., equity*self.cfg['risk_fraction']-self.loss_day-stranded)
        return max(0., self.cash), min(daily, research)

    def settle(self, now, snaps):
        day = now//86400000
        if day != self.day:
            self.day, self.loss_day, self.realized_day = day, 0., 0.
        if self.active and self.active.done:
            row = self.active.result()
            self.cash += row['cash_net_krw']
            sold_cost = row['sold_qty']*row['action']['price']
            realized = row['gross_exit_krw']-sold_cost-row['fees_krw']
            self.realized_day += realized
            self.loss_day += max(0., -realized)
            if row['residual_qty']:
                self.residuals.append(dict(coin=row['action']['coin'], qty=row['residual_qty'],
                                          cost=row['residual_cost_krw'], mark=(snaps.get(row['action']['coin']) or {}).get('bid',0.),
                                          episode_id=row['episode_id']))
            self.rows.append(row)
            self.active = None
        wealth = self.equity(snaps)
        self.peak = max(self.peak, wealth)
        self.drawdown = max(self.drawdown, self.peak-wealth)
        if not self.curve or now//60000 != self.curve[-1][0]//60000: self.curve.append([now,wealth])
        return wealth

    def report(self, now, snaps):
        wealth = self.equity(snaps)
        active = self.active.result(now) if self.active else None
        rows = self.rows+([active] if active else [])
        mark = self.residual_value(snaps)+(active['residual_value_krw'] if active else 0.)
        cash_net = sum(r['cash_net_krw'] for r in rows)
        net = wealth-self.initial
        error = net-cash_net-mark
        if abs(error) > 1e-6: raise AssertionError('C4 cash/inventory identity')
        filled = [r for r in rows if r['filled_qty']]
        return dict(attempts=len(rows), filled_attempts=len(filled), no_fill=len(rows)-len(filled),
                    cash_krw=self.cash+(active['cash_net_krw'] if active else 0.), terminal_equity_krw=wealth, marked_net_krw=net,
                    cash_recovery_stress_krw=cash_net, residual_value_krw=mark,
                    residuals=self.residuals, open_attempt=active,
                    max_drawdown_krw=self.drawdown, accounting_error_krw=error,
                    buy_notional_krw=sum(r['filled_qty']*r['action']['price'] for r in rows),
                    exit_notional_krw=sum(r['gross_exit_krw'] for r in rows),
                    external_pnl_krw=sum(r['external_pnl_krw'] for r in rows),
                    relative_pnl_krw=sum(r['relative_pnl_krw'] for r in rows),
                    fees_krw=sum(r['fees_krw'] for r in rows),
                    decisions=dict(self.decisions), outcomes=rows, curve=self.curve)

    def export(self):
        state = {k:deepcopy(v) for k,v in self.__dict__.items() if k not in ('cfg','active','decisions')}
        state['decisions'] = dict(self.decisions)
        state['active'] = ({k:deepcopy(v) for k,v in self.active.__dict__.items() if k not in ('cfg','exit_model')}
                           if self.active else None)
        return state

    @classmethod
    def restore(cls, state, cfg, exit_model):
        book = cls(cfg,state['initial'])
        for k in book.__dict__:
            if k not in ('cfg','active','decisions'): setattr(book,k,deepcopy(state[k]))
        book.decisions = Counter(state['decisions'])
        if state['active']:
            attempt = object.__new__(Attempt)
            attempt.__dict__.update(deepcopy(state['active']),cfg=cfg,exit_model=exit_model)
            book.active = attempt
        return book


