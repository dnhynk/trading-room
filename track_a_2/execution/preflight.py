"""Independent activation gates checked before credentials and before every order."""
import ipaddress
import json
from pathlib import Path

from track_a_2.settings import APPROVAL, ROOT, resolved_state_directory


def block_reasons(config, *, root=ROOT, egress=None):
    root = Path(root).resolve()
    reasons = []
    try:
        registry = json.loads((root / "config" / "tracks.json").read_text(encoding="utf-8-sig"))
        registered = registry["tracks"]["A-2"]
    except (OSError, ValueError, KeyError, TypeError):
        registered = {}
        reasons.append("track_registry_unavailable")
    if registered.get("status") != "active":
        reasons.append("track_registry_not_active")
    if registered.get("execution_enabled") is not True:
        reasons.append("track_registry_execution_disabled")
    if config.get("status") != "active":
        reasons.append("config_not_active")
    if config.get("mode") != "live" or config.get("execution_enabled") is not True:
        reasons.append("config_execution_disabled")
    if config.get("portfolio_isolation_confirmed") is not True:
        reasons.append("portfolio_isolation_unconfirmed")
    approval = config.get("live_approval_id")
    if not isinstance(approval, str) or not APPROVAL.fullmatch(approval):
        reasons.append("evaluation_approval_missing")
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
    for directory, label in ((root, "repository"), (state, "runtime")):
        for name in ("STOP", "PAUSE"):
            if (directory / name).exists():
                reasons.append(f"{label}_{name.lower()}")
    return list(dict.fromkeys(reasons))


def require_live(config, *, root=ROOT, egress=None):
    reasons = block_reasons(config, root=root, egress=egress)
    if reasons:
        raise RuntimeError("Track A-2 live preflight blocked: " + ", ".join(reasons))
    return True
