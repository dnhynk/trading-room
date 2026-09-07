"""Credential-free external KRW benchmark recording for Track A-2.

The recorder preserves the exact public websocket payload plus process-local
arrival order.  It never imports account or order clients.
"""
import asyncio
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import time

from track_a_2.execution.store import encoded
from track_a_2.observe import (
    MAX_DATA_GAP_MS, PONG_TIMEOUT_S, coins, observation_storage_bytes,
)


WS = {
    "upbit": "wss://api.upbit.com/websocket/v1",
    "bithumb": "wss://ws-api.bithumb.com/websocket/v1",
}
CHANNELS = ("ORDERBOOK", "TRADE")
CONTROL_PING_INTERVAL_S = 20.0
FIRST_DATA_TIMEOUT_S = 60.0


class ExternalFeedError(RuntimeError):
    pass


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stream_fields(venue, message):
    """Return causal indexing fields without replacing the preserved payload."""
    if venue not in WS or not isinstance(message, dict):
        raise ValueError("invalid external websocket message")
    if message.get("error") is not None:
        raise ExternalFeedError("external subscription rejected")
    kind = str(message.get("type") or "").upper()
    code = str(message.get("code") or "").upper()
    if kind not in CHANNELS or not code.startswith("KRW-"):
        return None
    coin = code[4:]
    timestamp = (
        message.get("trade_timestamp")
        if kind == "TRADE"
        else message.get("timestamp")
    )
    if isinstance(timestamp, bool):
        raise ValueError("invalid external exchange timestamp")
    exchange_ms = int(timestamp)
    if venue == "bithumb" and kind == "ORDERBOOK" and exchange_ms > 10**14:
        exchange_ms //= 1000
    if exchange_ms < 0:
        raise ValueError("invalid external exchange timestamp")
    return dict(coin=coin, channel=kind, exchange_ms=exchange_ms)


class ExternalCapture:
    def __init__(self, folder, config, selected, venues, *, clock):
        self.folder = Path(folder)
        self.config = config
        self.coins = coins(selected)
        self.venues = list(venues)
        if not self.venues or len(self.venues) != len(set(self.venues)):
            raise ValueError("one or more unique external venues are required")
        if any(venue not in WS for venue in self.venues):
            raise ValueError("unsupported external benchmark venue")
        self.path = self.folder / "external.jsonl.gz"
        self.file = gzip.open(self.path, "at", encoding="utf-8")
        self.clock = clock
        self.count = 0
        self.closed = False
        self.last_received_ms = 0
        self.counts = Counter()

    def write(
        self, venue, *, received_ms=None, received_ns=None, raw=None,
        event=None, fields=None, stream=None,
    ):
        if venue not in self.venues:
            raise ValueError("unconfigured external benchmark venue")
        row = self.clock.stamp(received_ms=received_ms, received_ns=received_ns)
        row["venue"] = venue
        self.last_received_ms = max(self.last_received_ms, row["received_ms"])
        if raw is not None:
            if not isinstance(raw, str):
                raise ValueError("external websocket payload must be text")
            row["raw"] = raw
        if event is not None:
            row["event"] = event
            row["fields"] = fields or {}
            self.counts[f"{venue}:{event}"] += 1
        if stream is not None:
            row["stream"] = stream
            self.counts[f"{venue}:{stream['channel']}"] += 1
        self.file.write(encoded(row) + "\n")
        self.count += 1
        if self.count % 100 == 0:
            self.file.flush()
        if (
            self.count % 1000 == 0
            and observation_storage_bytes(self.folder.parent)
            > self.config["public_storage_max_bytes"]
        ):
            raise OSError("Track A-2 observation storage cap reached")

    def close(self):
        if self.closed:
            return
        self.file.close()
        self.closed = True
        manifest_path = self.folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        completed_ms = max(
            time.time_ns() // 1_000_000,
            int(manifest.get("created_ms") or 0),
            self.last_received_ms,
        )
        metadata = dict(
            schema=1,
            format="a2-external-public-v1",
            file=self.path.name,
            sha256=_file_sha256(self.path),
            message_count=self.count,
            completed_ms=completed_ms,
            venues=self.venues,
            coins=self.coins,
            markets={
                venue: [f"KRW-{coin}" for coin in self.coins]
                for venue in self.venues
            },
            channels=list(CHANNELS),
            quality_policy=dict(
                control_ping_interval_s=CONTROL_PING_INTERVAL_S,
                pong_timeout_s=PONG_TIMEOUT_S,
                first_data_timeout_s=FIRST_DATA_TIMEOUT_S,
                max_data_gap_ms=MAX_DATA_GAP_MS,
            ),
            counts=dict(self.counts),
        )
        metadata["quality"] = external_quality(self.path, metadata)
        manifest["external_capture"] = metadata
        manifest["completed_ms"] = max(
            int(manifest.get("completed_ms") or 0), completed_ms,
        )
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(encoded(manifest) + "\n", encoding="utf-8")
        temporary.replace(manifest_path)


def external_envelopes(path, expected_count=None):
    previous_sequence = 0
    count = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("invalid external observation envelope")
                sequence = row.get("sequence")
                received_ns = row.get("received_ns")
                monotonic_ns = row.get("monotonic_ns")
                if (
                    isinstance(sequence, bool) or not isinstance(sequence, int)
                    or sequence <= previous_sequence
                    or isinstance(received_ns, bool) or not isinstance(received_ns, int)
                    or received_ns < 0
                    or isinstance(monotonic_ns, bool) or not isinstance(monotonic_ns, int)
                    or monotonic_ns < 0
                    or row.get("received_ms") != received_ns // 1_000_000
                    or row.get("venue") not in WS
                ):
                    raise ValueError("invalid external observation arrival identity")
                previous_sequence = sequence
                count += 1
                yield row
        if expected_count is not None and count != expected_count:
            raise ValueError("external observation message count mismatch")
    except (OSError, TypeError, json.JSONDecodeError):
        raise ValueError("invalid external observation feed") from None


def external_quality(path, metadata):
    venues = tuple(metadata["venues"])
    expected = {
        (venue, coin, channel)
        for venue in venues
        for coin in metadata["coins"]
        for channel in metadata["channels"]
    }
    opens = Counter()
    disconnects = Counter()
    completed = Counter()
    server_errors = Counter()
    subscriptions = Counter()
    pings = Counter()
    pongs = Counter()
    start = {}
    finish = {}
    received = {}
    invalid = 0
    for row in external_envelopes(path):
        venue = row["venue"]
        at = row["received_ms"]
        event = row.get("event")
        if event == "SOCKET_OPEN":
            opens[venue] += 1
            start.setdefault(venue, at)
        elif event == "DISCONNECTED":
            disconnects[venue] += 1
        elif event == "COMPLETED":
            completed[venue] += 1
            finish[venue] = at
        elif event == "SERVER_ERROR":
            server_errors[venue] += 1
        elif event == "SUBSCRIPTION_SENT":
            subscriptions[venue] += 1
        elif event == "CONTROL_PING_SENT":
            pings[venue] += 1
        elif event == "CONTROL_PONG":
            pongs[venue] += 1
        stream = row.get("stream")
        if stream is None:
            continue
        try:
            pair = (venue, stream["coin"], stream["channel"])
            exchange_ms = stream["exchange_ms"]
            if (
                pair not in expected
                or isinstance(exchange_ms, bool)
                or not isinstance(exchange_ms, int)
                or exchange_ms < 0
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        received.setdefault(pair, []).append(at)
    max_gap = int(metadata["quality_policy"]["max_data_gap_ms"])
    ping_interval_ms = int(
        float(metadata["quality_policy"]["control_ping_interval_s"]) * 1000
    )
    gap_by_stream = {}
    for pair in expected:
        times = received.get(pair, ())
        key = ":".join(pair)
        if not times or pair[0] not in start:
            gap_by_stream[key] = None
            continue
        end = finish.get(pair[0], int(metadata["completed_ms"]))
        gaps = [max(0, times[0] - start[pair[0]]), max(0, end - times[-1])]
        gaps.extend(max(0, right - left) for left, right in zip(times, times[1:]))
        gap_by_stream[key] = max(gaps)
    by_venue = {}
    for venue in venues:
        venue_expected = {pair for pair in expected if pair[0] == venue}
        venue_received = {pair for pair in received if pair[0] == venue}
        duration = max(0, finish.get(venue, int(metadata["completed_ms"])) - start.get(venue, int(metadata["completed_ms"])))
        coverage = venue_received == venue_expected
        gaps_ok = coverage and all(
            gap_by_stream[":".join(pair)] is not None
            and gap_by_stream[":".join(pair)] <= max_gap
            for pair in venue_expected
        )
        heartbeat = duration <= ping_interval_ms or (
            pings[venue] > 0 and pongs[venue] >= pings[venue]
        )
        contiguous = bool(
            opens[venue] == 1
            and disconnects[venue] == 0
            and completed[venue] == 1
            and server_errors[venue] == 0
            and subscriptions[venue] == 1
            and coverage
            and gaps_ok
            and heartbeat
        )
        by_venue[venue] = dict(
            socket_open=opens[venue],
            disconnected=disconnects[venue],
            completed=completed[venue],
            server_errors=server_errors[venue],
            subscription_sent=subscriptions[venue],
            expected_streams=len(venue_expected),
            data_streams=len(venue_received),
            data_messages=sum(len(received.get(pair, ())) for pair in venue_expected),
            coverage_complete=coverage,
            gaps_ok=gaps_ok,
            ping_sent=pings[venue],
            pongs=pongs[venue],
            heartbeat_ok=heartbeat,
            contiguous=contiguous,
        )
    return dict(
        venues=by_venue,
        invalid_streams=invalid,
        max_data_gap_ms=gap_by_stream,
        contiguous=invalid == 0 and all(row["contiguous"] for row in by_venue.values()),
    )


async def record_external(
    capture, venue, seconds=None, *, connector=None,
    ping_interval_s=CONTROL_PING_INTERVAL_S,
    pong_timeout_s=PONG_TIMEOUT_S,
    first_data_timeout_s=FIRST_DATA_TIMEOUT_S,
    stop_event=None,
):
    """Record one public external venue with bounded reconnects and coverage checks."""
    if venue not in capture.venues:
        raise ValueError("unconfigured external benchmark venue")
    if connector is None:
        from websockets.asyncio.client import connect as connector
    started = time.monotonic()
    running = lambda: (
        (stop_event is None or not stop_event.is_set())
        and (seconds is None or time.monotonic() - started < seconds)
    )
    expected = {
        (coin, channel) for coin in capture.coins for channel in CHANNELS
    }
    codes = [f"KRW-{coin}" for coin in capture.coins]
    backoff = 1.0
    while running():
        capture.write(venue, event="CONNECTING")
        try:
            async with connector(
                WS[venue],
                open_timeout=15,
                ping_interval=None,
                close_timeout=3,
                max_queue=4096,
                max_size=2**22,
            ) as websocket:
                capture.write(venue, event="SOCKET_OPEN")
                request = [
                    {"ticket": f"trading-room-a2-{venue}-{capture.folder.name}"},
                    {"type": "orderbook", "codes": codes},
                    {"type": "trade", "codes": codes},
                    {"format": "DEFAULT"},
                ]
                await websocket.send(json.dumps(request, separators=(",", ":")))
                capture.write(
                    venue, event="SUBSCRIPTION_SENT",
                    fields={"markets": codes, "expected_streams": len(expected)},
                )
                opened = time.monotonic()
                next_ping = opened + ping_interval_s
                seen = set()
                while running():
                    now = time.monotonic()
                    if now - opened >= first_data_timeout_s and seen != expected:
                        raise ExternalFeedError("external initial data coverage unavailable")
                    deadlines = [now + 1.0, next_ping]
                    if seen != expected:
                        deadlines.append(opened + first_data_timeout_s)
                    if seconds is not None:
                        deadlines.append(started + seconds)
                    timeout = max(0.001, min(deadlines) - now)
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None:
                        received_ns = time.time_ns()
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        try:
                            message = json.loads(raw)
                            stream = stream_fields(venue, message)
                        except ExternalFeedError:
                            capture.write(venue, received_ns=received_ns, raw=raw)
                            capture.write(venue, event="SERVER_ERROR")
                            raise
                        except (TypeError, ValueError, json.JSONDecodeError):
                            capture.write(venue, received_ns=received_ns, raw=raw)
                            raise ExternalFeedError("invalid external websocket payload") from None
                        capture.write(
                            venue, received_ns=received_ns, raw=raw, stream=stream,
                        )
                        if stream is not None and stream["coin"] in capture.coins:
                            seen.add((stream["coin"], stream["channel"]))
                    now = time.monotonic()
                    if running() and now >= next_ping:
                        capture.write(venue, event="CONTROL_PING_SENT")
                        pong_waiter = await websocket.ping()
                        await asyncio.wait_for(pong_waiter, pong_timeout_s)
                        capture.write(venue, event="CONTROL_PONG")
                        next_ping = time.monotonic() + ping_interval_s
                capture.write(
                    venue, event="COMPLETED",
                    fields={
                        "data_streams": len(seen),
                        "expected_streams": len(expected),
                    },
                )
                return
        except OSError as exc:
            capture.write(venue, event="DISCONNECTED", fields={"error": type(exc).__name__})
            if "storage cap" in str(exc):
                raise
        except Exception as exc:
            capture.write(venue, event="DISCONNECTED", fields={"error": type(exc).__name__})
        if not running():
            return
        remaining = None if seconds is None else seconds - (time.monotonic() - started)
        delay = backoff if remaining is None else min(backoff, max(0.0, remaining))
        waited = 0.0
        while running() and waited < delay:
            step = min(0.5, delay - waited)
            await asyncio.sleep(step)
            waited += step
        backoff = min(30.0, backoff * 2)
