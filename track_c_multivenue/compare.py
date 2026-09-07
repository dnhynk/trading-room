"""Causal P0-P3 comparison over finalized A-2 multi-venue observations.

This is an offline counterfactual evaluator.  It imports no authenticated
client, has no live mode, and never treats a positive report as approval.
"""
from collections import Counter
from copy import deepcopy
import gzip
from itertools import groupby
import json
import math
from pathlib import Path

from track_a_2.execution.preflight import evaluation_config_digest
from track_a_2.market.feed import Market as A2Market
from track_a_2.settings import CONFIG as A2_CONFIG, load as load_a2
from track_c.market.state import Market as CMarket, candidates as c_candidates
from track_c.replay.execution import Attempt
from track_c_multivenue import POLICIES, VERSION
from track_c_multivenue.contract import (
    COINS, RECORDER_SESSION_MS, c_config, digest, research_contract,
    source_identity, terminal_entry_guard_ms,
)
from track_c_multivenue.diagnostics import (
    FillMarkoutProbe, local_minimum_action, markout_report, reference_group,
)
from track_c_multivenue.input import StudyTape
from track_c_multivenue.policies import membership, reasons
from track_c_multivenue.registration import exploratory_design, validate_registration


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_jsonl_gz(path, rows):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as target:
        for row in rows:
            target.write(json.dumps(
                row, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ) + "\n")
    temporary.replace(path)


def _outside_repository(path):
    root = Path(__file__).resolve().parents[1]
    target = Path(path).resolve()
    if target == root or root in target.parents:
        raise ValueError("research output must be outside the repository")
    return target


def _market_seed(session, coin, a2_config):
    seed = session.observation.seed["markets"][coin]
    candles = seed["candles"]
    return A2Market(
        coin, a2_config, seed["contract"], seed["units"],
        session.observation.manifest["fee_assumption"],
        candles.get("1m", ()), candles.get("15m", ()), candles.get("1d", ()),
        now_ms=session.captured_ms,
    )


def _external_eligible(state, cfg):
    if not state or not state.get("entry_fresh"):
        return False
    reference = state.get("reference") or {}
    return bool(
        reference.get("ready")
        and reference.get("dev_ticks", -math.inf) >= cfg["entry_ticks"]
        and reference.get("m10") is not None
        and reference["m10"] > -cfg["common_drop_ticks"]
        and state.get("entry_eligible")
    )


def _shared_action(state, cfg, candidate_id, cash):
    if not state:
        return None, "local_state_unavailable"
    if not state.get("entry_fresh"):
        return None, "local_book_stale"
    if not (state.get("reference") or {}).get("ready"):
        return None, "external_reference_unavailable"
    if not (state.get("risk") or {}).get("ready"):
        return None, "local_risk_unavailable"
    copied = deepcopy(state)
    copied["episode_id"] = candidate_id
    actions = c_candidates(
        copied, cfg, cash, cash * cfg["risk_fraction"], research=True,
    )
    action = next((row for row in actions if row["id"] == "0:minimum"), None)
    if action is None:
        return None, "common_price_size_or_capacity_unavailable"
    return action, None


def _candidate_summary(state):
    if not state:
        return None
    reference = state.get("reference") or {}
    risk = state.get("risk") or {}
    return {
        "t_ms": state["t_ms"],
        "bid": state["bid"],
        "ask": state["ask"],
        "tick": state["tick"],
        "spread_ticks": (state["ask"] - state["bid"]) / state["tick"],
        "pressure": state["pressure"],
        "flow": state["flow"],
        "entry_fresh": state["entry_fresh"],
        "c_episode_id": state.get("episode_id"),
        "reference": {
            key: reference.get(key)
            for key in (
                "ready", "reason", "fair", "lower", "upper", "dev_ticks",
                "disagreement_ticks", "m10", "m30", "regime_id",
            )
        },
        "risk": {
            key: risk.get(key)
            for key in ("ready", "reason", "n_returns", "distance_price")
        },
    }


def _finish(probes, outcomes, now, *, boundary=False):
    remaining = []
    for row in probes:
        attempt = row["attempt"]
        if boundary and not attempt.done:
            attempt.censored = True
            attempt.observation_gap = True
        if attempt.done or boundary:
            result_now = attempt.end if attempt.done else now
            valuation_fresh = attempt.fresh(result_now)
            outcome = attempt.result(now if not attempt.done else None)
            residual = outcome["residual_qty"] > outcome["action"]["qty_step"] * 0.1
            if not residual:
                valuation = "not_applicable"
            elif valuation_fresh:
                valuation = "fresh_bid"
            else:
                valuation = "unavailable"
            notional = outcome["action"]["notional"]
            outcome.update({
                "policy": row["policy"],
                "candidate_id": row["candidate_id"],
                "session_id": row["session_id"],
                "orders_enabled": False,
                "fully_cash_settled": bool(
                    not outcome["censored"] and not residual
                ),
                "residual_valuation_status": valuation,
                "marked_net_bp": (
                    outcome["net_krw"] / notional * 10000
                    if notional and valuation != "unavailable" else None
                ),
            })
            outcomes.append(outcome)
        else:
            remaining.append(row)
    return remaining


def _policy_report(candidates, outcomes, name):
    selected = [row for row in candidates if row["selected_by_policy"][name]]
    submitted = [row for row in candidates if row["attempted_by_policy"][name]]
    rows = [row for row in outcomes if row["policy"] == name]
    fills = [row for row in rows if row["filled_qty"] > 0]
    completed = [row for row in rows if not row["censored"]]
    censored = [row for row in rows if row["censored"]]
    settled = [row for row in rows if row["fully_cash_settled"]]
    marked = [row for row in rows if row["marked_net_bp"] is not None]
    fresh_residual = [
        row for row in rows
        if row["residual_valuation_status"] == "fresh_bid"
    ]
    unvalued_residual = [
        row for row in rows
        if row["residual_valuation_status"] == "unavailable"
    ]
    filled_principal = sum(
        row["filled_qty"] * row["action"]["price"] for row in rows
    )
    censored_residual_principal = sum(
        row["residual_cost_krw"] for row in censored
    )

    def mean(items, key):
        values = [row[key] for row in items]
        return sum(values) / len(values) if values else None

    return {
        "union_events": len(candidates),
        "premise_selected": len(selected),
        "common_attempts": len(submitted),
        "outcomes": len(rows),
        "fills": len(fills),
        "completed_outcomes": len(completed),
        "censored_outcomes": len(censored),
        "censored_outcome_rate": len(censored) / len(rows) if rows else None,
        "censored_filled_outcomes": sum(row["filled_qty"] > 0 for row in censored),
        "censored_filled_rate": (
            sum(row["filled_qty"] > 0 for row in censored) / len(fills)
            if fills else None
        ),
        "censored_residual_outcomes": sum(
            row["residual_cost_krw"] > 0 for row in censored
        ),
        "filled_principal_krw": filled_principal,
        "censored_residual_principal_krw": censored_residual_principal,
        "censored_residual_principal_fraction": (
            censored_residual_principal / filled_principal
            if filled_principal else None
        ),
        "fully_cash_settled": {
            "outcomes": len(settled),
            "mean_net_krw_per_outcome": mean(settled, "net_krw"),
            "mean_net_bp_per_outcome": mean(settled, "marked_net_bp"),
            "nonadditive_sum_krw": sum(row["net_krw"] for row in settled),
        },
        "freshly_marked_outcomes": {
            "outcomes": len(marked),
            "mean_net_krw_per_valued_outcome": mean(marked, "net_krw"),
            "mean_net_bp_per_valued_outcome": mean(marked, "marked_net_bp"),
            "fresh_residual_outcomes": len(fresh_residual),
            "fresh_residual_cost_krw": sum(
                row["residual_cost_krw"] for row in fresh_residual
            ),
            "fresh_residual_value_krw": sum(
                row["residual_value_krw"] for row in fresh_residual
            ),
        },
        "unvalued_residual_inventory": {
            "outcomes": len(unvalued_residual),
            "cost_krw": sum(
                row["residual_cost_krw"] for row in unvalued_residual
            ),
        },
        "cash_recovery_zero_residual_stress": {
            "outcomes": len(rows),
            "mean_krw_per_attempt": mean(rows, "cash_net_krw"),
            "mean_bp_per_attempt": mean(rows, "net_bp"),
            "nonadditive_sum_krw": sum(row["cash_net_krw"] for row in rows),
            "interpretation": "remaining_inventory_is_deliberately_valued_at_zero",
        },
        "selection_eligible": False,
        "selection_blocked_by": [
            "censoring_requires_stratified_interpretation",
            "counterfactual_attempts_overlap",
            "frequency_matched_control_not_yet_run",
            "future_holdout_registration_required",
        ],
        "interpretation": "overlapping candidate attempts are counterfactuals, not portfolio turnover",
    }


def evaluate(study, a2_config):
    audit = study.audit()
    if not audit["safe_for_research"]:
        raise ValueError("one or more observation sessions failed research quality")
    expected_config = evaluation_config_digest(a2_config)
    for row in audit["sessions"]:
        if row["manifest_config_digest"] != expected_config:
            raise ValueError("A-2 signal configuration differs from recorded session")

    cfg = c_config()
    cash = research_contract(a2_config)["comparison_cash_krw"]
    all_candidates = []
    outcomes = []
    diagnostic_outcomes = []
    counts = Counter()
    for key in (
        "external_eligible_frames", "discount_below_floor_frames",
        "common_market_fall_frames", "local_entry_stale_frames",
        *(f"{coin}_{name}_frames" for coin in COINS for name in (
            "external_eligible", "discount_below_floor",
            "common_market_fall", "local_entry_stale",
        )),
    ):
        counts[key] = 0

    for session in study.sessions:
        c_markets = {coin: CMarket(coin, cfg) for coin in COINS}
        a2_markets = {coin: _market_seed(session, coin, a2_config) for coin in COINS}
        contracts = {
            coin: session.observation.seed["markets"][coin]["contract"]
            for coin in COINS
        }
        units = {
            coin: session.observation.seed["markets"][coin]["units"]
            for coin in COINS
        }
        last_a2_signal = {coin: None for coin in COINS}
        pending_a2_signals = {coin: [] for coin in COINS}
        last_sell_arrival = {coin: None for coin in COINS}
        probes = []
        diagnostic_probes = []
        last_now = session.captured_ms
        last_sequence = 0
        last_received_ns = None
        interval = cfg["decision_ms"]
        next_decision = (session.captured_ms // interval + 1) * interval
        scheduled_entry_cutoff_ms = (
            session.captured_ms + RECORDER_SESSION_MS - terminal_entry_guard_ms(cfg)
        )

        def sample(decision_bucket_ms, decision_after_sequence, included_received_ns):
            nonlocal probes
            decision_ns = (decision_bucket_ms + 1) * 1_000_000
            now = decision_bucket_ms + 1
            if included_received_ns is not None and included_received_ns >= decision_ns:
                raise ValueError("decision includes an arrival after its effective time")
            snapshots = {
                coin: c_markets[coin].snapshot(now, contracts[coin], units[coin])
                for coin in COINS
            }
            for coin, state in snapshots.items():
                counts["coin_decision_frames"] += 1
                counts[f"{coin}_decision_frames"] += 1
                if state is None:
                    counts["local_state_unavailable_frames"] += 1
                    counts[f"{coin}_local_state_unavailable_frames"] += 1
                    continue
                counts["local_state_frames"] += 1
                counts[f"{coin}_local_state_frames"] += 1
                reference = state.get("reference") or {}
                risk = state.get("risk") or {}
                reference_reason = (
                    "ready" if reference.get("ready")
                    else str(reference.get("reason") or "unavailable")
                )
                risk_reason = (
                    "ready" if risk.get("ready")
                    else str(risk.get("reason") or "unavailable")
                )
                counts[f"reference_{reference_reason}_frames"] += 1
                counts[f"{coin}_reference_{reference_reason}_frames"] += 1
                counts[f"risk_{risk_reason}_frames"] += 1
                counts[f"{coin}_risk_{risk_reason}_frames"] += 1
                if not state.get("entry_fresh"):
                    counts["local_entry_stale_frames"] += 1
                    counts[f"{coin}_local_entry_stale_frames"] += 1
                elif reference.get("ready") and reference.get(
                    "dev_ticks", -math.inf,
                ) < cfg["entry_ticks"]:
                    counts["discount_below_floor_frames"] += 1
                    counts[f"{coin}_discount_below_floor_frames"] += 1
                elif (
                    reference.get("ready")
                    and reference.get("m10") is not None
                    and reference["m10"] <= -cfg["common_drop_ticks"]
                ):
                    counts["common_market_fall_frames"] += 1
                    counts[f"{coin}_common_market_fall_frames"] += 1
                elif _external_eligible(state, cfg):
                    counts["external_eligible_frames"] += 1
                    counts[f"{coin}_external_eligible_frames"] += 1
            for probe in probes:
                coin = probe["attempt"].a["coin"]
                probe["attempt"].decide(now, snapshots[coin])
            probes = _finish(probes, outcomes, now)
            for probe in diagnostic_probes:
                probe.observe(now, snapshots[probe.action["coin"]])
            counts["decision_frames"] += 1

            for coin in COINS:
                state = snapshots[coin]
                dip_signals = pending_a2_signals[coin]
                pending_a2_signals[coin] = []
                a2_trigger = bool(dip_signals)
                c_trigger = bool(state and state.get("new_episode"))
                if not (a2_trigger or c_trigger):
                    continue
                signal_at = last_a2_signal[coin]
                sell_at = last_sell_arrival[coin]
                known_recent = bool(
                    signal_at is not None
                    and 0 <= now - signal_at[0]
                    <= a2_config["strategy"]["buy_ttl_s"] * 1000
                )
                external = _external_eligible(state, cfg)
                selected = membership(
                    a2_trigger=a2_trigger,
                    c_trigger=c_trigger,
                    external_eligible=external,
                    a2_known_recent=known_recent,
                )
                rejected = reasons(
                    a2_trigger=a2_trigger,
                    c_trigger=c_trigger,
                    external_eligible=external,
                    a2_known_recent=known_recent,
                )
                candidate_id = f"{session.session_id}:{decision_ns}:{coin}"
                action, action_reason = _shared_action(
                    state, cfg, candidate_id, cash,
                )
                terminal_entry_allowed = now <= scheduled_entry_cutoff_ms
                if action is not None and not terminal_entry_allowed:
                    action = None
                    action_reason = "scheduled_session_terminal_entry_guard"
                    counts["terminal_entry_guard_frames"] += 1
                    counts[f"{coin}_terminal_entry_guard_frames"] += 1
                attempted = {
                    name: bool(accepted and action is not None)
                    for name, accepted in selected.items()
                }
                trigger_ns = [
                    row["available_received_ns"] for row in dip_signals
                ]
                if c_trigger and sell_at is not None:
                    trigger_ns.append(sell_at[2])
                latest_trigger_ns = max(trigger_ns) if trigger_ns else None
                if latest_trigger_ns is not None and latest_trigger_ns >= decision_ns:
                    raise ValueError("trigger was not observable at decision time")
                diagnostic_group = reference_group(state, cfg)
                diagnostic_action, diagnostic_reason = local_minimum_action(
                    state, cfg, candidate_id,
                )
                if diagnostic_action is not None and not terminal_entry_allowed:
                    diagnostic_action = None
                    diagnostic_reason = "scheduled_session_terminal_entry_guard"
                row = {
                    "schema": 1,
                    "version": VERSION,
                    "candidate_id": candidate_id,
                    "session_id": session.session_id,
                    "decision_bucket_ms": decision_bucket_ms,
                    "decision_ms": now,
                    "decision_ns": decision_ns,
                    "decision_after_sequence": decision_after_sequence,
                    "latest_included_received_ns": included_received_ns,
                    "latest_trigger_received_ns": latest_trigger_ns,
                    "coin": coin,
                    "trigger": {
                        "a2_deceleration": a2_trigger,
                        "c_new_sell_episode": c_trigger,
                        "a2_deceleration_known_and_recent": known_recent,
                        "a2_signals": dip_signals,
                    },
                    "external_eligible": external,
                    "selected_by_policy": selected,
                    "policy_reasons": rejected,
                    "attempted_by_policy": attempted,
                    "scheduled_terminal_entry_allowed": terminal_entry_allowed,
                    "common_action_reason": action_reason,
                    "common_action": deepcopy(action),
                    "diagnostic_reference_group": diagnostic_group,
                    "diagnostic_action_reason": diagnostic_reason,
                    "diagnostic_action": deepcopy(diagnostic_action),
                    "state": _candidate_summary(state),
                }
                all_candidates.append(row)
                counts["union_events"] += 1
                for name in POLICIES:
                    counts[f"{name}_selected"] += selected[name]
                    counts[f"{name}_attempted"] += attempted[name]
                    if attempted[name]:
                        probes.append({
                            "policy": name,
                            "candidate_id": candidate_id,
                            "session_id": session.session_id,
                            "attempt": Attempt(action, cfg, state),
                        })
                if diagnostic_action is not None:
                    diagnostic_probes.append(FillMarkoutProbe(
                        diagnostic_action, cfg, state, diagnostic_group,
                    ))

        for received_ms, arrivals in groupby(
            session.events(), key=lambda row: row["received_ms"],
        ):
            if received_ms < last_now:
                raise ValueError("wall-clock regression cannot enter timed replay")
            while next_decision < received_ms:
                sample(next_decision, last_sequence, last_received_ns)
                next_decision += interval
            for arrival in arrivals:
                last_sequence = arrival["sequence"]
                last_received_ns = arrival["received_ns"]
                if arrival["kind"] != "data":
                    continue
                stream = arrival["stream"]
                coin = stream["coin"]
                c_market = c_markets[coin]
                if arrival["venue"] == "coinone":
                    _, channel, data = arrival["normalized"]
                    c_event = c_market.feed(channel, data, received_ms)
                    emitted = a2_markets[coin].feed(channel, data, received_ms)
                    for signal in emitted:
                        if signal.get("sig") != "DIP_SLOWING" or signal.get("shadow"):
                            continue
                        recorded = deepcopy(signal)
                        recorded.update(
                            available_sequence=arrival["sequence"],
                            available_received_ns=arrival["received_ns"],
                            available_ms=received_ms,
                        )
                        pending_a2_signals[coin].append(recorded)
                        last_a2_signal[coin] = (
                            received_ms, arrival["sequence"], arrival["received_ns"],
                        )
                    if c_event is not None:
                        if c_event["kind"] == "trade" and not c_event["buy"]:
                            last_sell_arrival[coin] = (
                                received_ms, arrival["sequence"], arrival["received_ns"],
                            )
                        for probe in probes:
                            if probe["attempt"].a["coin"] == coin:
                                probe["attempt"].event(c_event)
                        for probe in diagnostic_probes:
                            if probe.action["coin"] == coin:
                                probe.event(c_event)
                elif arrival["normalized"][0] == "b":
                    c_market.reference.quote(arrival["normalized"])
            probes = _finish(probes, outcomes, received_ms)
            if next_decision == received_ms and received_ms < session.completed_ms:
                sample(next_decision, last_sequence, last_received_ns)
                next_decision += interval
            last_now = received_ms

        while next_decision < session.completed_ms:
            sample(next_decision, last_sequence, last_received_ns)
            next_decision += interval
        probes = _finish(probes, outcomes, session.completed_ms, boundary=True)
        if probes:
            raise AssertionError("session boundary left active counterfactuals")
        diagnostic_outcomes.extend(
            probe.result(session.completed_ms) for probe in diagnostic_probes
        )
        counts["sessions_completed"] += 1

    report = {
        "schema": 1,
        "version": VERSION,
        "status": "COMPLETE",
        "verdict": "RESEARCH_ONLY",
        "orders_enabled": False,
        "exchange_orders": 0,
        "exchange_fills": 0,
        "policy_ranking": "WITHHELD",
        "evaluation_design": exploratory_design(),
        "study": audit,
        "contract": research_contract(a2_config),
        "counts": dict(sorted({
            **{key: 0 for key in (
                "decision_frames", "union_events", "sessions_completed",
                *(f"{name}_{suffix}" for name in POLICIES for suffix in ("selected", "attempted")),
            )},
            **counts,
        }.items())),
        "policies": {
            name: _policy_report(all_candidates, outcomes, name)
            for name in POLICIES
        },
        "diagnostic_fill_markouts": markout_report(diagnostic_outcomes),
        "limitations": [
            "candidate counterfactuals overlap and their cash sums are not portfolio PnL",
            "public queue fills are replay hypotheses, not actual account fills",
            "every recorder reconnect boundary censors inventory and resets market state",
            "a frequency-matched random exclusion control is required before a selection claim",
            "no positive metric grants live approval or changes the frozen C-BTC model",
            "COMPLETE means the research computation finished, not that alpha was validated",
        ],
    }
    return report, all_candidates, outcomes, diagnostic_outcomes


def run(
    session_paths, output, *, a2_config_path=A2_CONFIG,
    registration_path=None,
):
    output = _outside_repository(output)
    output.mkdir(parents=True, exist_ok=False)
    try:
        a2_config = load_a2(a2_config_path)
        study = StudyTape(session_paths)
        source = source_identity()
        evaluation_design = (
            validate_registration(registration_path, a2_config, study)
            if registration_path else exploratory_design()
        )
        opening = {
            "schema": 1,
            "version": VERSION,
            "status": "OPENED_BEFORE_OUTCOMES",
            "orders_enabled": False,
            "source": source,
            "source_digest": digest(source),
            "contract": research_contract(a2_config),
            "study": study.audit(),
            "evaluation_design": evaluation_design,
        }
        _atomic_json(output / "frozen-before-evaluation.json", opening)
        report, candidates, outcomes, diagnostics = evaluate(study, a2_config)
        if source != source_identity():
            raise ValueError("research source changed during evaluation")
        after = [session.observation.data_digest() for session in study.sessions]
        before = [row["data_digest"] for row in opening["study"]["sessions"]]
        if after != before:
            raise ValueError("observation data changed during evaluation")
        report["identity"] = {
            "source_digest": opening["source_digest"],
            "study_digest": opening["study"]["study_digest"],
            "contract_digest": digest(opening["contract"]),
            "registration_digest": evaluation_design["registration_digest"],
        }
        report["evaluation_design"] = evaluation_design
        _atomic_jsonl_gz(output / "candidate-events.jsonl.gz", candidates)
        _atomic_jsonl_gz(output / "attempt-outcomes.jsonl.gz", outcomes)
        _atomic_jsonl_gz(output / "diagnostic-fill-markouts.jsonl.gz", diagnostics)
        _atomic_json(output / "report.json", report)
        return report
    except BaseException as exc:
        _atomic_json(output / "failure.json", {
            "schema": 1,
            "version": VERSION,
            "status": "RESEARCH_ERROR",
            "orders_enabled": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "opening_receipt": (
                "frozen-before-evaluation.json"
                if (output / "frozen-before-evaluation.json").is_file()
                else None
            ),
        })
        raise
