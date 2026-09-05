"""Separate acquisition coverage from model-based trading eligibility."""
import math

ASSET_POLICY_VERSION='nonpegged-20260905-v1'
PEGGED=frozenset('USDT USDC RLUSD DAI USDS USDE SUSDE TUSD FDUSD PYUSD USDD USD1 USDG USDP GUSD FRAX LUSD EURC EURT EURS USDF USD0 USD0++ USDR USDX USDJ CUSD USDB DOLA MIM SUSD CRVUSD USDT0 AUSDT UST USTC'.split())


def asset_reason(coin):
    if coin in PEGGED: return 'pegged_asset'
    if not coin.isalnum() or coin.upper()!=coin: return 'invalid_asset'
    return None


def coverage(contracts, tickers, *, existing=(), foreign=(), benchmarks=('BTC','ETH'), limit=20):
    eligible={r['target_currency']:r for r in contracts if r.get('trade_status')==1 and r.get('maintenance_status')==0
              and {'limit','market','stop_limit'}<=set(r.get('order_types',[]))}
    reasons={}; ranked=[]
    for row in tickers:
        coin=row['target_currency']
        why=asset_reason(coin) or ('external_ownership' if coin in foreign else None) or ('contract_unavailable' if coin not in eligible else None)
        if why: reasons[coin]=why; continue
        volume=float(row.get('quote_volume') or 0)
        if not math.isfinite(volume) or volume<=0: reasons[coin]='no_turnover'; continue
        ranked.append((volume,coin))
    ranked.sort(reverse=True)
    admitted={coin for _,coin in ranked}
    # Benchmarks get observation coverage, but no exemption from execution economics.
    desired=[]
    for coin in list(existing)+list(benchmarks)+[coin for _,coin in ranked]:
        if coin in admitted and coin not in desired and len(desired)<limit: desired.append(coin)
    for _,coin in ranked:
        if coin not in desired: reasons[coin]='outside_acquisition_budget'
    return desired,reasons
