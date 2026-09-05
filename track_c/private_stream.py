"""Authenticated MYORDER wakeups. REST cumulative detail remains authoritative."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid


def headers(credentials):
    raw=dict(access_token=credentials.access_token,nonce=str(uuid.uuid4()),timestamp=int(time.time()*1000))
    payload=base64.b64encode(json.dumps(raw,separators=(',',':')).encode()).decode()
    signature=hmac.new(credentials.secret_key.encode(),payload.encode(),hashlib.sha512).hexdigest()
    return {'X-COINONE-PAYLOAD':payload,'X-COINONE-SIGNATURE':signature}


async def follow(runner):
    from websockets.asyncio.client import connect
    backoff=1
    while not runner.stopping:
        try:
            # Fixed TLS origin. Handshake credentials are never logged or persisted.
            async with connect('wss://stream.coinone.co.kr/v1/private',additional_headers=headers(runner.client._credentials),
                               open_timeout=10,ping_interval=15,ping_timeout=15,close_timeout=3,max_queue=1024) as ws:
                subscribed=set(); last_ping=time.monotonic()
                while not runner.stopping:
                    desired=set(runner.markets)
                    for action,coins in (('UNSUBSCRIBE',subscribed-desired),('SUBSCRIBE',desired-subscribed)):
                        if coins:
                            await ws.send(json.dumps(dict(request_type=action,channel='MYORDER',topic=[dict(quote_currency='KRW',target_currency=c) for c in sorted(coins)])))
                    subscribed=desired
                    if time.monotonic()-last_ping>=15: await ws.send('{"request_type":"PING"}'); last_ping=time.monotonic()
                    try: raw=await asyncio.wait_for(ws.recv(),1)
                    except asyncio.TimeoutError: continue
                    msg=json.loads(raw)
                    if msg.get('response_type')=='ERROR': raise ValueError('private subscription rejected')
                    if msg.get('response_type')=='SUBSCRIBED': runner.private_connected=True
                    if msg.get('response_type')=='DATA' and msg.get('channel')=='MYORDER':
                        row=msg.get('data') or {}; cid=row.get('user_order_id')
                        if cid in runner.oms.state['orders'] or row.get('target_currency') in runner.oms.campaigns:
                            runner.counts['private_order_events']+=1; runner.wakeup.set()
                backoff=1
        except Exception:
            runner.counts['private_ws_errors']+=1
        finally: runner.private_connected=False
        if not runner.stopping: await asyncio.sleep(backoff); backoff=min(30,backoff*2)
