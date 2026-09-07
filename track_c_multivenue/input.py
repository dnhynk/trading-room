"""Lossless, sequence-first adapter for finalized A-2 multi-venue sessions."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from track_a_2.external import stream_fields
from track_a_2.replay.loader import Observation
from track_c.market.leaders import parse as parse_leader
from track_c_multivenue.contract import COINS, EXTERNAL_VENUES, digest


def _coinone_stream(message):
    if not isinstance(message, dict) or message.get("response_type") != "DATA":
        return None
    channel = message.get("channel")
    data = message.get("data")
    if channel not in ("ORDERBOOK", "TRADE") or not isinstance(data, dict):
        raise ValueError("invalid Coinone data message")
    coin = data.get("target_currency")
    if data.get("quote_currency") != "KRW" or coin not in COINS:
        raise ValueError("unexpected Coinone market")
    timestamp = data.get("timestamp")
    if isinstance(timestamp, bool):
        raise ValueError("invalid Coinone exchange timestamp")
    exchange_ms = int(timestamp)
    if exchange_ms < 0:
        raise ValueError("invalid Coinone exchange timestamp")
    return {"coin": coin, "channel": channel, "exchange_ms": exchange_ms}


class Session:
    """One recorder process lifetime. Sequence is local to this session."""

    def __init__(self, folder):
        self.observation = Observation(folder)
        self.folder = self.observation.folder
        manifest = self.observation.manifest
        external = manifest.get("external_capture") or {}
        if (
            manifest.get("schema") != 2
            or manifest.get("format") != "coinone-public-v2"
            or manifest.get("coins") != list(COINS)
            or external.get("format") != "a2-external-public-v1"
            or tuple(external.get("venues") or ()) != EXTERNAL_VENUES
            or external.get("coins") != list(COINS)
        ):
            raise ValueError("session is not the frozen C multi-venue universe")
        self.session_id = manifest["session"]
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("invalid observation session identity")
        self.captured_ms = int(self.observation.seed["captured_ms"])
        self.completed_ms = int(manifest["completed_ms"])
        self._audit = None

    def events(self):
        """Yield every envelope in process-local order, preserving exact raw text."""
        expected_sequence = 1
        for venue, row in self.observation.causal_envelopes():
            if row.get("sequence") != expected_sequence:
                raise ValueError("noncontiguous process-local arrival sequence")
            expected_sequence += 1
            raw = row.get("raw")
            message = None
            stream = None
            normalized = None
            if raw is not None:
                if not isinstance(raw, str):
                    raise ValueError("recorded websocket payload is not text")
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    raise ValueError("invalid recorded websocket payload") from None
                if venue == "coinone":
                    stream = _coinone_stream(message)
                    if stream is not None:
                        normalized = (
                            stream["coin"], stream["channel"], message["data"],
                        )
                else:
                    stream = stream_fields(venue, message)
                    recorded = row.get("stream")
                    if stream != recorded:
                        raise ValueError("external stream index differs from raw payload")
                    if stream is not None:
                        normalized = parse_leader(venue, row["received_ms"], raw)
                        if (
                            normalized is None
                            or normalized[3] != stream["coin"]
                            or normalized[4] != stream["exchange_ms"]
                        ):
                            raise ValueError("external normalization identity mismatch")
            elif row.get("stream") is not None:
                raise ValueError("external stream index has no raw payload")
            yield {
                "schema": "c-multivenue-arrival-v1",
                "session_id": self.session_id,
                "sequence": row["sequence"],
                "received_ns": row["received_ns"],
                "received_ms": row["received_ms"],
                "monotonic_ns": row["monotonic_ns"],
                "venue": venue,
                "kind": "data" if stream is not None else "control",
                "stream": deepcopy(stream),
                "normalized": deepcopy(normalized),
                "message": deepcopy(message),
                "raw": raw,
                "event": row.get("event"),
                "fields": deepcopy(row.get("fields") or {}),
            }

    def audit(self):
        if self._audit is not None:
            return deepcopy(self._audit)
        quality = self.observation.multivenue_quality()
        counts = Counter()
        streams = Counter()
        first = last = None
        prior_received_ns = -1
        prior_monotonic_ns = -1
        wall_clock_regressions = 0
        monotonic_regressions = 0
        for event in self.events():
            counts[event["kind"]] += 1
            counts["events"] += 1
            if event["kind"] == "data":
                stream = event["stream"]
                streams[":".join((event["venue"], stream["coin"], stream["channel"]))] += 1
            else:
                counts["control:" + str(event["event"] or "raw")] += 1
            received_ns = event["received_ns"]
            monotonic_ns = event["monotonic_ns"]
            if received_ns < prior_received_ns:
                wall_clock_regressions += 1
            if monotonic_ns < prior_monotonic_ns:
                monotonic_regressions += 1
            prior_received_ns = received_ns
            prior_monotonic_ns = monotonic_ns
            first = event if first is None else first
            last = event
        safe = bool(
            quality
            and quality["contiguous"]
            and counts["events"] > 0
            and wall_clock_regressions == 0
            and monotonic_regressions == 0
        )
        self._audit = {
            "schema": 1,
            "session_id": self.session_id,
            "folder": str(self.folder),
            "captured_ms": self.captured_ms,
            "completed_ms": self.completed_ms,
            "data_digest": self.observation.data_digest(),
            "manifest_source_digest": self.observation.manifest["source_digest"],
            "manifest_config_digest": self.observation.manifest["config_digest"],
            "manifest_fee_assumption": deepcopy(
                self.observation.manifest["fee_assumption"]
            ),
            "counts": dict(sorted(counts.items())),
            "streams": dict(sorted(streams.items())),
            "first_sequence": first["sequence"] if first else None,
            "last_sequence": last["sequence"] if last else None,
            "first_received_ns": first["received_ns"] if first else None,
            "last_received_ns": last["received_ns"] if last else None,
            "wall_clock_regressions": wall_clock_regressions,
            "monotonic_regressions": monotonic_regressions,
            "quality": quality,
            "safe_for_research": safe,
        }
        return deepcopy(self._audit)


class StudyTape:
    """Chronological sessions with explicit, never-bridged reconnect boundaries."""

    def __init__(self, folders):
        paths = [Path(folder).resolve() for folder in folders]
        if not paths or len(paths) != len(set(paths)):
            raise ValueError("one or more unique observation sessions are required")
        sessions = [Session(path) for path in paths]
        sessions.sort(key=lambda item: (item.captured_ms, item.session_id))
        if len({item.session_id for item in sessions}) != len(sessions):
            raise ValueError("duplicate observation session identity")
        self.sessions = sessions

    def audit(self):
        rows = [session.audit() for session in self.sessions]
        boundaries = []
        for left, right in zip(self.sessions, self.sessions[1:]):
            gap = right.captured_ms - left.completed_ms
            boundaries.append({
                "left": left.session_id,
                "right": right.session_id,
                "gap_ms": gap,
                "overlap": gap < 0,
                "inventory": "censor",
                "state": "reset",
                "bridged": False,
            })
        safe = all(row["safe_for_research"] for row in rows) and not any(
            row["overlap"] for row in boundaries
        )
        identity = {
            "schema": 1,
            "sessions": [
                {
                    "session_id": row["session_id"],
                    "data_digest": row["data_digest"],
                    "captured_ms": row["captured_ms"],
                    "completed_ms": row["completed_ms"],
                }
                for row in rows
            ],
        }
        return {
            "schema": 1,
            "sessions": rows,
            "boundaries": boundaries,
            "safe_for_research": safe,
            "study_digest": digest(identity),
        }

    def events(self):
        ordinal = 0
        for index, session in enumerate(self.sessions):
            for event in session.events():
                ordinal += 1
                row = dict(event)
                row["session_index"] = index
                row["study_ordinal"] = ordinal
                yield row
