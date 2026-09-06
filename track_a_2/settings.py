"""Fail-closed configuration contract for the Track A-2 live engine."""
import ipaddress
import json
import math
from pathlib import Path
import re

from common.cycle import valid_params
from common.signal import SIG, STRAT


ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(__file__).with_name("config.json")
TOP_FIELDS = {
    "version", "track", "status", "venue", "market", "quote_currency", "sides",
    "mode", "execution_enabled", "portfolio_isolation_required",
    "portfolio_isolation_confirmed", "capital_mode", "live_approval_id",
    "expected_egress_ip", "env_path", "state_directory", "strategy_contract",
    "selection_contract", "universe", "basket_size", "max_open_books",
    "min_quote_volume_24h", "min_range_24h_pct", "max_range_24h_pct",
    "max_spread_bp", "scan_seconds", "decision_ms", "quote_max_age_ms",
    "account_poll_s", "account_fresh_s", "reconcile_poll_s", "reconcile_halt_s",
    "account_mismatch_grace_s", "http_timeout_s", "public_storage_max_bytes",
    "cash_fraction", "unit_fraction", "book_notional_fraction",
    "portfolio_notional_fraction", "book_risk_fraction", "daily_loss_fraction",
    "depth_fraction", "minimum_exit_multiple", "stop_limit_buffer_ticks",
    "stop_limit_buffer_bp", "max_fee_rate", "signal", "strategy",
}
DYNAMIC_STRATEGY = {
    "side", "unit_qty", "max_notional", "cap_usdt", "tick", "qstep",
    "fee_rt_pct", "lever", "margin_mode", "wallet_frac", "wind_down",
    "unit_frac", "cap_frac", "daily_loss_frac", "notional_frac",
    "daily_loss_limit", "hunt",
}
ALLOWED_STRATEGY = set(STRAT) - DYNAMIC_STRATEGY
APPROVAL = re.compile(r"a2-eval-[a-z0-9_.-]{4,80}")


def _number(config, name, *, low=0, high=None, positive=False, integer=False):
    value = config[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"invalid Track A-2 numeric field: {name}")
    if value < low or (positive and value <= 0) or (high is not None and value > high):
        raise ValueError(f"invalid Track A-2 numeric range: {name}")
    if integer and not isinstance(value, int):
        raise ValueError(f"invalid Track A-2 integer field: {name}")
    return value


def resolved_state_directory(config, root=ROOT):
    value = Path(config["state_directory"])
    return (value if value.is_absolute() else Path(root) / value).resolve()


def resolved_env_path(config, root=ROOT):
    value = Path(config["env_path"])
    return (value if value.is_absolute() else Path(root) / value).resolve()


def _validate(config, *, root=ROOT):
    if not isinstance(config, dict) or set(config) != TOP_FIELDS:
        raise ValueError("invalid Track A-2 configuration fields")
    identity = (
        config["version"], config["track"], config["venue"], config["market"],
        config["quote_currency"], config["sides"], config["capital_mode"],
        config["portfolio_isolation_required"], config["strategy_contract"],
        config["selection_contract"],
    )
    if identity != (
        2, "A-2", "coinone", "spot", "KRW", ["long"], "portfolio_equity", True,
        "track_a_long_only_rotation_v1", "coinone_krw_native_v1",
    ):
        raise ValueError("Track A-2 identity or long-only safety contract changed")
    if config["status"] not in ("paused", "active") or config["mode"] not in ("observe", "live"):
        raise ValueError("invalid Track A-2 operating mode")
    for name in ("execution_enabled", "portfolio_isolation_confirmed"):
        if type(config[name]) is not bool:
            raise ValueError(f"invalid Track A-2 boolean field: {name}")
    if config["execution_enabled"] != (config["mode"] == "live"):
        raise ValueError("Track A-2 live mode and execution flag must change together")
    approval = config["live_approval_id"]
    if approval is not None and (not isinstance(approval, str) or not APPROVAL.fullmatch(approval)):
        raise ValueError("invalid Track A-2 approval identifier")
    address = config["expected_egress_ip"]
    if address is not None:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise ValueError("invalid Track A-2 egress address") from None
        if parsed.version != 4 or parsed.is_unspecified or parsed.is_multicast:
            raise ValueError("invalid Track A-2 egress address")
    if not isinstance(config["env_path"], str) or not config["env_path"]:
        raise ValueError("invalid Track A-2 credential path")
    state = resolved_state_directory(config, root)
    approved_parent = (Path(root).resolve().parent / "trading-room-state").resolve()
    if state != approved_parent / "track-a-2":
        raise ValueError("Track A-2 state directory must be its dedicated sibling path")

    universe = config["universe"]
    if (
        not isinstance(universe, list) or len(universe) > 20
        or any(not isinstance(coin, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", coin) for coin in universe)
        or len(set(universe)) != len(universe)
    ):
        raise ValueError("invalid Track A-2 universe")
    _number(config, "basket_size", positive=True, high=20, integer=True)
    _number(config, "max_open_books", positive=True, high=config["basket_size"], integer=True)
    for name in (
        "min_quote_volume_24h", "min_range_24h_pct", "max_range_24h_pct",
        "max_spread_bp", "scan_seconds", "decision_ms", "quote_max_age_ms",
        "account_poll_s", "account_fresh_s", "reconcile_poll_s", "reconcile_halt_s",
        "account_mismatch_grace_s", "http_timeout_s", "minimum_exit_multiple",
        "stop_limit_buffer_bp", "max_fee_rate",
    ):
        _number(config, name, positive=True)
    if config["minimum_exit_multiple"] <= 1:
        raise ValueError("Track A-2 exit multiple must exceed one")
    if config["stop_limit_buffer_bp"] > 1000 or config["max_fee_rate"] > 0.01:
        raise ValueError("Track A-2 stop buffer or fee ceiling is unsafe")
    if config["decision_ms"] > config["quote_max_age_ms"]:
        raise ValueError("Track A-2 decision interval exceeds quote lifetime")
    if config["account_poll_s"] > config["account_fresh_s"]:
        raise ValueError("Track A-2 account polling is slower than its freshness bound")
    if config["reconcile_poll_s"] >= config["reconcile_halt_s"]:
        raise ValueError("Track A-2 reconciliation timing is inverted")
    _number(config, "public_storage_max_bytes", low=64 * 1024 * 1024, integer=True)
    _number(config, "stop_limit_buffer_ticks", positive=True, high=100, integer=True)
    for name in (
        "cash_fraction", "unit_fraction", "book_notional_fraction",
        "portfolio_notional_fraction", "book_risk_fraction", "daily_loss_fraction",
        "depth_fraction",
    ):
        _number(config, name, positive=True, high=1)
    if not config["min_range_24h_pct"] < config["max_range_24h_pct"]:
        raise ValueError("Track A-2 range filter is inverted")
    if config["unit_fraction"] > config["book_notional_fraction"]:
        raise ValueError("Track A-2 unit exceeds per-book notional")
    if config["book_notional_fraction"] > config["portfolio_notional_fraction"]:
        raise ValueError("Track A-2 book exceeds portfolio notional")
    if config["book_risk_fraction"] > config["daily_loss_fraction"]:
        raise ValueError("Track A-2 book risk exceeds daily loss budget")
    if config["book_risk_fraction"] > 0.05 or config["daily_loss_fraction"] > 0.10:
        raise ValueError("Track A-2 loss fraction exceeds the hard safety ceiling")

    signal, strategy = config["signal"], config["strategy"]
    if not isinstance(signal, dict) or not set(signal) <= set(SIG):
        raise ValueError("invalid Track A-2 signal fields")
    if not isinstance(strategy, dict) or not set(strategy) <= ALLOWED_STRATEGY:
        raise ValueError("invalid Track A-2 strategy fields")
    forced = {
        **strategy, "side": "long", "unit_qty": 1.0, "max_notional": 4.0,
        "cap_usdt": 1.0, "tick": 1.0, "qstep": 1.0, "fee_rt_pct": 0.0,
        "lever": 0, "margin_mode": None, "unit_frac": 0.0, "cap_frac": 0.0,
        "daily_loss_frac": 0.0, "notional_frac": 0.0, "entry_random": 0.0,
        "exit_random": 0.0,
    }
    bad = valid_params(forced, signal)
    if bad:
        raise ValueError("invalid Track A-2 strategy parameters: " + ",".join(bad))
    if strategy.get("entry_random", 0) or strategy.get("exit_random", 0):
        raise ValueError("Track A-2 measurement baselines cannot execute live")
    if not isinstance(strategy.get("max_units"), int) or not 1 <= strategy["max_units"] <= 10:
        raise ValueError("Track A-2 max_units must be an integer from one to ten")
    if not isinstance(strategy.get("max_stops_day"), int) or not 1 <= strategy["max_stops_day"] <= 20:
        raise ValueError("Track A-2 max_stops_day must be an integer from one to twenty")
    if (
        strategy.get("trim_taker_after_s") != 0
        or strategy.get("trim_rest_pct") != 0
        or strategy.get("blowoff_atr") != 0
        or strategy.get("cap_per_unit") != 0
    ):
        raise ValueError("Track A-2 Coinone execution requires immediate trims and one campaign cap")
    return config


def load(path=CONFIG, *, root=ROOT):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("invalid Track A-2 configuration") from exc
    return _validate(config, root=root)


def main():
    config = load()
    state = resolved_state_directory(config)
    print(
        f"{config['track']} configuration valid: Coinone KRW spot long-only; "
        f"mode={config['mode']}; execution={str(config['execution_enabled']).lower()}; state={state}"
    )


if __name__ == "__main__":
    main()
