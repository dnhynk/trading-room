"""Replay of the C3 rule over recorded Coinone + leader tapes with the production OMS.

Same FairValue/rule/Portfolio/OMS code as live; the exchange is the conservative
public-queue counterfactual (track_c.simulation.Exchange). Controls: `flip` negates
the fair-value deviation, `unconditional` removes gate/cancel/defend (stop/time only).
Outcomes share market paths and carried inventory; they are not independent samples.
Primary performance is marked wealth, including unsold residuals, on a KST day grid.

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

from .dataset import public_contracts, asof, sha
from .accounting import inventory_rows, residual_value, marked_equity
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


def coinone_rows(paths, quality=None):
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
                    if quality is not None: quality['malformed_coinone_rows'] += 1
                    continue
                if msg.get('response_type') == 'DATA':
                    yield recv, msg


def run(cfg, contracts_path, coinone, leaders, *, control=None, latency_ms=250, cash=594574, step_ms=None):
    if control not in (None, 'flip', 'unconditional') or latency_ms < 0 or cash <= 0:
        raise ValueError('invalid replay scenario')
    step_ms = int(step_ms or cfg['decision_ms'])
    if step_ms <= 0: raise ValueError('invalid replay step')
    coinone, leaders = list(coinone), list(leaders)
    if len(set(map(str, coinone))) != len(coinone) or len(set(map(str, leaders))) != len(leaders):
        raise ValueError('duplicate tape paths')
    cfg = dict(cfg, mode='live', funding_confirmed=True)
    if control == 'unconditional':
        cfg.update(entry_ticks=-1e9, cancel_ticks=-1e9, defend_ticks=-1e9)
    contracts, units = public_contracts(contracts_path)
    coins = [c for c in cfg['coins'] if c in contracts]
    events = []  # (t, order, kind, payload)
    quality = Counter()
    warm = {}
    for recv, msg in sorted(coinone_rows(coinone, quality), key=lambda r: r[0]):
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
            elif row[0] == 's':
                events.append((row[1], 1, 'connection', row))
                quality['recorded_connection_events'] += 1
    events.sort(key=lambda e: (e[0], e[1]))
    if not events:
        raise ValueError('no replayable events')
    tapes = {c: Tape([e[3][3] for e in events if e[2] == 'coinone' and e[3][0] == c], stale_ms=cfg['liveness_ms']) for c in coins}
    tapes = {c: t for c, t in tapes.items() if t.events}
    for c, micro in warm.items():
        for key, value in micro.quality.items(): quality['coinone_'+key] += value
    last_coinone = max((e[0] for e in events if e[2] == 'coinone'), default=0)
    last_leader = max((e[0] for e in events if e[2] == 'leader'), default=0)
    end = min(last_coinone, last_leader)
    if end <= events[0][0]: raise ValueError('no overlapping venue coverage')
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
    last_decision = -1
    curve = []
    peak = float(cash)
    drawdown = 0.

    def metadata(coin, now):
        contract, ladder = asof(contracts, coin, now), asof(units, coin, now)
        if not contract: return None, None
        ladder = ladder['rows'] if ladder else [dict(range_min=0, price_unit=contract['price_unit'])] if contract.get('price_unit') else None
        return contract, ladder

    def live_book(coin, now):
        micro = micros.get(coin)
        if not micro or not micro.bids or not 0 <= now - micro.book_ms <= cfg['liveness_ms']:
            return None
        _, ladder = metadata(coin, now)
        if not ladder: return None
        bid, ask = micro.bids[0][0], micro.asks[0][0]
        return dict(bid=bid, ask=ask, tick=float(price_unit(ladder, D(str(bid)))))

    start_t = (events[0][0] // step_ms + 1) * step_ms
    # No new orders near the boundary; existing inventory still receives its full
    # entry/holding horizon. Only the final drain uses operator_stop.
    drain_ms = max(5000, 4*latency_ms + 4*step_ms)
    drain_t = end - drain_ms
    entry_end = drain_t - (cfg['hold_s'] + cfg['entry_ttl_s']) * 1000
    for t in range(start_t, end + 1, step_ms):
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
            elif kind == 'leader':
                fairs[payload[3]].leader_quote(payload[2], payload[1], payload[5], payload[7], payload[6], payload[8])
            elif payload[4] == 'disconnected':
                for fair in fairs.values(): fair.disconnect(payload[2])
        client.settle(t)
        for coin in coins:
            book = live_book(coin, t)
            last_fair[coin] = fairs[coin].evaluate(t, (book['bid'] + book['ask']) / 2, book['tick']) if book else None
        portfolio.mark_residuals({c: b['bid'] for c in portfolio.state['residuals'] if (b := live_book(c, t))})
        for coin in list(portfolio.campaigns):
            c = portfolio.campaigns[coin]
            book = live_book(coin, t)
            fair = last_fair.get(coin)
            dev = fair['dev_ticks'] if fair else None
            decision = rule.hold(cfg, dev=dev, bid=book['bid'] if book else None, entry=float(c['plan']['entry']),
                                 tick=float(c['plan'].get('tick', 1)), stop=c['stop'],
                                 age_s=clock[0] - c['first_fill'] if c['first_fill'] is not None else None)
            decision.update(cancel_entry=rule.cancel_entry(cfg, dev=dev), dev_ticks=dev)
            portfolio.book(coin).drive(bid=book['bid'] if book else None, fresh=bool(book), quantitative_decision=decision, stopping=t > drain_t)
            if not book: quality['holding_without_fresh_book_steps'] += 1
        wealth = float(marked_equity(portfolio.state))
        peak = max(peak, wealth)
        drawdown = max(drawdown, peak - wealth)
        curve.append((t, wealth))
        portfolio.state['capital_at'] = clock[0]
        if t > entry_end or t // cfg['decision_ms'] == last_decision:
            continue
        last_decision = t // cfg['decision_ms']
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
            contract, ladder = metadata(coin, t)
            if not contract or not ladder:
                decisions['metadata_unavailable'] += 1
                continue
            result = rule.assess(cfg, coin=coin, bid=snap['bid'], ask=snap['ask'], tick=snap['tick'], dev=fair['dev_ticks'] if fair else None,
                                 contract=contract, units=ladder, cash=D(portfolio.state['cash_krw']) - portfolio.reserved_cash(),
                                 risk_remaining=portfolio.remaining_risk(coin))
            decisions[result['reason']] += 1
            if result['accepted']:
                plan = dict(result['plan'], fair=fair)
                ranked.append((result['best']['score'], coin, plan, snap, contract))
        for _, coin, plan, snap, contract in sorted(ranked, reverse=True, key=lambda r: r[0]):
            portfolio.enter(coin, plan, snap['features'], contract['min_order_amount'])
    closed = [(t, b['campaign'], b.get('residual')) for t, k, b in store.events if k == 'CLOSE']
    attempts = sum(k == 'CAMPAIGN_INTENT' for _, k, _ in store.events)
    outcomes = []
    bought_gross = defaultdict(float)
    turnover = 0.
    for _, kind, body in store.events:
        if kind == 'FILL':
            turnover += float(body['gross'])
            if body['role'] == 'entry': bought_gross[body.get('campaign_id')] += float(body['gross'])
    for t, c, residual in closed:
        notional = bought_gross[c['id']] + float((c.get('residual') or {}).get('cost', 0))
        if not notional: continue
        outcomes.append(dict(t=t, id=c['id'], day=(t + KST_MS) // 86400000, coin=c['coin'], net_krw=float(c['net']), net_bp=float(c['net']) / notional * 1e4,
                             inventory_flat=not bool(float(c['qty'])), capital_involved_krw=notional, first_fill=c['first_fill'],
                             reason=c['exit_reason'], bought=float(c['bought']), sold=float(c['sold']), residual=residual, dev=c['plan'].get('dev_ticks')))
    days = defaultdict(list)
    for o in outcomes:
        days[o['day']].append(o['net_bp'])
    by_coin = defaultdict(list)
    for o in outcomes:
        by_coin[o['coin']].append(o['net_bp'])
    bps = [o['net_bp'] for o in outcomes]
    marked_net = float(marked_equity(portfolio.state)) - float(cash)
    unrealized = sum(float(residual_value(r)-D(r['cost'])) for r in inventory_rows(portfolio.state))
    identity_error = marked_net - float(portfolio.state['realized']) - unrealized
    if abs(identity_error) > 1e-6: raise AssertionError('wealth / PnL identity failed')
    daily = {}
    previous_t, previous_w = events[0][0], float(cash)
    for t, wealth in curve:
        day = str((t + KST_MS) // 86400000)
        row = daily.setdefault(day, dict(first_t=previous_t, last_t=t, net_krw=0., opening_equity_krw=previous_w))
        row['last_t'] = t
        row['net_krw'] += wealth - previous_w
        previous_t, previous_w = t, wealth
    for key, row in daily.items():
        boundary = int(key)*86400000-KST_MS
        row['complete'] = row['first_t'] <= boundary and row['last_t'] >= boundary+86400000-step_ms
    root = Path(__file__).parent
    # Freeze trading/measurement dependencies; notification-only changes do not
    # reset an experiment because they cannot affect decisions or execution.
    source_names = ('c3_replay.py','c3_runner.py','c3_evidence.py','simulation.py','oms.py','portfolio.py','accounting.py','fair.py','rule.py',
                    'microstructure.py','settings.py','sizing.py','dataset.py','leaders.py','outcomes.py','coinone.py','execution.py','store.py',
                    'rate_limit.py','marketdata.py','private_stream.py','runner.py','quant_runner.py','universe.py','requirements.txt',
                    '../bot/signal.py','../bot/risk.py')
    return dict(schema=2, control=control or 'none', rule=rule.VERSION, params={k: cfg[k] for k in rule.PARAMS}, coins=coins, start=events[0][0], end=end,
                scenario=dict(latency_ms=latency_ms, step_ms=step_ms, initial_cash_krw=float(cash)),
                identity=dict(source={n:sha(root/n) for n in source_names}, data={str(p):sha(p) for p in coinone+leaders+[contracts_path]},
                              config={k:cfg.get(k) for k in rule.PARAMS+('policy','coins','leader_price','leader_weights','ratio_window_s','ratio_min_samples','leader_max_age_ms','liveness_ms','decision_ms','quote_max_age_ms','risk_fraction','daily_loss_fraction','cash_fraction')}),
                attempts=attempts, campaigns=len(outcomes), fill_per_attempt=len(outcomes) / attempts if attempts else None,
                mean_bp=statistics.mean(bps) if bps else None, p_positive=sum(b > 0 for b in bps) / len(bps) if bps else None,
                sum_krw=sum(o['net_krw'] for o in outcomes), reasons=dict(Counter(o['reason'] for o in outcomes)),
                days={str(d): dict(n=len(v), mean_bp=statistics.mean(v)) for d, v in sorted(days.items())},
                by_coin={c: dict(n=len(v), mean_bp=statistics.mean(v), p_positive=sum(b > 0 for b in v) / len(v)) for c, v in by_coin.items()},
                decisions=dict(decisions), residuals=portfolio.state['residuals'], halt=portfolio.state['halt'],
                realized_krw=float(portfolio.state['realized']), outcomes=outcomes,
                marked_net_krw=marked_net, terminal_equity_krw=float(marked_equity(portfolio.state)), unrealized_krw=unrealized,
                cash_krw=float(portfolio.state['cash_krw']), turnover_krw=turnover, actual_buys_krw=sum(bought_gross.values()),
                net_per_hour_krw=marked_net/((end-events[0][0])/3600000), max_drawdown_krw=drawdown,
                max_drawdown_fraction=drawdown/float(cash), accounting_error_krw=identity_error,
                daily_wealth=daily, quality=dict(quality), open_campaigns=deepcopy(portfolio.campaigns),
                boundary=dict(entry_end=entry_end, liquidation_start=drain_t),
                stress_krw={str(bp):marked_net-turnover*bp/10000 for bp in (0, .5, 1, 2)},
                admission='HOLD: diagnostic replay; prospective daily evidence required',
                limitations=['public queue counterfactual, not exchange fills', 'leader receive times from a separate process; live latency differs',
                             'queue at own price; trade-through consumes reported quantity only; no hidden queue or self-impact identification',
                             'campaigns share market paths and residuals; not independent observations',
                             'cost stress is post-trade attribution, not fee-aware policy simulation',
                             'residual marks are not executable liquidation below exchange minimum',
                             'no recorded connection events does not establish uninterrupted coverage'])


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
    print(json.dumps({k: result[k] for k in ('rule','control','attempts','campaigns','marked_net_krw','max_drawdown_krw','stress_krw','residuals','halt','admission')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
