"""Replay of the C3 rule over recorded Coinone + leader tapes with the production OMS.

Same FairValue/rule/Portfolio/OMS code as live; the exchange is the conservative
public-queue counterfactual (track_c.simulation.Exchange). Controls: `flip` negates
the fair-value deviation, `unconditional` removes gate/cancel/defend (stop/time only).
Outcomes are distinct campaigns (one fill event each), grouped by KST day.

python -m track_c.c3_replay --config track_c/config-c3.json --contracts <public.json> \
    --coinone <files...> --leaders <files...> --output out.json [--control flip|unconditional]
"""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal as D
import gzip
import json
import math
from pathlib import Path
import statistics

from .dataset import public_contracts
from .fair import FairValue
from .leaders import rows as leader_rows
from .microstructure import Micro
from .outcomes import Path as Tape
from .portfolio import Portfolio
from . import rule
from .settings import load
from .simulation import Exchange, MemoryStore
from .sizing import price_unit

KST_MS = 9 * 3600000


def coinone_rows(paths):
    """Yields (recv_ms, message) from recorder files (object or string message)."""
    for path in paths:
        opener = gzip.open if str(path).endswith('.gz') else open
        with opener(path, 'rt', encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    recv = int(row.get('received_ms', row.get('recv_ms')))
                    msg = row['message']
                    msg = json.loads(msg) if isinstance(msg, str) else msg
                except (ValueError, KeyError, TypeError):
                    continue
                if msg.get('response_type') == 'DATA':
                    yield recv, msg


def run(cfg, contracts_path, coinone, leaders, *, control=None, latency_ms=250, cash=594574, step_ms=500):
    cfg = dict(cfg, mode='live', funding_confirmed=True)
    if control == 'unconditional':
        cfg.update(entry_ticks=-1e9, cancel_ticks=-1e9, defend_ticks=-1e9)
    contracts, units = public_contracts(contracts_path)
    coins = [c for c in cfg['coins'] if c in contracts]
    events = []  # (t, order, kind, payload)
    warm = {}
    for recv, msg in coinone_rows(coinone):
        data = msg.get('data') or {}
        coin = data.get('target_currency')
        if coin not in coins:
            continue
        micro = warm.setdefault(coin, Micro(coin, stale_ms=cfg['quote_max_age_ms']))
        event = micro.feed(msg.get('channel'), data, recv)
        if event:
            events.append((recv, 0, 'coinone', (coin, msg.get('channel'), data, event)))
    for path in leaders:
        for row in leader_rows(path):
            if row[0] == 'b' and row[3] in coins:
                events.append((row[1], 1, 'leader', row))
    events.sort(key=lambda e: (e[0], e[1]))
    if not events:
        raise ValueError('no replayable events')
    tapes = {c: Tape([e[3][3] for e in events if e[2] == 'coinone' and e[3][0] == c], stale_ms=cfg['quote_max_age_ms']) for c in coins}
    tapes = {c: t for c, t in tapes.items() if t.events}
    clock = [events[0][0] / 1000]
    store = MemoryStore(lambda: clock[0])
    client = Exchange(tapes, lambda: clock[0], latency_ms, cash=cash)
    portfolio = Portfolio(cfg, client, store, clock=lambda: clock[0])
    portfolio.sync_cash(client.cash)
    fairs = {c: FairValue(window_s=cfg['ratio_window_s'], min_samples=cfg['ratio_min_samples'], leader_max_age_ms=cfg['leader_max_age_ms'],
                          flip=(control == 'flip'), weights=cfg.get('leader_weights'), price=cfg.get('leader_price', 'microprice')) for c in coins}
    micros = {}
    last_fair = {}
    decisions = Counter()
    index = 0
    last_event = events[0][0]
    ladders = {c: units.get(c, {}).get('rows', [dict(range_min=0, price_unit=contracts[c]['price_unit'])]) for c in coins}
    last_decision = -1

    def live_book(coin, now):
        micro = micros.get(coin)
        if not micro or not micro.bids or not 0 <= now - micro.book_ms <= cfg['liveness_ms']:
            return None
        bid, ask = micro.bids[0][0], micro.asks[0][0]
        return dict(bid=bid, ask=ask, tick=float(price_unit(ladders[coin], D(str(bid)))))

    start_t = (events[0][0] // step_ms + 1) * step_ms
    end_t = events[-1][0] - (cfg['hold_s'] + cfg['entry_ttl_s']) * 1000 - 3000
    for t in range(start_t, events[-1][0] + step_ms, step_ms):
        clock[0] = t / 1000
        while index < len(events) and events[index][0] <= t:
            at, _, kind, payload = events[index]
            index += 1
            if at - last_event > 120000:
                micros = {}
            last_event = at
            if kind == 'coinone':
                coin, channel, data, event = payload
                if coin in tapes:
                    client.event(coin, event)
                micros.setdefault(coin, Micro(coin, stale_ms=cfg['quote_max_age_ms'])).feed(channel, data, at)
            else:
                fairs[payload[3]].leader_quote(payload[2], payload[1], payload[5], payload[7], payload[6], payload[8])
        client.settle(t)
        for coin in coins:
            book = live_book(coin, t)
            last_fair[coin] = fairs[coin].evaluate(t, (book['bid'] + book['ask']) / 2, book['tick']) if book else None
        for coin in list(portfolio.campaigns):
            c = portfolio.campaigns[coin]
            book = live_book(coin, t)
            fair = last_fair.get(coin)
            dev = fair['dev_ticks'] if fair else None
            decision = dict(hold=True, reason=None)
            if c['first_fill'] is not None:
                tick = book['tick'] if book else float(c['plan']['entry']) - float(c['plan']['stop'])
                decision = rule.hold(cfg, dev=dev, bid=book['bid'] if book else None, entry=float(c['plan']['entry']), tick=tick, age_s=clock[0] - c['first_fill'])
            decision.update(cancel_entry=rule.cancel_entry(cfg, dev=dev), dev_ticks=dev)
            portfolio.book(coin).drive(bid=book['bid'] if book else None, fresh=bool(book), quantitative_decision=decision, stopping=t > end_t)
        portfolio.state['capital_at'] = clock[0]
        if t > end_t or t // 1000 == last_decision:
            continue
        last_decision = t // 1000
        ranked = []
        for coin in coins:
            if coin in portfolio.campaigns or coin not in micros:
                continue
            book = live_book(coin, t)
            snap = micros[coin].snapshot(t, book['tick']) if book else None
            if not snap:
                decisions['stale_book'] += 1
                continue
            fair = last_fair.get(coin)
            result = rule.assess(cfg, coin=coin, bid=snap['bid'], ask=snap['ask'], tick=snap['tick'], dev=fair['dev_ticks'] if fair else None,
                                 contract=contracts[coin], units=ladders[coin], cash=D(portfolio.state['cash_krw']) - portfolio.reserved_cash(),
                                 risk_remaining=portfolio.remaining_risk())
            decisions[result['reason']] += 1
            if result['accepted']:
                plan = dict(result['plan'], fair=fair)
                ranked.append((result['best']['score'], coin, plan, snap))
        for _, coin, plan, snap in sorted(ranked, reverse=True, key=lambda r: r[0]):
            portfolio.enter(coin, plan, snap['features'], contracts[coin]['min_order_amount'])
    closed = [(t, b['campaign'], b.get('residual')) for t, k, b in store.events if k == 'CLOSE']
    attempts = sum(k == 'CAMPAIGN_INTENT' for _, k, _ in store.events)
    outcomes = []
    for t, c, residual in closed:
        notional = float(c['plan']['qty']) * float(c['plan']['entry'])
        outcomes.append(dict(t=t, day=(t + KST_MS) // 86400000, coin=c['coin'], net_krw=float(c['net']), net_bp=float(c['net']) / notional * 1e4,
                             reason=c['exit_reason'], bought=float(c['bought']), sold=float(c['sold']), residual=residual, dev=c['plan'].get('dev_ticks')))
    days = defaultdict(list)
    for o in outcomes:
        days[o['day']].append(o['net_bp'])
    by_coin = defaultdict(list)
    for o in outcomes:
        by_coin[o['coin']].append(o['net_bp'])
    bps = [o['net_bp'] for o in outcomes]
    return dict(control=control or 'none', rule=rule.VERSION, params={k: cfg[k] for k in rule.PARAMS}, coins=coins, start=events[0][0], end=events[-1][0],
                attempts=attempts, campaigns=len(outcomes), fill_per_attempt=len(outcomes) / attempts if attempts else None,
                mean_bp=statistics.mean(bps) if bps else None, p_positive=sum(b > 0 for b in bps) / len(bps) if bps else None,
                sum_krw=sum(o['net_krw'] for o in outcomes), reasons=dict(Counter(o['reason'] for o in outcomes)),
                days={str(d): dict(n=len(v), mean_bp=statistics.mean(v)) for d, v in sorted(days.items())},
                by_coin={c: dict(n=len(v), mean_bp=statistics.mean(v), p_positive=sum(b > 0 for b in v) / len(v)) for c, v in by_coin.items()},
                decisions=dict(decisions), residuals=portfolio.state['residuals'], halt=portfolio.state['halt'],
                realized_krw=float(portfolio.state['realized']), outcomes=outcomes,
                limitations=['public queue counterfactual, not exchange fills', 'leader receive times from a separate process; live latency differs',
                             'campaign counts are distinct fill events; overlapping 1 s attempts are not independent'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--contracts', required=True)
    p.add_argument('--coinone', nargs='+', required=True)
    p.add_argument('--leaders', nargs='+', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--control', choices=['flip', 'unconditional'])
    p.add_argument('--latency-ms', type=int, default=250)
    args = p.parse_args()
    result = run(load(args.config), args.contracts, args.coinone, args.leaders, control=args.control, latency_ms=args.latency_ms)
    Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('outcomes',)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
