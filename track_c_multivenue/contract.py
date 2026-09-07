"""Frozen, research-only contract for comparing A-2 premises inside Track C.

The package lives outside ``track_c`` deliberately: historical C-BTC model
identities hash every Python file below that directory.  Adding this research
annex must not silently change an already registered BTC artifact.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from track_a_2.execution.preflight import evaluation_config_digest
from track_c.learning.config import validate as c4_validate
from track_c_multivenue import POLICIES, VERSION


COINS = ("ETH", "SOL", "XRP")
VENUES = ("coinone", "upbit", "bithumb")
EXTERNAL_VENUES = ("upbit", "bithumb")
RESEARCH_CASH_KRW = 300_000.0

POLICY_DEFINITIONS = {
    "P0": {
        "name": "a2_deceleration",
        "trigger": "newly_observable_DIP_SLOWING",
        "external_discount_required": False,
    },
    "P1": {
        "name": "a2_deceleration_plus_external",
        "trigger": "newly_observable_DIP_SLOWING",
        "external_discount_required": True,
    },
    "P2": {
        "name": "c_local_shock_plus_external",
        "trigger": "new_C_local_sell_episode",
        "external_discount_required": True,
    },
    "P3": {
        "name": "c_local_shock_plus_external_plus_known_deceleration",
        "trigger": "new_C_local_sell_episode",
        "external_discount_required": True,
        "deceleration_must_be_known_at_decision": True,
    },
}


def digest(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def c_config():
    """Return C4's numerical hypotheses under a separate multi-asset identity."""
    cfg = deepcopy(c4_validate())
    cfg["version"] = VERSION
    cfg["coins"] = list(COINS)
    return cfg


def research_contract(a2_config):
    ttl = a2_config["strategy"]["buy_ttl_s"]
    if type(ttl) is not int or ttl <= 0:
        raise ValueError("invalid A-2 signal lifetime")
    return {
        "schema": 1,
        "version": VERSION,
        "mode": "offline_observation",
        "orders_enabled": False,
        "live_promotion": "forbidden",
        "coins": list(COINS),
        "venues": list(VENUES),
        "policies": deepcopy(POLICY_DEFINITIONS),
        "common_action": {
            "price_offset_ticks": 0,
            "size_mode": "minimum",
            "candidate_id": "0:minimum",
        },
        "comparison_cash_krw": RESEARCH_CASH_KRW,
        "a2_signal": {
            "name": "DIP_SLOWING",
            "known_within_s": ttl,
            "quote_max_age_ms": a2_config["quote_max_age_ms"],
            "parameters": deepcopy(a2_config["signal"]),
            "config_digest": evaluation_config_digest(a2_config),
        },
        "execution": c_config(),
        "session_boundary": {
            "inventory": "censor",
            "market_state": "reset",
            "reference_state": "reset",
            "cross_boundary_labels": "forbidden",
            "reason": "the_current_hourly_recorder_reconnects_all_venues",
        },
        "interpretation": {
            "candidate_attempts": "counterfactual_and_not_additive_portfolio_pnl",
            "fills": "public_queue_replay_not_exchange_fills",
            "approval": "research_only_even_when_metrics_are_positive",
            "frequency_matched_control": "required_before_any_selection_claim",
        },
    }


def source_identity(root=None):
    """Hash only this annex and the exact research dependencies it executes."""
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    paths = list((root / "track_c_multivenue").glob("*.py"))
    paths += [
        root / "pyproject.toml",
        root / "common" / "cycle.py",
        root / "common" / "signal.py",
        root / "track_a_2" / "execution" / "preflight.py",
        root / "track_a_2" / "external.py",
        root / "track_a_2" / "market" / "feed.py",
        root / "track_a_2" / "market" / "units.py",
        root / "track_a_2" / "observe.py",
        root / "track_a_2" / "replay" / "loader.py",
        root / "track_a_2" / "settings.py",
        root / "track_c" / "execution" / "coinone.py",
        root / "track_c" / "execution" / "sizing.py",
        root / "track_c" / "learning" / "config.py",
        root / "track_c" / "learning" / "features.py",
        root / "track_c" / "market" / "exit_settings.py",
        root / "track_c" / "market" / "leaders.py",
        root / "track_c" / "market" / "microstructure.py",
        root / "track_c" / "market" / "prices.py",
        root / "track_c" / "market" / "reference.py",
        root / "track_c" / "market" / "risk.py",
        root / "track_c" / "market" / "state.py",
        root / "track_c" / "replay" / "execution.py",
        root / "track_c" / "replay" / "queue.py",
    ]
    if any(not path.is_file() for path in paths):
        raise OSError("multi-venue research source unavailable")
    result = {}
    for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()):
        body = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        result[path.relative_to(root).as_posix()] = hashlib.sha256(body).hexdigest()
    return result


def validate_contract(value, a2_config):
    expected = research_contract(a2_config)
    if value != expected or tuple(value.get("policies", ())) != POLICIES:
        raise ValueError("invalid multi-venue research contract")
    return deepcopy(value)
