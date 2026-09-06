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
            self.manifest.get("schema") != 1
            or self.manifest.get("track") != "A-2"
            or self.manifest.get("format") != "coinone-public-v1"
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
        events = [row.get("event") for row in self.envelopes() if "event" in row]
        connected = sum(event == "CONNECTED" for event in events)
        disconnected = sum(event == "DISCONNECTED" for event in events)
        return dict(
            connected=connected,
            disconnected=disconnected,
            contiguous=connected == 1 and disconnected == 0,
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
