"""Causal contract snapshots and input hashes for replay."""
import gzip
import hashlib
import json
from pathlib import Path

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def public_contracts(path):
    records=json.loads(Path(path).read_text(encoding='utf-8'))
    contracts={}; units={}
    if isinstance(records,dict) and 'captures' in records:
        for r in sorted(records['captures'],key=lambda r:r['available_ms']):
            coin=r['coin']; at=r['available_ms']; contract=dict(r['contract'],available_ms=at)
            ladder=dict(available_ms=at,rows=r['units'])
            if coin not in contracts: contracts[coin]=dict(contract,history=[]); units[coin]=dict(ladder,history=[])
            contracts[coin]['history'].append(contract); units[coin]['history'].append(ladder)
        return contracts,units
    for row in records:
        response=row['response']; available=row['available_ms']
        if 'markets' in response:
            contracts.update({m['target_currency']:dict(m,available_ms=available) for m in response['markets']})
        if 'range_price_units' in response:
            coin=row['url'].split('?')[0].rsplit('/',1)[-1]
            units[coin]=dict(available_ms=available,rows=response['range_price_units'])
    return contracts,units

def asof(mapping,coin,t):
    row=mapping.get(coin)
    if not row: return None
    history=row.get('history',[row]); candidates=[r for r in history if r['available_ms']<=t]
    return candidates[-1] if candidates else None

def rows(path):
    with gzip.open(path,'rt',encoding='utf-8') as stream:
        return [json.loads(line) for line in stream]
