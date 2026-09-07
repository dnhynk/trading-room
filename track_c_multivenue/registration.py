"""Local, immutable registration for genuinely future evaluation windows."""
import json
from pathlib import Path
import time

from track_c_multivenue import POLICIES, VERSION
from track_c_multivenue.contract import digest, research_contract, source_identity


def _outside_repository(path):
    root = Path(__file__).resolve().parents[1]
    target = Path(path).resolve()
    if target == root or root in target.parents:
        raise ValueError("registration must be outside the repository")
    return target


def registration_document(
    a2_config, start_ms, end_ms, *, created_ms=None,
):
    created_ms = time.time_ns() // 1_000_000 if created_ms is None else created_ms
    for name, value in (
        ("created_ms", created_ms), ("start_ms", start_ms), ("end_ms", end_ms),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid registration {name}")
    if not created_ms < start_ms < end_ms:
        raise ValueError("registration must be created before a nonempty future window")
    contract = research_contract(a2_config)
    source = source_identity()
    body = {
        "schema": 1,
        "version": VERSION,
        "status": "REGISTERED_BEFORE_WINDOW",
        "created_ms": created_ms,
        "evaluation_window": {
            "start_ms_inclusive": start_ms,
            "end_ms_inclusive": end_ms,
            "session_rule": "whole_finalized_sessions_inside_window",
            "cross_session_labels": "forbidden",
            "input_completeness": "must_be_audited_separately",
        },
        "policies": list(POLICIES),
        "primary_metrics": [
            "fully_cash_settled.mean_net_bp_per_outcome",
            "freshly_marked_outcomes.mean_net_bp_per_valued_outcome",
            "censored_residual_principal_fraction",
        ],
        "censoring": {
            "exclude_from_cash_settled_mean": True,
            "exclude_from_marked_mean_only_when_valuation_unavailable": True,
            "always_report_zero_residual_cash_recovery_stress": True,
            "always_report_rate_and_residual_principal_fraction": True,
            "winner_selection_when_censored": "withheld_pending_predeclared_inference",
        },
        "exclusions": [
            "session_fails_recording_quality_contract",
            "session_not_wholly_inside_registered_window",
            "candidate_inside_scheduled_terminal_entry_guard",
        ],
        "inference_requirements": [
            "frequency_matched_random_exclusion_control",
            "coin_date_and_market_shock_cluster_uncertainty",
            "sequential_portfolio_capital_evaluation",
        ],
        "source_digest": digest(source),
        "contract_digest": digest(contract),
        "orders_enabled": False,
        "live_promotion": "forbidden",
    }
    body["registration_digest"] = digest(body)
    return body


def create_registration(path, a2_config, start_ms, end_ms, *, created_ms=None):
    target = _outside_repository(path)
    document = registration_document(
        a2_config, start_ms, end_ms, created_ms=created_ms,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(
            document, ensure_ascii=False, indent=2, allow_nan=False,
        ) + "\n")
    return document


def validate_registration(path, a2_config, study):
    source = _outside_repository(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("evaluation registration is unreadable") from None
    if not isinstance(document, dict):
        raise ValueError("invalid evaluation registration")
    stored_digest = document.get("registration_digest")
    unsigned = dict(document)
    unsigned.pop("registration_digest", None)
    if stored_digest != digest(unsigned):
        raise ValueError("evaluation registration digest mismatch")
    window = document.get("evaluation_window") or {}
    start = window.get("start_ms_inclusive")
    end = window.get("end_ms_inclusive")
    created = document.get("created_ms")
    if not all(type(value) is int for value in (created, start, end)):
        raise ValueError("invalid registered evaluation window")
    if not created < start < end:
        raise ValueError("registration was not created before its window")
    expected = registration_document(
        a2_config, start, end, created_ms=created,
    )
    if document != expected:
        raise ValueError("evaluation registration differs from current contract")
    outside = [
        session.session_id for session in study.sessions
        if session.captured_ms < start or session.completed_ms > end
    ]
    if outside:
        raise ValueError("one or more sessions fall outside the registered window")
    return {
        "status": "PREREGISTERED_FUTURE_WINDOW",
        "registration_file": str(source),
        "registration_digest": stored_digest,
        "created_ms": created,
        "evaluation_window": window,
        "future_window_enforced": True,
        "session_completeness_proven": False,
        "interpretation": "local_registration_not_external_timestamp_attestation",
    }


def exploratory_design():
    return {
        "status": "EXPLORATORY_POSTHOC",
        "registration_file": None,
        "registration_digest": None,
        "future_window_enforced": False,
        "session_completeness_proven": False,
        "interpretation": "run_receipt_freezes_inputs_but_does_not_prove_prior_registration",
    }
