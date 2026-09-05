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
from .portfolio import Portfolio
from . import rule
from .settings import load
from .exit_model import validate_exit_settings
from .c3_identity import config_identity, source_hashes, EXECUTION_VERSION
from .replay_stream import EventSpool, BookWindow, ReplayExchange, OutcomeStore, WealthSummary
from .sizing import price_unit

KST_MS = 9 * 3600000


def evaluation_blocks(curve, start_ms, end_ms):
    """Fixed 24-hour wealth blocks anchored at activation, excluding earlier PnL."""
    if start_ms is None or end_ms is None or not curve:
        return {}
    times = [t for t,_ in curve]
    result = {}
    for i,start in enumerate(range(start_ms, end_ms, 86400000)):
        end = min(start+86400000, end_ms)
        left, right = bisect_right(times,start)-1, bisect_right(times,end)-1
        complete = left >= 0 and right >= 0 and times[-1] >= end
        result[str(i)] = dict(start_ms=start,end_ms=end,complete=complete,
                             net_krw=curve[right][1]-curve[left][1] if complete else None)
    return result


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


def run(cfg, contracts_path, coinone, leaders, *, control=None, latency_ms=250, cash=594574, step_ms=None,
        evaluation_start_ms=None, evaluation_end_ms=None):
    if control not in (None, 'flip', 'unconditional') or latency_ms < 0 or cash <= 0:
        raise ValueError('invalid replay scenario')
    step_ms = int(step_ms or cfg['decision_ms'])
    if step_ms <= 0: raise ValueError('invalid replay step')
    if (evaluation_start_ms is None) != (evaluation_end_ms is None) or (evaluation_start_ms is not None and evaluation_start_ms >= evaluation_end_ms):
        raise ValueError('evaluation start/end must be a valid explicit pair')
    coinone, leaders = list(coinone), list(leaders)
    if len(set(map(str, coinone))) != len(coinone) or len(set(map(str, leaders))) != len(leaders):
        raise ValueError('duplicate tape paths')
    data_hashes = {str(p): sha(p) for p in coinone+leaders+[contracts_path]}
    code_hashes = source_hashes(Path(__file__).parent)
    cfg = dict(cfg, **validate_exit_settings(cfg), mode='live', funding_confirmed=True)
    if control == 'unconditional':
        cfg.update(entry_ticks=-1e9, cancel_ticks=-1e9, defend_ticks=-1e9)
    contracts, units = public_contracts(contracts_path)
    coins = [c for c in cfg['coins'] if c in contracts]
    quality = Counter()
    leader_stream = (row for path in leaders for row in leader_rows(path))
    with EventSpool(coinone_rows(coinone, quality), leader_stream, coins, cfg['quote_max_age_ms'], quality) as spool:
        return _stream(cfg, contracts_path, coinone, leaders, control, latency_ms, cash, step_ms,
                       evaluation_start_ms, evaluation_end_ms, contracts, units, coins, quality, spool, data_hashes, code_hashes)


def _stream(cfg, contracts_path, coinone, leaders, control, latency_ms, cash, step_ms,
            evaluation_start_ms, evaluation_end_ms, contracts, units, coins, quality, spool, data_hashes, code_hashes):
    start, end = spool.start, spool.end
    tapes = {c: BookWindow(cfg['liveness_ms']) for c in coins}
    clock = [start / 1000]
    store = OutcomeStore(lambda: clock[0])
    client = ReplayExchange(tapes, lambda: clock[0], latency_ms, cash=cash)
    portfolio = Portfolio(cfg, client, store, clock=lambda: clock[0])
    portfolio.sync_cash(client.cash)
    fairs = {c: FairValue(window_s=cfg['ratio_window_s'], min_samples=cfg['ratio_min_samples'], leader_max_age_ms=cfg['leader_max_age_ms'],
                          flip=(control == 'flip'), weights=cfg.get('leader_weights'), price=cfg.get('leader_price', 'microprice'),
                          momentum_window_s=cfg['momentum_window_s'], momentum_recent_s=cfg['momentum_recent_s'], exit_config=cfg) for c in coins}
    micros = {}
    last_fair = {}
    decisions = Counter()
    events = iter(spool)
    upcoming = next(events, None)
    last_event = start
    last_decision = -1
    wealth_summary = WealthSummary(start, float(cash), step_ms, evaluation_start_ms, evaluation_end_ms)
    model_counts = Counter()
    last_value_second = {}
    max_step_events = 0

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

    start_t = (start // step_ms + 1) * step_ms
    # No new orders near the boundary; existing inventory still receives its full
    # entry/holding horizon. Only the final drain uses operator_stop.
    drain_ms = max(5000, 4*latency_ms + 4*step_ms)
    drain_t = end - drain_ms
    if evaluation_end_ms is not None:
        drain_t = min(drain_t, evaluation_end_ms-drain_ms)
    entry_end = drain_t - (cfg['hold_s'] + cfg['entry_ttl_s']) * 1000
    for t in range(start_t, end + 1, step_ms):
        clock[0] = t / 1000
        batch = []
        while upcoming is not None and upcoming[0] <= t:
            batch.append(upcoming)
            if upcoming[2] == 'coinone':
                tapes[upcoming[3][0]].append(upcoming[3][3])
            upcoming = next(events, None)
        max_step_events = max(max_step_events, len(batch))
        # Preloading every book in this step preserves original as-of arrivals,
        # including all books tied at the same timestamp as an earlier trade.
        for at, _, kind, payload in batch:
            if at - last_event > 120000:
                micros = {}
            last_event = at
            if kind == 'coinone':
                coin, channel, data, event = payload
                if coin in tapes:
                    client.event(coin, event)
                micros.setdefault(coin, Micro(coin, stale_ms=cfg['quote_max_age_ms'], legacy_features=False)).feed(channel, data, at)
            elif kind == 'leader':
                fairs[payload[3]].leader_quote(payload[2], payload[1], payload[5], payload[7], payload[6], payload[8])
            elif payload[4] == 'disconnected':
                for fair in fairs.values(): fair.disconnect(payload[2])
        client.settle(t)
        for coin in coins:
            book = live_book(coin, t)
            last_fair[coin] = fairs[coin].evaluate(t, (book['bid'] + book['ask']) / 2, book['tick'], bid=book['bid'], ask=book['ask']) if book else None
            model_counts['fair_steps'] += bool(last_fair[coin])
            model_counts['risk_ready_steps'] += bool((last_fair[coin] or {}).get('risk', {}).get('ready'))
        portfolio.mark_residuals({c: b['bid'] for c in portfolio.state['residuals'] if (b := live_book(c, t))})
        for coin in list(portfolio.campaigns):
            c = portfolio.campaigns[coin]
            book = live_book(coin, t)
            fair = last_fair.get(coin)
            dev = fair['dev_ticks'] if fair else None
            snap = micros[coin].snapshot(t, book['tick']) if book and coin in micros else None
            motion = dict(m30=(fair or {}).get('m30'), m10=(fair or {}).get('m10'),
                          flow32=snap['features'].get('flow_32') if snap else None, unconditional=(control == 'unconditional'))
            age_s = clock[0] - c['first_fill'] if c['first_fill'] is not None else None
            hold_args = dict(dev=dev, bid=book['bid'] if book else None, entry=float(c['plan']['entry']),
                             tick=float(c['plan'].get('tick', 1)), stop=c['stop'], age_s=age_s, **motion)
            decision = rule.hold(cfg, **hold_args)
            # Evaluate immediate protection first; optional value work cannot
            # delay or override stop/brake/defend/time and needs an actual fill.
            if (decision['hold'] and book and age_s is not None and cfg['value_exit'] and control != 'unconditional'
                    and last_value_second.get(coin) != (c['id'], t//1000)):
                last_value_second[coin] = (c['id'], t//1000)
                continuation = fairs[coin].continuation(t, bid=book['bid'], ask=book['ask'], tick=book['tick'],
                    stop=float(c['stop']), stop_limit=float(c['stop_limit']), take=float(c['plan']['take_profit']), age_s=age_s)
                model_counts['value_queries'] += 1
                model_counts['value_ready_queries'] += bool(continuation['ready'])
                model_counts['value_exits'] += bool(continuation['exit'])
                decision = rule.hold(cfg, **hold_args, continuation=continuation)
            decision.update(cancel_entry=rule.cancel_entry(cfg, dev=dev, risk=(fair or {}).get('risk'), **motion), dev_ticks=dev)
            portfolio.book(coin).drive(bid=book['bid'] if book else None, fresh=bool(book), quantitative_decision=decision, stopping=t > drain_t)
            if not book: quality['holding_without_fresh_book_steps'] += 1
        wealth = float(marked_equity(portfolio.state))
        wealth_summary.observe(t, wealth)
        client.prune(portfolio.state['orders'])
        for tape in tapes.values(): tape.trim(t)
        portfolio.state['capital_at'] = clock[0]
        if (evaluation_start_ms is not None and t < evaluation_start_ms) or t > entry_end or t // cfg['decision_ms'] == last_decision:
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
                                 risk_remaining=portfolio.remaining_risk(coin), flow32=snap['features'].get('flow_32'),
                                 m30=(fair or {}).get('m30'), m10=(fair or {}).get('m10'), risk=(fair or {}).get('risk'), unconditional=(control == 'unconditional'))
            decisions[result['reason']] += 1
            if result['accepted']:
                plan = dict(result['plan'], fair=fair)
                ranked.append((result['best']['score'], coin, plan, snap, contract))
        for _, coin, plan, snap, contract in sorted(ranked, reverse=True, key=lambda r: r[0]):
            portfolio.enter(coin, plan, snap['features'], contract['min_order_amount'])
    outcomes = store.outcomes
    attempts, turnover = store.attempts, store.turnover
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
    daily, blocks = wealth_summary.finish()
    drawdown = wealth_summary.drawdown
    if any(sha(p) != digest for p,digest in data_hashes.items()):
        raise ValueError('source tape changed during replay')
    if source_hashes(Path(__file__).parent) != code_hashes:
        raise ValueError('source code changed during replay')
    return dict(schema=2, control=control or 'none', rule=rule.VERSION, execution_version=EXECUTION_VERSION,
                params={k: cfg[k] for k in rule.PARAMS}, coins=coins, start=start, end=end,
                scenario=dict(latency_ms=latency_ms, step_ms=step_ms, initial_cash_krw=float(cash)),
                identity=dict(source=code_hashes, data=data_hashes, config=config_identity(cfg)),
                attempts=attempts, campaigns=len(outcomes), fill_per_attempt=len(outcomes) / attempts if attempts else None,
                mean_bp=statistics.mean(bps) if bps else None, p_positive=sum(b > 0 for b in bps) / len(bps) if bps else None,
                sum_krw=sum(o['net_krw'] for o in outcomes), reasons=dict(Counter(o['reason'] for o in outcomes)),
                days={str(d): dict(n=len(v), mean_bp=statistics.mean(v)) for d, v in sorted(days.items())},
                by_coin={c: dict(n=len(v), mean_bp=statistics.mean(v), p_positive=sum(b > 0 for b in v) / len(v)) for c, v in by_coin.items()},
                decisions=dict(decisions), residuals=portfolio.state['residuals'], halt=portfolio.state['halt'],
                realized_krw=float(portfolio.state['realized']), outcomes=outcomes,
                marked_net_krw=marked_net, terminal_equity_krw=float(marked_equity(portfolio.state)), unrealized_krw=unrealized,
                cash_krw=float(portfolio.state['cash_krw']), turnover_krw=turnover, actual_buys_krw=store.actual_buys,
                net_per_hour_krw=marked_net/((end-start)/3600000), max_drawdown_krw=drawdown,
                max_drawdown_fraction=drawdown/float(cash), accounting_error_krw=identity_error,
                daily_wealth=daily, quality=dict(quality), open_campaigns=deepcopy(portfolio.campaigns),
                evaluation_window=dict(start_ms=evaluation_start_ms,end_ms=evaluation_end_ms),
                evaluation_blocks=blocks, model_coverage=dict(model_counts), no_fill=store.counts['NO_FILL'],
                streaming=dict(spooled_events=spool.count,temporary_disk_bytes=spool.bytes,max_step_events=max_step_events,
                               max_book_window=max((p.max_books for p in tapes.values()),default=0),retained_exchange_orders=len(client.orders),
                               retained_outcomes=len(outcomes),retained_market_curve=0),
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
    p.add_argument('--start-ms', type=int, help='registered activation timestamp; earlier tape is warmup only')
    p.add_argument('--end-ms', type=int, help='fixed evaluation end, exclusive')
    args = p.parse_args()
    result = run(load(args.config), args.contracts, args.coinone, args.leaders, control=args.control, latency_ms=args.latency_ms,
                 evaluation_start_ms=args.start_ms, evaluation_end_ms=args.end_ms)
    Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('rule','control','attempts','campaigns','marked_net_krw','max_drawdown_krw','stress_krw','residuals','halt','admission')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
