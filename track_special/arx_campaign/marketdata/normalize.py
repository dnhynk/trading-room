"""Lossless normalization of public responses into durable observation records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


def _time(value: Any, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return fallback


def raw_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    venue: str
    api_family: str
    category: str
    symbol: str
    base_coin: str | None
    quote_coin: str | None
    settlement_coin: str | None
    record_type: str
    exchange_time: datetime
    received_at: datetime
    source_sequence: str | None
    raw_hash: str
    duplicate_of: str | None
    fields: Mapping[str, Any]
    raw: Mapping[str, Any]

    def json_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["exchange_time"] = self.exchange_time.isoformat()
        value["received_at"] = self.received_at.isoformat()
        return value


def _items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        for key in ("list", "resultList"):
            if isinstance(data.get(key), list):
                return list(data[key])
        return [data]
    return [] if data is None else [data]


def _candle(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return item
    if isinstance(item, (list, tuple)):
        names = ("ts", "open", "high", "low", "close", "volume", "turnover")
        return {"candle": list(item), **{name: value for name, value in zip(names, item)}}
    return {"value": item}


def normalize_response(
    record_type: str,
    payload: Mapping[str, Any],
    received_at: datetime,
    *,
    category: str,
    symbol: str,
    interval: str | None = None,
    candle_type: str | None = None,
) -> tuple[NormalizedRecord, ...]:
    """Retain every response item and distinguish requested from returned identity."""
    items = _items(payload.get("data", []))
    request_time = _time(payload.get("requestTime"), received_at)
    response_digest = raw_hash(payload)
    seen: dict[str, str] = {}
    records = []
    for item in items:
        item = _candle(item) if record_type == "candles" else (item if isinstance(item, Mapping) else {"value": item})
        digest = raw_hash(item)
        sequence = item.get("seq") or item.get("sequence") or item.get("tradeId") or item.get("id")
        identity = str(sequence) if sequence is not None else digest
        duplicate = seen.get(identity)
        seen.setdefault(identity, digest)
        exchange_time = _time(
            item.get("ts")
            or item.get("timestamp")
            or item.get("cTime")
            or item.get("fundingTime")
            or item.get("time")
            or payload.get("requestTime"),
            request_time,
        )
        fields = {key: value for key, value in item.items() if key not in {"seq", "sequence", "tradeId", "id", "ts", "timestamp", "cTime"}}
        fields.update(
            {
                "requested_symbol": symbol,
                "requested_category": category,
                "response_symbol": item.get("symbol"),
                "response_category": item.get("category"),
                "identity_observed": item.get("symbol") is not None,
                "response_raw_hash": response_digest,
            }
        )
        if record_type == "candles" and interval is not None:
            duration = _interval_seconds(interval)
            fields["completed"] = duration is not None and exchange_time.timestamp() + duration <= received_at.timestamp()
            fields["interval"] = interval
            fields["candle_type"] = candle_type or "market"
        # ``symbol`` is the requested stream identity. Whether the venue echoed
        # it is retained separately and must be checked before live use.
        records.append(NormalizedRecord("bitget", "uta_v3", category, str(item.get("symbol", symbol)), item.get("baseCoin"), item.get("quoteCoin"), item.get("settleCoin"), record_type, exchange_time, received_at, str(sequence) if sequence is not None else None, digest, duplicate, fields, dict(item)))
    return tuple(records)


def _interval_seconds(interval: str) -> int | None:
    units = {"m": 60, "H": 3600, "D": 86400}
    try:
        return int(interval[:-1]) * units[interval[-1]]
    except (ValueError, KeyError, IndexError):
        return None
