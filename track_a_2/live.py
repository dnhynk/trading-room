"""Start the guarded Track A-2 Coinone live owner."""
import argparse
import asyncio
import ipaddress
import json
import signal
import urllib.request

from track_a_2.execution.preflight import block_reasons, require_live
from track_a_2.runtime import Runtime
from track_a_2.settings import CONFIG, ROOT, load
from track_c.execution.coinone import CoinoneError, NoRedirect


def egress_ip():
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open("https://checkip.amazonaws.com", timeout=10) as response:
            value = response.read(65).decode("ascii").strip()
        address = ipaddress.ip_address(value)
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError("Track A-2 egress address could not be verified") from None
    if address.version != 4:
        raise RuntimeError("Track A-2 requires a fixed IPv4 egress address")
    return str(address)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", default=str(CONFIG))
    result.add_argument("--seconds", type=float)
    result.add_argument(
        "--check", action="store_true",
        help="validate configuration and local activation locks without credentials or network",
    )
    return result


def main():
    args = parser().parse_args()
    if args.seconds is not None and args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    try:
        config = load(args.config, root=ROOT)
        if args.check:
            reasons = block_reasons(config, root=ROOT)
            print(json.dumps(dict(track="A-2", configuration="valid", live_ready=not reasons, blocks=reasons)))
            return
        # This first gate intentionally runs before credential or runtime-state access.
        require_live(config, root=ROOT)
        address = egress_ip()
        require_live(config, root=ROOT, egress=address)
        runner = Runtime(config, config_path=args.config, root=ROOT)

        def stop(*_):
            runner.stopping = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        asyncio.run(runner.run(args.seconds))
    except (CoinoneError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
