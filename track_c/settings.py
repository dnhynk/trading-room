import json
from pathlib import Path
from bot.signal import SIG
from .coinone import decimal, symbol


def load(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if cfg.get("version") != 1 or cfg.get("mode") not in ("observe", "live"):
        raise ValueError("invalid Track C configuration")
    if cfg.get("capital_mode") != "account_equity" or "capital_krw" in cfg or "other_strategy_reserve_krw" in cfg:
        raise ValueError("Track C capital must follow the entire account balance")
    for key in ("risk_fraction", "daily_loss_fraction", "cash_fraction", "depth_fraction", "volume_fraction", "depth_ticks", "stop_atr", "max_cost_atr"):
        decimal(cfg[key], positive=True)
    if not 0 < decimal(cfg["risk_fraction"]) <= decimal(cfg["daily_loss_fraction"]) <= 1:
        raise ValueError("invalid loss budget")
    if any(decimal(cfg[k]) > 1 for k in ("cash_fraction", "depth_fraction", "volume_fraction")):
        raise ValueError("invalid participation/cash fraction")
    if type(cfg["funding_confirmed"]) is not bool:
        raise ValueError("funding status must be explicit")
    if cfg["mode"] == "live" and not cfg["funding_confirmed"]:
        raise ValueError("live funding allocation is unresolved")
    if not 2 <= cfg["max_symbols"] <= 20 or not 250 <= cfg["quote_max_age_ms"] <= 5000:
        raise ValueError("invalid universe/freshness settings")
    for coin in cfg["excluded_symbols"] + cfg["benchmark_symbols"]:
        symbol(coin)
    if set(cfg["signal"]) - set(SIG):
        raise ValueError("unknown signal parameter")
    if cfg.get('policy')=='quantitative':
        if type(cfg.get('learning_enabled')) is not bool or not cfg.get('model_path'):
            raise ValueError('quantitative model/learning settings required')
        if not .2<=cfg.get('decision_seconds',0)<=10 or not 600<=cfg.get('model_max_age_seconds',0)<=86400:
            raise ValueError('invalid quantitative cadence/model age')
    if cfg.get('policy')=='rule':
        coins=cfg.get('coins')
        if not isinstance(coins,list) or not coins or len(set(coins))!=len(coins):
            raise ValueError('rule policy requires an explicit coin list')
        for coin in coins+list(cfg.get('record_coins',[])):
            symbol(coin)
        for key in ('entry_ticks','cancel_ticks','defend_ticks'):
            if not -10<=float(cfg[key])<=10:
                raise ValueError('invalid fair-value threshold')
        if not (1<=int(cfg['stop_ticks'])<=20 and 10<=int(cfg['hold_s'])<=300 and 1<=int(cfg['target_ticks'])<=5 and 1<=float(cfg['max_spread_ticks'])<=10):
            raise ValueError('invalid rule exit/target settings')
        if not (5000<=float(cfg['notional_krw'])<=10_000_000 and 1<=int(cfg['entry_ttl_s'])<=60):
            raise ValueError('invalid rule size/TTL')
        if not (60<=int(cfg['ratio_window_s'])<=3600 and 10<=int(cfg['ratio_min_samples'])<=int(cfg['ratio_window_s'])):
            raise ValueError('invalid fair-value window')
        if not (1000<=int(cfg['leader_max_age_ms'])<=120000 and 100<=int(cfg['decision_ms'])<=5000 and 5000<=int(cfg['liveness_ms'])<=300000):
            raise ValueError('invalid rule cadence/liveness')
        if cfg.get('leader_price','microprice') not in ('microprice','mid'):
            raise ValueError('invalid leader price kind')
        weights=cfg.get('leader_weights')
        if weights is not None and (not isinstance(weights,dict) or not weights or any(not isinstance(v,(int,float)) or v<0 for v in weights.values()) or sum(weights.values())<=0):
            raise ValueError('invalid leader weights')
    cfg["signal"] = {**SIG, **cfg["signal"]}
    return cfg
