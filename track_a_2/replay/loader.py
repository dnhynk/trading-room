"""Validated reader for credential-free Track A-2 observations."""
import gzip
import hashlib
import json
from pathlib import Path
import re


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Observation:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        try:
            self.manifest_raw = (self.folder / "manifest.json").read_bytes()
            self.manifest = json.loads(self.manifest_raw)
            seed_name = self.manifest["seed_file"]
            if Path(seed_name).name != seed_name:
                raise ValueError
            self.seed_raw = (self.folder / seed_name).read_bytes()
            self.seed = json.loads(self.seed_raw)
        except (OSError, KeyError, TypeError, ValueError):
            raise ValueError("invalid Track A-2 observation") from None
        coins = self.manifest.get("coins")
        captured = self.seed.get("captured_ms")
        fee = self.manifest.get("fee_assumption")
        completed = self.manifest.get("completed_ms")
        message_count = self.manifest.get("message_count")
        if (
            self.manifest.get("schema") not in (1, 2)
            or self.manifest.get("track") != "A-2"
            or self.manifest.get("format") not in ("coinone-public-v1", "coinone-public-v2")
            or self.seed.get("schema") != 1
            or not isinstance(coins, list)
            or not coins
            or len(coins) != len(set(coins))
            or any(not isinstance(coin, str) or not coin for coin in coins)
            or not isinstance(captured, int)
            or captured < 0
            or not isinstance(completed, int)
            or completed < captured
            or not isinstance(message_count, int)
            or message_count < 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(self.manifest.get("public_sha256") or ""))
            or not isinstance(fee, dict)
            or not {"maker", "taker", "source"} <= set(fee)
            or hashlib.sha256(self.seed_raw).hexdigest() != self.manifest.get("seed_sha256")
            or set(coins) != set(self.seed.get("markets") or {})
        ):
            raise ValueError("Track A-2 observation identity mismatch")
        self.public = self.folder / "public.jsonl.gz"
        if not self.public.is_file():
            raise ValueError("Track A-2 observation public feed missing")
        try:
            if _file_sha256(self.public) != self.manifest["public_sha256"]:
                raise ValueError("Track A-2 observation public feed digest mismatch")
        except OSError:
            raise ValueError("Track A-2 observation public feed unavailable") from None

    def envelopes(self):
        previous = -1
        count = 0
        try:
            with gzip.open(self.public, "rt", encoding="utf-8") as source:
                for line in source:
                    envelope = json.loads(line)
                    received = int(envelope["received_ms"])
                    if received < previous or received < self.seed["captured_ms"]:
                        raise ValueError("nonmonotonic observation receive time")
                    previous = received
                    if not isinstance(envelope, dict):
                        raise ValueError("invalid public observation envelope")
                    count += 1
                    yield envelope
            if count != self.manifest["message_count"]:
                raise ValueError("Track A-2 observation message count mismatch")
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("invalid Track A-2 observation feed") from None

    def rows(self):
        for envelope in self.envelopes():
            if "raw" not in envelope:
                continue
            if not isinstance(envelope["raw"], str):
                raise ValueError("invalid public websocket payload")
            message = json.loads(envelope["raw"])
            if not isinstance(message, dict):
                raise ValueError("invalid public websocket message")
            yield int(envelope["received_ms"]), message

    def connection_quality(self):
        envelopes = list(self.envelopes())
        events = [row.get("event") for row in envelopes if "event" in row]
        socket_open = sum(event in ("SOCKET_OPEN", "CONNECTED") for event in events)
        disconnected = sum(event == "DISCONNECTED" for event in events)
        ping_sent = sum(event == "PING_SENT" for event in events)
        server_connected = 0
        pongs = 0
        errors = 0
        subscribed = set()
        received = {}
        channels = tuple(
            (self.manifest.get("quality_policy") or {}).get("channels")
            or ("ORDERBOOK", "TRADE")
        )
        expected = {
            (coin, channel) for coin in self.manifest["coins"] for channel in channels
        }
        for envelope in envelopes:
            raw = envelope.get("raw")
            if raw is None:
                continue
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                errors += 1
                continue
            if not isinstance(message, dict):
                errors += 1
                continue
            kind = message.get("response_type")
            if kind == "CONNECTED":
                server_connected += 1
            elif kind == "PONG":
                pongs += 1
            elif kind == "ERROR":
                errors += 1
            data = message.get("data") or {}
            pair = (data.get("target_currency"), message.get("channel"))
            if pair not in expected:
                continue
            if kind == "SUBSCRIBED":
                subscribed.add(pair)
            elif kind == "DATA":
                received.setdefault(pair, []).append(int(envelope["received_ms"]))
        start = min(
            (
                int(row["received_ms"])
                for row in envelopes
                if row.get("event") in ("SOCKET_OPEN", "CONNECTED")
            ),
            default=self.seed["captured_ms"],
        )
        completed = int(self.manifest["completed_ms"])
        max_allowed = int(
            (self.manifest.get("quality_policy") or {}).get("max_data_gap_ms")
            or 120_000
        )
        gap_by_stream = {}
        for pair in expected:
            times = sorted(received.get(pair, ()))
            if not times:
                gap_by_stream[":".join(pair)] = None
                continue
            gaps = [max(0, times[0] - start), max(0, completed - times[-1])]
            gaps.extend(max(0, right - left) for left, right in zip(times, times[1:]))
            gap_by_stream[":".join(pair)] = max(gaps)
        coverage_complete = set(received) == expected
        subscriptions_complete = subscribed == expected
        gaps_ok = coverage_complete and all(
            gap is not None and gap <= max_allowed for gap in gap_by_stream.values()
        )
        duration_ms = max(0, completed - start)
        ping_interval_ms = int(
            float((self.manifest.get("quality_policy") or {}).get("ping_interval_s") or 60)
            * 1000
        )
        heartbeat_ok = duration_ms <= ping_interval_ms or (ping_sent > 0 and pongs >= ping_sent)
        contiguous = bool(
            socket_open == 1
            and server_connected == 1
            and disconnected == 0
            and errors == 0
            and subscriptions_complete
            and coverage_complete
            and gaps_ok
            and heartbeat_ok
        )
        return dict(
            socket_open=socket_open,
            server_connected=server_connected,
            disconnected=disconnected,
            errors=errors,
            subscriptions=len(subscribed),
            expected_streams=len(expected),
            data_streams=len(received),
            data_messages=sum(len(rows) for rows in received.values()),
            coverage_complete=coverage_complete,
            subscriptions_complete=subscriptions_complete,
            max_data_gap_ms=gap_by_stream,
            gap_limit_ms=max_allowed,
            gaps_ok=gaps_ok,
            ping_sent=ping_sent,
            pongs=pongs,
            heartbeat_ok=heartbeat_ok,
            contiguous=contiguous,
        )

    def data_digest(self):
        digest = hashlib.sha256()
        digest.update(len(self.manifest_raw).to_bytes(8, "big"))
        digest.update(self.manifest_raw)
        digest.update(len(self.seed_raw).to_bytes(8, "big"))
        digest.update(self.seed_raw)
        try:
            with gzip.open(self.public, "rb") as source:
                for line in source:
                    digest.update(len(line).to_bytes(8, "big"))
                    digest.update(line)
        except OSError:
            raise ValueError("invalid Track A-2 observation feed") from None
        return digest.hexdigest()
