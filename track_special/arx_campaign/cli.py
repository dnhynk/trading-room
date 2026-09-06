"""Command-line surface for the isolated ARX research campaign."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from .config import CampaignConfig, load
from .contracts import RiskState
from .execution.engine import CampaignEngine
from .reporting.korean import daily_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="track_special")
    parser.add_argument("--config", default="track_special/configs/observe.yaml")
    parser.add_argument("--state", help="absolute SQLite state path")
    parser.add_argument(
        "--state-directory", help="absolute external root for public observations"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "doctor",
        "collect",
        "observe",
        "paper",
        "status",
        "report",
        "pause-entries",
        "cancel-entry-orders",
        "request-exit",
        "emergency-halt",
        "validate-live",
    ):
        subparsers.add_parser(name)
    replay = subparsers.add_parser("replay")
    replay.add_argument(
        "--scenario",
        choices=(
            "long_decline",
            "pump_absent",
            "probe_only_then_rally",
            "fake_breakout_after_add",
            "gap_collapse",
            "mark_last_divergence",
            "funding_spike_interval_change",
            "trading_halt",
            "server_stop_rejected",
            "residual_orders_after_liquidation",
            "adl_forced_reduction",
            "restart_duplicate_order",
        ),
        default="pump_absent",
    )
    proposal = subparsers.add_parser("propose-risk-change")
    proposal.add_argument("--change", default="UNSPECIFIED")
    proposal.add_argument("--before", default="UNKNOWN")
    proposal.add_argument("--after", default="UNKNOWN")
    proposal.add_argument("--worst-loss", default="UNKNOWN")
    proposal.add_argument("--cooling-hours", type=int, default=48)
    return parser


def _outside_repository(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("state path must be absolute")
    resolved = path.resolve()
    repository = Path(__file__).resolve().parents[2]
    if resolved == repository or resolved.is_relative_to(repository):
        raise ValueError("state path must remain outside the repository")
    return resolved


def _state(args: argparse.Namespace, config: CampaignConfig) -> Path:
    path = Path(args.state) if args.state else config.state_directory / "campaign.sqlite"
    return _outside_repository(path)


def _market_state(args: argparse.Namespace, config: CampaignConfig) -> Path:
    path = (
        Path(args.state_directory)
        if args.state_directory
        else config.state_directory / "marketdata"
    )
    return _outside_repository(path)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _collect(args: argparse.Namespace, config: CampaignConfig) -> dict[str, Any]:
    from .marketdata.collector import collect_public_snapshot

    receipt = collect_public_snapshot(_market_state(args, config))
    value = asdict(receipt)
    value["paths"] = {key: str(path) for key, path in receipt.paths.items()}
    value["first_received_at"] = (
        receipt.first_received_at.isoformat() if receipt.first_received_at else None
    )
    value["last_received_at"] = (
        receipt.last_received_at.isoformat() if receipt.last_received_at else None
    )
    value["exchange_writes"] = 0
    value["evidence_kind"] = "actual_unsigned_public_api"
    return value


def _synthetic_replay(name: str) -> dict[str, Any]:
    from .replay import get_scenario, replay

    scenario = get_scenario(name)
    result = replay(
        scenario.bars, Decimal("10"), stop=Decimal("9"), take_profit=Decimal("11")
    )
    return {
        "scenario": name,
        "evidence_kind": "synthetic_failure_scenario_not_profitability_evidence",
        "exchange_writes": 0,
        "operational_events": scenario.operational_events,
        "result": asdict(result),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load(args.config)

    if args.command == "doctor":
        print(
            _json(
                {
                    "mode": config.mode.value,
                    "config_hash": config.config_hash,
                    "state_directory": str(config.state_directory),
                    "exchange_writes": 0,
                    "credentials_read": False,
                    "live_operational": False,
                    "live_issues": config.live_issues,
                }
            )
        )
        return 0
    if args.command in {"collect", "observe"}:
        print(_json(_collect(args, config)))
        return 0
    if args.command == "paper":
        print(
            _json(
                {
                    "command": "paper",
                    "mode": config.mode.value,
                    "exchange_writes": 0,
                    "live_operational": False,
                    "note": "paper engine requires explicit synthetic intents and risk approvals",
                }
            )
        )
        return 0
    if args.command == "replay":
        print(_json(_synthetic_replay(args.scenario)))
        return 0
    if args.command == "validate-live":
        blockers = list(config.live_issues)
        blockers.extend(
            (
                "AUTHENTICATED_ACCOUNT_CAPABILITIES_UNVERIFIED",
                "SERVER_PROTECTION_CAPABILITY_UNVERIFIED",
                "LIVE_ADAPTER_NOT_IMPLEMENTED",
                "USER_RISK_APPROVAL_MISSING",
            )
        )
        print(
            _json(
                {
                    "config_mode": config.mode.value,
                    "live_operational": False,
                    "private_requests": 0,
                    "exchange_writes": 0,
                    "blockers": tuple(dict.fromkeys(blockers)),
                }
            )
        )
        return 2
    if args.command == "propose-risk-change":
        if args.cooling_hours < 48:
            raise ValueError("risk-increasing proposal cooling time cannot be below 48 hours")
        proposal = {
            "kind": "risk_change_proposal",
            "change": args.change,
            "before": args.before,
            "after": args.after,
            "worst_loss_after": args.worst_loss,
            "config_hash": config.config_hash,
            "cooling_hours": args.cooling_hours,
            "approval": "pending",
            "applied": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        target = config.state_directory / "risk-change-proposals.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_json(proposal) + "\n")
        print(_json({**proposal, "path": str(target)}))
        return 0

    with CampaignEngine(_state(args, config), operating_mode=config.mode) as engine:
        if args.command == "pause-entries":
            engine.set_control(RiskState.PAUSE_ENTRIES, "operator")
        elif args.command == "cancel-entry-orders":
            print(
                _json(
                    {
                        "cancellation_state": engine.cancel_entry_orders(),
                        "exchange_writes": 0,
                        "note": "local state only; exchange cancellation is not claimed",
                    }
                )
            )
            return 0
        elif args.command == "request-exit":
            engine.request_exit()
        elif args.command == "emergency-halt":
            engine.halt("operator_emergency_halt")
        elif args.command == "report":
            status = engine.status()
            print(
                daily_report(
                    {
                        "risk_state": status["risk_state"],
                        "reconciliation": status["startup_reconciled"],
                        "reservations": len(status["orders"]),
                        "unprotected_exposure": status["unprotected_entry_count"],
                    }
                )
            )
            return 0
        print(_json(engine.status()))
        return 0
