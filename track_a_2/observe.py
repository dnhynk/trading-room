"""Credential-free Coinone public recorder for Track A-2 research.

Example:
    python -m track_a_2.observe --coin BTC --coin ETH --seconds 3600

Only public REST and websocket endpoints are used. Outputs live below the
dedicated Track A-2 state directory, never in the repository.
"""
import argparse
import asyncio
import gzip
import hashlib
import json
from pathlib import Path
import re
import time

from track_a_2.execution.client import CoinoneA2
from track_a_2.execution.preflight import (
    evaluation_config_digest,
    evaluation_source_digest,
)
from track_a_2.execution.store import encoded
from track_a_2.settings import CONFIG, ROOT, load, resolved_state_directory
from track_c.execution.coinone import CoinoneError, symbol
from track_c.execution.http_pool import HTTPSPool
from track_c.execution.rate_limit import Transport


def coins(values):
    result = [symbol(str(value).upper()) for value in values]
    if not result or len(result) > 20 or len(set(result)) != len(result):
        raise ValueError("one to twenty unique Coinone symbols are required")
    return result


def _json_bytes(value):
    return (encoded(value) + "\n").encode("utf-8")


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_observation(config, selected, client, directory, *, now_ms=None, session=None):
    """Capture causal seed/contract inputs before websocket recording starts."""
    selected = coins(selected)
    now_ms = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    session = session or time.strftime("%Y%m%d-%H%M%S", time.gmtime(now_ms / 1000))
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{4,80}", session):
        raise ValueError("invalid observation session name")
    folder = Path(directory).resolve() / "observations" / session
    if folder.exists():
        raise FileExistsError("observation session already exists")
    contracts, tickers = client.universe()
    by_contract = {row.get("target_currency"): row for row in contracts}
    by_ticker = {row.get("target_currency"): row for row in tickers}
    markets = {}
    for coin in selected:
        contract = by_contract.get(coin)
        ticker = by_ticker.get(coin)
        if contract is None or ticker is None:
            raise CoinoneError(f"public market snapshot unavailable for {coin}")
        markets[coin] = dict(
            contract=contract,
            ticker=ticker,
            units=client.price_units(coin),
            orderbook=client.orderbook(coin),
            candles={
                interval: client.candles(coin, interval, size)
                for interval, size in (("1m", 500), ("15m", 500), ("1d", 400))
            },
        )
    folder.mkdir(parents=True, exist_ok=False)
    seed = dict(schema=1, captured_ms=now_ms, markets=markets)
    seed_raw = _json_bytes(seed)
    seed_path = folder / "seed.json"
    seed_path.write_bytes(seed_raw)
    manifest = dict(
        schema=1,
        track="A-2",
        format="coinone-public-v1",
        session=session,
        created_ms=now_ms,
        coins=selected,
        seed_file=seed_path.name,
        seed_sha256=hashlib.sha256(seed_raw).hexdigest(),
        config_digest=evaluation_config_digest(config),
        source_digest=evaluation_source_digest(ROOT),
        fee_assumption=dict(
            maker=str(config["max_fee_rate"]),
            taker=str(config["max_fee_rate"]),
            source="configured_ceiling_without_credentials",
        ),
    )
    (folder / "manifest.json").write_bytes(_json_bytes(manifest))
    return folder, manifest


class Capture:
    def __init__(self, folder, config, selected):
        self.folder = Path(folder)
        self.config = config
        self.coins = coins(selected)
        self.path = self.folder / "public.jsonl.gz"
        self.file = gzip.open(self.path, "at", encoding="utf-8")
        self.count = 0
        self.closed = False

    def write(self, *, received_ms=None, raw=None, event=None, fields=None):
        received_ms = time.time_ns() // 1_000_000 if received_ms is None else int(received_ms)
        row = dict(received_ms=received_ms)
        if raw is not None:
            row["raw"] = raw
        else:
            row.update(event=event, fields=fields or {})
        self.file.write(encoded(row) + "\n")
        self.count += 1
        if self.count % 100 == 0:
            self.file.flush()
        if self.path.stat().st_size > self.config["public_storage_max_bytes"]:
            raise OSError("Track A-2 observation storage cap reached")

    def close(self):
        if self.closed:
            return
        self.file.close()
        self.closed = True
        manifest_path = self.folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            completed_ms=max(
                time.time_ns() // 1_000_000,
                int(manifest.get("created_ms") or 0),
            ),
            message_count=self.count,
            public_sha256=_file_sha256(self.path),
        )
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_bytes(_json_bytes(manifest))
        temporary.replace(manifest_path)


async def record_public(capture, seconds=None):
    from websockets.asyncio.client import connect

    started = time.monotonic()
    backoff = 1
    while seconds is None or time.monotonic() - started < seconds:
        try:
            capture.write(event="CONNECTING")
            async with connect(
                "wss://stream.coinone.co.kr",
                open_timeout=10,
                ping_interval=15,
                ping_timeout=15,
                close_timeout=3,
                max_queue=4096,
            ) as websocket:
                capture.write(event="CONNECTED")
                for coin in capture.coins:
                    for channel in ("ORDERBOOK", "TRADE"):
                        await websocket.send(encoded(dict(
                            request_type="SUBSCRIBE",
                            channel=channel,
                            topic=dict(quote_currency="KRW", target_currency=coin),
                        )))
                while seconds is None or time.monotonic() - started < seconds:
                    timeout = 1.0
                    if seconds is not None:
                        timeout = min(timeout, max(0.001, seconds - (time.monotonic() - started)))
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout)
                    except asyncio.TimeoutError:
                        continue
                    capture.write(raw=raw)
                return
        except OSError as exc:
            capture.write(event="DISCONNECTED", fields={"error": type(exc).__name__})
            if "storage cap" in str(exc):
                raise
        except Exception as exc:
            capture.write(event="DISCONNECTED", fields={"error": type(exc).__name__})
        remaining = None if seconds is None else seconds - (time.monotonic() - started)
        if remaining is not None and remaining <= 0:
            return
        await asyncio.sleep(min(backoff, remaining) if remaining is not None else backoff)
        backoff = min(30, backoff * 2)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", default=str(CONFIG))
    result.add_argument("--coin", action="append", required=True, dest="coins")
    result.add_argument("--seconds", type=float)
    result.add_argument("--session")
    return result


def main():
    args = parser().parse_args()
    if args.seconds is not None and args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    pool = None
    capture = None
    try:
        config = load(args.config, root=ROOT)
        selected = coins(args.coins)
        pool = HTTPSPool()
        # No credential lookup: all methods used during preparation are public.
        client = CoinoneA2(None, transport=Transport(pool), timeout=float(config["http_timeout_s"]))
        folder, _ = prepare_observation(
            config, selected, client, resolved_state_directory(config),
            session=args.session,
        )
        capture = Capture(folder, config, selected)
        asyncio.run(record_public(capture, args.seconds))
        print(encoded(dict(track="A-2", observation=str(folder), messages=capture.count)))
    except (CoinoneError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    finally:
        if capture:
            capture.close()
        if pool:
            pool.close()


if __name__ == "__main__":
    main()
