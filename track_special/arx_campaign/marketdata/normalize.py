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
    except (ValueError, OverflowError):
        return fallback


def raw_hash(payload: Mapping[str, Any]) -> str:
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


def normalize_response(record_type: str, payload: Mapping[str, Any], received_at: datetime, *, category: str, symbol: str) -> tuple[NormalizedRecord, ...]:
    """Retain every payload item, including identical event identity duplicates."""
    data = payload.get("data", [])
    items = data if isinstance(data, list) else [data]
    request_time = _time(payload.get("requestTime"), received_at)
    seen: dict[str, str] = {}
    records = []
    for item in items:
        item = item if isinstance(item, Mapping) else {"value": item}
        digest = raw_hash(item)
        sequence = item.get("seq") or item.get("sequence") or item.get("tradeId") or item.get("id")
        identity = str(sequence) if sequence is not None else digest
        duplicate = seen.get(identity)
        seen.setdefault(identity, digest)
        exchange_time = _time(item.get("ts") or item.get("timestamp") or item.get("cTime") or payload.get("requestTime"), request_time)
        fields = {key: value for key, value in item.items() if key not in {"seq", "sequence", "tradeId", "id", "ts", "timestamp", "cTime"}}
        records.append(NormalizedRecord("bitget", "uta_v3", category, str(item.get("symbol", symbol)), item.get("baseCoin") or "ARX", item.get("quoteCoin") or "USDT", item.get("settleCoin") or ("USDT" if category == "USDT-FUTURES" else None), record_type, exchange_time, received_at, str(sequence) if sequence is not None else None, digest, duplicate, fields, dict(item)))
    return tuple(records)
