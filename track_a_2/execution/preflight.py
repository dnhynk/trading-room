"""Independent activation gates checked before credentials and before every order."""
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import sqlite3

from track_a_2 import EXECUTION_VERSION
from track_a_2.settings import (
    EVALUATION_APPROVAL,
    OWNER_APPROVAL,
    ROOT,
    account_access_approved,
    resolved_state_directory,
)


OPERATIONAL_CONFIG = {
    "status", "mode", "execution_enabled", "portfolio_isolation_confirmed",
    "shared_portfolio_approved", "credential_profile",
    "repository_ab_controls_acknowledged", "owner_unvalidated_live_approved",
    "live_approval_id", "evaluation_manifest", "evaluation_sha256",
    "expected_egress_ip", "env_path", "state_directory",
}


def evaluation_config_digest(config):
    body = {key: value for key, value in config.items() if key not in OPERATIONAL_CONFIG}
    raw = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def evaluation_source_digest(root=ROOT):
    root = Path(root).resolve()
    required = [
        *(root / "common" / name for name in ("cycle.py", "signal.py", "risk.py")),
        *(
            root / "track_c" / "execution" / name
            for name in ("coinone.py", "http_pool.py", "rate_limit.py")
        ),
        root / "pyproject.toml",
    ]
    if any(not path.is_file() for path in required):
        raise OSError("Track A-2 evaluation source unavailable")
    track_paths = list((root / "track_a_2").rglob("*.py"))
    if not track_paths:
        raise OSError("Track A-2 evaluation source unavailable")
    paths = track_paths
    paths.extend(required)
    paths = sorted(
        (path for path in paths if path.is_file() and "__pycache__" not in path.parts),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not paths:
        raise OSError("Track A-2 evaluation source unavailable")
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        # Git checkouts can materialize CRLF on Windows and LF on Linux. The
        # evaluated program is identical, so bind normalized source content.
        body = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _evaluation_reasons(config, root):
    reasons = []
    relative = config.get("evaluation_manifest")
    expected_hash = config.get("evaluation_sha256")
    if not relative or not expected_hash:
        return ["evaluation_manifest_missing"]
    path = resolved_state_directory(config, root) / relative
    allowed = (resolved_state_directory(config, root) / "evaluations").resolve()
    try:
        if path.resolve().parent != allowed:
            return ["evaluation_manifest_boundary"]
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            reasons.append("evaluation_manifest_digest")
        manifest = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return reasons + ["evaluation_manifest_unavailable"]
    try:
        source = evaluation_source_digest(root)
    except OSError:
        reasons.append("evaluation_source_unavailable")
        return reasons
    identity = {
        "schema": 2,
        "track": "A-2",
        "approval_id": config.get("live_approval_id"),
        "execution_version": EXECUTION_VERSION,
        "config_digest": evaluation_config_digest(config),
        "source_digest": source,
        "universe": config.get("universe"),
        "result": "APPROVED",
    }
    if not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in identity.items()):
        reasons.append("evaluation_manifest_identity")
        return reasons
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("data_digest") or "")):
        reasons.append("evaluation_data_digest")
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict) or any(
        protocol.get(name) is not True
        for name in (
            "holdout", "feed_contiguous", "stress_passed", "ladder_beats_one_unit",
            "fixed_selection_from_seed", "user_approved",
        )
    ):
        reasons.append("evaluation_protocol_incomplete")
    execution = manifest.get("execution")
    try:
        maker = float(execution["maker_fee"])
        taker = float(execution["taker_fee"])
        latency = int(execution["latency_ms"])
        depth = float(execution["depth_fraction"])
        valid_execution = (
            all(math.isfinite(value) for value in (maker, taker, depth))
            and maker >= 0 and taker >= 0 and latency >= 0 and 0 < depth <= 1
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        valid_execution = False
    if not isinstance(execution, dict) or not valid_execution:
        reasons.append("evaluation_cost_model_incomplete")
    try:
        metrics = manifest["metrics"]
        main = metrics["main"]
        one = metrics["one_unit"]
        stress = metrics["stress"]
        main_pnl = float(main["net_pnl_krw"])
        one_pnl = float(one["net_pnl_krw"])
        stress_pnl = float(stress["net_pnl_krw"])
        valid_metrics = (
            all(math.isfinite(value) for value in (main_pnl, one_pnl, stress_pnl))
            and main_pnl > 0
            and stress_pnl > 0
            and int(main["campaigns"]) > 0
            and main.get("halt") is None
            and stress.get("halt") is None
            and (
                config["strategy"]["max_units"] == 1
                or main_pnl > one_pnl
            )
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        valid_metrics = False
    if not valid_metrics:
        reasons.append("evaluation_metrics_incomplete")
    return reasons


def block_reasons(config, *, root=ROOT, egress=None):
    root = Path(root).resolve()
    reasons = []
    try:
        registry = json.loads((root / "config" / "tracks.json").read_text(encoding="utf-8-sig"))
        registered = registry["tracks"]["A-2"]
        c_registered = registry["tracks"].get("C", {})
    except (OSError, ValueError, KeyError, TypeError):
        registered = {}
        c_registered = {}
        reasons.append("track_registry_unavailable")
    if registered.get("status") != "active":
        reasons.append("track_registry_not_active")
    if registered.get("execution_enabled") is not True:
        reasons.append("track_registry_execution_disabled")
    if config.get("status") != "active":
        reasons.append("config_not_active")
    if config.get("mode") != "live" or config.get("execution_enabled") is not True:
        reasons.append("config_execution_disabled")
    if not account_access_approved(config):
        reasons.append(
            "portfolio_isolation_unconfirmed"
            if config.get("portfolio_isolation_required")
            else "shared_portfolio_unapproved"
        )
    if not config.get("portfolio_isolation_required"):
        reserved = set(config.get("shared_reserved_symbols") or ())
        c_symbols = set(c_registered.get("trading_coins") or ())
        if c_symbols and not c_symbols <= reserved:
            reasons.append("shared_reserved_symbols_stale")
    approval = config.get("live_approval_id")
    if config.get("owner_unvalidated_live_approved"):
        if not isinstance(approval, str) or not OWNER_APPROVAL.fullmatch(approval):
            reasons.append("owner_live_approval_missing")
    else:
        if not isinstance(approval, str) or not EVALUATION_APPROVAL.fullmatch(approval):
            reasons.append("evaluation_approval_missing")
        reasons.extend(_evaluation_reasons(config, root))
    if not config.get("universe"):
        reasons.append("universe_empty")
    expected = config.get("expected_egress_ip")
    try:
        valid_ip = ipaddress.ip_address(expected).version == 4
    except (ValueError, TypeError):
        valid_ip = False
    if not valid_ip:
        reasons.append("egress_ip_unconfigured")
    elif egress is not None and egress != expected:
        reasons.append("egress_ip_mismatch")
    state = resolved_state_directory(config, root)
    approved_parent = (root.parent / "trading-room-state").resolve()
    if state != approved_parent / "track-a-2":
        reasons.append("state_directory_boundary")
    if not config.get("repository_ab_controls_acknowledged"):
        for name in ("STOP", "PAUSE"):
            if (root / name).exists():
                reasons.append(f"repository_{name.lower()}")
    for name in ("STOP", "PAUSE"):
        if (state / name).exists():
            reasons.append(f"runtime_{name.lower()}")
    return list(dict.fromkeys(reasons))


def require_live(config, *, root=ROOT, egress=None):
    reasons = block_reasons(config, root=root, egress=egress)
    if reasons:
        raise RuntimeError("Track A-2 live preflight blocked: " + ", ".join(reasons))
    return True


def recovery_reasons(config, *, root=ROOT, egress=None):
    """Gates for managing already-owned risk without authorizing new entries."""
    root = Path(root).resolve()
    reasons = []
    if not account_access_approved(config):
        reasons.append(
            "portfolio_isolation_unconfirmed"
            if config.get("portfolio_isolation_required")
            else "shared_portfolio_unapproved"
        )
    expected = config.get("expected_egress_ip")
    try:
        valid_ip = ipaddress.ip_address(expected).version == 4
    except (ValueError, TypeError):
        valid_ip = False
    if not valid_ip:
        reasons.append("egress_ip_unconfigured")
    elif egress is not None and egress != expected:
        reasons.append("egress_ip_mismatch")
    state = resolved_state_directory(config, root)
    if state != (root.parent / "trading-room-state" / "track-a-2").resolve():
        reasons.append("state_directory_boundary")
    database = state / "a2-ledger.sqlite"
    try:
        uri = "file:" + database.resolve().as_posix() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            row = connection.execute("SELECT body FROM state WHERE id=1").fetchone()
        finally:
            connection.close()
        ledger = json.loads(row[0]) if row else None
        if not isinstance(ledger, dict) or ledger.get("version") != 1:
            raise ValueError
        positioned = any(book.get("lots") for book in ledger.get("books", {}).values())
        terminal = {
            "FILLED", "CANCELED", "NOT_TRIGGERED_CANCELED", "CANCELED_NO_ORDER",
            "CANCELED_LIMIT_PRICE_EXCEED", "CANCELED_UNDER_PRODUCT_UNIT", "REJECTED",
        }
        active = any(
            order.get("status") not in terminal
            for order in ledger.get("orders", {}).values()
        )
        if not positioned and not active:
            reasons.append("recovery_exposure_missing")
    except (OSError, ValueError, TypeError, sqlite3.Error):
        reasons.append("recovery_ledger_unavailable")
    return list(dict.fromkeys(reasons))


def require_recovery(config, *, root=ROOT, egress=None):
    reasons = recovery_reasons(config, root=root, egress=egress)
    if reasons:
        raise RuntimeError("Track A-2 recovery preflight blocked: " + ", ".join(reasons))
    return True
