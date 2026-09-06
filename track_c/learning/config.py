"""Frozen, validated research specification; numerical defaults are hypotheses."""
import hashlib
from copy import deepcopy
import json
import math
from pathlib import Path

DEFAULTS = dict(
    version='c4-liquidity-v1', coins=['BTC'], notional_krw=20000., entry_ticks=2.,
    basis_window_s=1800, basis_min_samples=60, basis_quiet_ticks=1.5,
    basis_break_s=180, leader_max_age_ms=3000, book_max_age_ms=1500, sampling_book_age_ms=3000,
    disagreement_ticks=2., episode_quiet_s=32, episode_max_s=240,
    sell_depth_ratio=1., common_drop_ticks=1., ttl_s=8, hold_s=180,
    latency_ms=250, cancel_latency_ms=500, decision_ms=500,
    depth_haircut=.5, depth_fraction=.1, flow_fraction=.2,
    risk_fraction=.0025, daily_loss_fraction=.015, cash_fraction=.95,
    risk_aversion=.1, capital_cost_bp_hour=1., min_attempts=30, min_fills=10,
    min_blocks=3, block_ms=86400000, alpha=.1, hazard_prior=1.,
    price_offsets=[-1, 0, 1], size_modes=['minimum', 'base'],
    hazard_seconds=[2, 8, 32, 60, 120, 180], fee_bp=0.,
)


def validate(cfg=None):
    cfg = {**deepcopy(DEFAULTS), **(cfg or {})}
    if set(cfg) != set(DEFAULTS) or cfg['version'] != DEFAULTS['version'] or cfg['coins'] != ['BTC']:
        raise ValueError('invalid C4 schema/universe')
    for key, value in DEFAULTS.items():
        if isinstance(value, (int, float)):
            if type(cfg[key]) not in (int, float) or not math.isfinite(cfg[key]):
                raise ValueError('non-finite C4 setting: '+key)
            if cfg[key] < 0 or (value > 0 and cfg[key] == 0):
                raise ValueError('non-positive C4 setting: '+key)
    for key in ('depth_haircut', 'depth_fraction', 'flow_fraction', 'risk_fraction',
                'daily_loss_fraction', 'cash_fraction', 'alpha'):
        if not 0 < cfg[key] < 1: raise ValueError('invalid C4 fraction: '+key)
    if not (cfg['risk_fraction'] <= cfg['daily_loss_fraction'] and cfg['min_blocks'] >= 2
            and 2 <= cfg['basis_min_samples'] <= cfg['basis_window_s']
            and 1 <= cfg['ttl_s'] <= 60 and 10 <= cfg['hold_s'] <= 300
            and cfg['notional_krw'] <= 20000 and cfg['entry_ticks'] >= 2
            and cfg['min_fills'] >= 2 and cfg['min_attempts'] >= cfg['min_fills']):
        raise ValueError('invalid C4 risk/support contract')
    if not (cfg['book_max_age_ms'] <= cfg['sampling_book_age_ms'] <= 5000
            and cfg['latency_ms'] <= 10000 and cfg['cancel_latency_ms'] <= 10000 and cfg['fee_bp'] <= 100):
        raise ValueError('invalid C4 timing/cost stress')
    for key in ('basis_min_samples', 'basis_window_s', 'basis_break_s', 'min_blocks',
                'min_fills', 'min_attempts', 'block_ms', 'decision_ms', 'hold_s', 'ttl_s'):
        if type(cfg[key]) is not int: raise ValueError('integer required: '+key)
    if cfg['price_offsets'] != [-1, 0, 1] or cfg['size_modes'] != ['minimum', 'base']:
        raise ValueError('unregistered action search')
    bins = cfg['hazard_seconds']
    if (not isinstance(bins, list) or not bins or any(type(v) is not int or v <= 0 for v in bins)
            or bins != sorted(set(bins)) or bins[-1] != cfg['hold_s']):
        raise ValueError('invalid hazard intervals')
    return cfg


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def sources():
    # Identity follows shipped code, including the shared exchange feature math.
    # Historical artifacts still require their original source; never accept them
    # under this layout by silently dropping missing dependencies from the hash.
    root = Path(__file__).resolve().parents[2]
    paths = list((root/'track_c').rglob('*.py')) + list((root/'common').glob('*.py'))
    paths += [root/'pyproject.toml']
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


def load(path=None):
    return validate(json.loads(Path(path).read_text(encoding='utf-8')) if path else None)
