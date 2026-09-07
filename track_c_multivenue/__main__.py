"""Audit or compare finalized C multi-venue research sessions."""
import argparse
import json

from track_a_2.settings import CONFIG as A2_CONFIG, load as load_a2
from track_c_multivenue.compare import run
from track_c_multivenue.input import StudyTape
from track_c_multivenue.registration import create_registration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append")
    parser.add_argument("--output", help="new output directory outside the repository")
    parser.add_argument("--a2-config", default=str(A2_CONFIG))
    parser.add_argument("--registration", help="future-window registration for an evaluation run")
    parser.add_argument("--register-output", help="create an immutable future-window registration")
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    args = parser.parse_args()
    if args.register_output:
        if args.session or args.output or args.registration:
            parser.error("registration creation cannot also audit or evaluate sessions")
        if args.start_ms is None or args.end_ms is None:
            parser.error("registration creation requires --start-ms and --end-ms")
        document = create_registration(
            args.register_output, load_a2(args.a2_config),
            args.start_ms, args.end_ms,
        )
        print(json.dumps({
            "status": document["status"],
            "version": document["version"],
            "registration_digest": document["registration_digest"],
            "orders_enabled": document["orders_enabled"],
        }, ensure_ascii=False, separators=(",", ":")))
        return
    if not args.session:
        parser.error("at least one --session is required")
    if args.start_ms is not None or args.end_ms is not None:
        parser.error("--start-ms and --end-ms are only valid with --register-output")
    if args.output:
        report = run(
            args.session, args.output, a2_config_path=args.a2_config,
            registration_path=args.registration,
        )
        print(json.dumps({
            "status": report["status"],
            "verdict": report["verdict"],
            "orders_enabled": report["orders_enabled"],
            "policies": {
                key: value["common_attempts"]
                for key, value in report["policies"].items()
            },
        }, ensure_ascii=False, separators=(",", ":")))
    else:
        if args.registration:
            parser.error("--registration is only valid with --output")
        audit = StudyTape(args.session).audit()
        print(json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
