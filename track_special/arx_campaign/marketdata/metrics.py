"""Coverage and latency measurements without quietly filling observed gaps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Iterable

from .normalize import NormalizedRecord


@dataclass(frozen=True, slots=True)
class CollectionMetrics:
    count: int
    duplicates: int
    first_received_at: datetime | None
    last_received_at: datetime | None
    longest_gap_seconds: float | None
    median_latency_ms: float | None
    negative_latency_count: int


def collection_metrics(records: Iterable[NormalizedRecord]) -> CollectionMetrics:
    values = list(records)
    exchange_ordered = sorted(values, key=lambda item: item.exchange_time)
    received_ordered = sorted(values, key=lambda item: item.received_at)
    gaps = [
        (right.exchange_time - left.exchange_time).total_seconds()
        for left, right in zip(exchange_ordered, exchange_ordered[1:])
    ]
    latencies = [
        (item.received_at - item.exchange_time).total_seconds() * 1000
        for item in values
    ]
    return CollectionMetrics(
        len(values),
        sum(item.duplicate_of is not None for item in values),
        received_ordered[0].received_at if received_ordered else None,
        received_ordered[-1].received_at if received_ordered else None,
        max(gaps) if gaps else None,
        median(latencies) if latencies else None,
        sum(value < 0 for value in latencies),
    )
