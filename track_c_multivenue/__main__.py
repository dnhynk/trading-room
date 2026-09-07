"""Audit or compare finalized C multi-venue research sessions."""
import argparse
import json

from track_a_2.settings import CONFIG as A2_CONFIG
from track_c_multivenue.compare import run
from track_c_multivenue.input import StudyTape


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", required=True)
    parser.add_argument("--output", help="new output directory outside the repository")
    parser.add_argument("--a2-config", default=str(A2_CONFIG))
    args = parser.parse_args()
    if args.output:
        report = run(args.session, args.output, a2_config_path=args.a2_config)
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
        audit = StudyTape(args.session).audit()
        print(json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
