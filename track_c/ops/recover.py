"""Post-failure Track C protection under the same exclusive ledger-writer lock.

systemd runs this after the engine exits and before its restart. It never competes
with a live writer or adopts external inventory. Unknown orders retain their slot.
"""
import argparse
from decimal import Decimal as D
import time

from track_c.execution.coinone import CoinoneError, Credentials, decimal
from track_c.execution.client import CoinoneExecution
from track_c.execution.http_pool import HTTPSPool
from track_c.execution.portfolio import Portfolio
from track_c.execution.rate_limit import Transport
from track_c.settings import load
from track_c.ops.store import Store


def protect_once(portfolio):
    """One bounded reconciliation pass. True only when flat or protection confirmed."""
    safe = True
    for coin in list(portfolio.campaigns):
        book = portfolio.book(coin)
        book.reconcile(force=True)
        for order in book.active('entry')+book.active('take'):
            book.cancel(order)
        c = book.campaign
        if book.active('entry') or book.active('take') or book.active('exit'):
            safe = False
            continue
        qty = D(c['qty'])
        if not qty or (c['first_fill'] is None and c.get('residual')):
            book.drive(stopping=True,force_reconcile=True)
            safe = safe and book.campaign is None
            continue
        if book.active('protect'):
            safe = safe and all(o['status'] not in ('INTENT','UNKNOWN','SUBMITTED') for o in book.active('protect'))
            continue
        if qty*D(c['stop_limit']) < D(c['minimum']):
            portfolio.halt('RECOVERY_UNTRADEABLE_PARTIAL')
            safe = False
            continue
        rows=portfolio.client.balances()
        asset=next((r for r in rows if r.get('currency')==coin),None)
        if asset is None or decimal(asset['available']) < qty:
            portfolio.halt('INVENTORY_UNAVAILABLE')
            safe = False
            continue
        public=portfolio.client.orderbook(coin)
        bid=max(decimal(r['price'],positive=True) for r in public['bids'] if decimal(r['qty'])>0)
        expired=c['first_fill'] is not None and portfolio.clock()-c['first_fill']>=float(c['plan']['hold_limit_s'])
        if bid<=D(c['stop']) or expired or c['exit_reason'] or portfolio.state['halt']:
            book.request_exit('recovery_stop' if bid<=D(c['stop']) else 'time' if expired else 'recovery_exit')
            book.drive(bid=bid,fresh=True,force_reconcile=True)
            safe = safe and book.campaign is None
            continue
        c['recovery_protection']=True
        book.save('RECOVERY_PROTECTION_INTENT',qty=str(qty),stop=c['stop'],stop_limit=c['stop_limit'])
        book.submit('protect','SELL','STOP_LIMIT',str(qty),price=c['stop_limit'],trigger_price=c['stop'])
        safe = False  # verify its accepted status on the next pass
    return safe


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--seconds',type=float,default=25)
    args=parser.parse_args()
    cfg=load(args.config)
    if cfg.get('policy')!='rule': raise SystemExit('Track C recovery requires the rule execution schema')
    pool=HTTPSPool(size=1)
    store=Store(cfg['data_directory'])  # exclusive: fails before any network mutation
    client=CoinoneExecution(Credentials.read(cfg['env_path'],profile=cfg['credential_profile']),transport=Transport(pool),timeout=2)
    portfolio=Portfolio(cfg,client,store)
    deadline=time.monotonic()+min(max(args.seconds,1),60)
    try:
        if cfg['mode']!='live' or not cfg['funding_confirmed']:
            if portfolio.campaigns: raise RuntimeError('non-live ledger has C exposure')
            return
        if not portfolio.campaigns: return
        store.event('RECOVERY_STARTED',coins=list(portfolio.campaigns))
        while time.monotonic()<deadline:
            try:
                if protect_once(portfolio):
                    store.event('RECOVERY_READY',coins=list(portfolio.campaigns))
                    return
            except (CoinoneError,ValueError,KeyError):
                store.event('RECOVERY_PENDING',reason='order_or_market_reconciliation')
            time.sleep(.25)
        store.event('RECOVERY_INCOMPLETE',coins=list(portfolio.campaigns))
        raise SystemExit(1)
    finally:
        store.close()
        pool.close()


if __name__=='__main__': main()
