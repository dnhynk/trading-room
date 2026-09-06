"""Evaluate a Track A-2 observation and optionally emit a pinned approval artifact."""
import argparse
import hashlib
import json
import re
import time

from track_a_2 import EXECUTION_VERSION
from track_a_2.execution.preflight import (
    evaluation_config_digest,
    evaluation_source_digest,
)
from track_a_2.execution.store import encoded
from track_a_2.replay.engine import run_observation
from track_a_2.replay.loader import Observation
from track_a_2.settings import CONFIG, ROOT, load, resolved_state_directory
from track_c.execution.coinone import decimal


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("observation")
    result.add_argument("--config", default=str(CONFIG))
    result.add_argument("--capital", default="1000000")
    result.add_argument("--maker-fee")
    result.add_argument("--taker-fee")
    result.add_argument("--latency-ms", type=int, default=250)
    result.add_argument("--depth-fraction")
    result.add_argument("--approval-id", required=True)
    result.add_argument("--holdout", action="store_true")
    result.add_argument(
        "--approve", action="store_true",
        help="record the user's approval only when holdout and stress gates pass",
    )
    return result


def main():
    args = parser().parse_args()
    if not re.fullmatch(r"a2-eval-[a-z0-9_.-]{4,80}", args.approval_id):
        raise SystemExit("invalid --approval-id")
    if args.latency_ms < 0:
        raise SystemExit("--latency-ms must be non-negative")
    config = load(args.config, root=ROOT)
    observation = Observation(args.observation)
    data_digest = observation.data_digest()
    assumed = observation.manifest["fee_assumption"]
    maker = args.maker_fee if args.maker_fee is not None else assumed["maker"]
    taker = args.taker_fee if args.taker_fee is not None else assumed["taker"]
    depth = args.depth_fraction or str(config["depth_fraction"])
    capital = decimal(args.capital, positive=True)
    main_result = run_observation(
        observation, config, capital=str(capital), maker_fee=maker, taker_fee=taker,
        latency_ms=args.latency_ms, depth_fraction=depth,
    )
    one_unit = run_observation(
        observation, config, capital=str(capital), maker_fee=maker, taker_fee=taker,
        latency_ms=args.latency_ms, depth_fraction=depth,
        strategy={"max_units": 1},
    )
    fee_stress = max(decimal(maker), decimal(taker), decimal(config["max_fee_rate"]))
    stressed = run_observation(
        observation, config, capital=str(capital),
        maker_fee=str(fee_stress), taker_fee=str(fee_stress),
        latency_ms=max(args.latency_ms * 2, args.latency_ms + 1),
        depth_fraction=str(decimal(depth) / 2),
    )
    if (
        observation.data_digest() != data_digest
        or any(
            result.get("data_digest") != data_digest
            for result in (main_result, one_unit, stressed)
        )
    ):
        raise SystemExit("observation changed during evaluation")
    quality = observation.connection_quality()
    stress_passed = decimal(stressed["net_pnl_krw"]) > 0 and stressed["halt"] is None
    ladder_ok = (
        config["strategy"]["max_units"] == 1
        or decimal(main_result["net_pnl_krw"]) > decimal(one_unit["net_pnl_krw"])
    )
    same_universe = observation.manifest["coins"] == config["universe"]
    approvable = bool(
        args.holdout
        and quality["contiguous"]
        and decimal(main_result["net_pnl_krw"]) > 0
        and stress_passed
        and ladder_ok
        and same_universe
        and main_result["campaigns"] > 0
        and main_result["halt"] is None
    )
    if args.approve and not approvable:
        raise SystemExit(
            "approval refused: continuity, holdout, profit, stress, ladder, "
            "universe, campaign, or halt gate failed"
        )
    manifest = dict(
        schema=1,
        track="A-2",
        approval_id=args.approval_id,
        execution_version=EXECUTION_VERSION,
        config_digest=evaluation_config_digest(config),
        source_digest=evaluation_source_digest(ROOT),
        universe=config["universe"],
        data_digest=data_digest,
        result="pass" if args.approve else "research",
        created_ms=time.time_ns() // 1_000_000,
        protocol=dict(
            holdout=bool(args.holdout),
            feed_contiguous=quality["contiguous"],
            stress_passed=stress_passed,
            ladder_beats_one_unit=ladder_ok,
            fixed_selection_from_seed=True,
            user_approved=bool(args.approve),
        ),
        execution=dict(
            maker_fee=str(decimal(maker)), taker_fee=str(decimal(taker)),
            latency_ms=args.latency_ms, depth_fraction=str(decimal(depth)),
        ),
        metrics=dict(main=main_result, one_unit=one_unit, stress=stressed),
        observation_quality=quality,
    )
    raw = (encoded(manifest) + "\n").encode("utf-8")
    relative = f"evaluations/{args.approval_id}.json"
    path = resolved_state_directory(config, ROOT) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as target:
            target.write(raw)
    except FileExistsError:
        raise SystemExit("evaluation artifact already exists") from None
    print(encoded(dict(
        track="A-2", result=manifest["result"], path=str(path),
        evaluation_manifest=relative,
        evaluation_sha256=hashlib.sha256(raw).hexdigest(),
        approvable=approvable,
    )))


if __name__ == "__main__":
    main()
