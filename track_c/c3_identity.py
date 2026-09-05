"""Shared source/settings identity for prospective C3 registrations and replays."""
import hashlib
from pathlib import Path
from .rule import PARAMS
from .exit_model import DEFAULTS

SOURCE_NAMES = (
    'c3_replay.py','replay_stream.py','c3_runner.py','c3_evidence.py','c3_identity.py',
    'simulation.py','oms.py','portfolio.py','accounting.py','fair.py','rule.py','exit_model.py',
    'microstructure.py','settings.py','sizing.py','dataset.py','leaders.py','outcomes.py',
    'coinone.py','execution.py','store.py','rate_limit.py','marketdata.py','private_stream.py',
    'runner.py','quant_runner.py','universe.py','http_pool.py','recovery.py','service_health.py',
    'storage.py','c3_observations.py','c3_live_evidence.py','requirements.txt','../bot/signal.py','../bot/risk.py')
CONFIG_KEYS = PARAMS + ('policy','coins','leader_price','leader_weights','ratio_window_s','ratio_min_samples',
    'leader_max_age_ms','liveness_ms','decision_ms','quote_max_age_ms','risk_fraction','daily_loss_fraction','cash_fraction',
    'http_timeout_s','public_storage_max_bytes','record_coins','reconcile_poll_s','scan_seconds')
EXECUTION_VERSION = 'c3-execution-v1'


def source_hashes(root=None):
    root=Path(root) if root is not None else Path(__file__).parent
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in SOURCE_NAMES}


def config_identity(cfg):
    normalized={**DEFAULTS, 'http_timeout_s':3, 'reconcile_poll_s':1.0, **cfg}
    return {k:normalized.get(k) for k in CONFIG_KEYS}
