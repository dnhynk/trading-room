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
    ordered = sorted(records, key=lambda item: item.received_at)
    gaps = [(right.received_at - left.received_at).total_seconds() for left, right in zip(ordered, ordered[1:])]
    latencies = [(item.received_at - item.exchange_time).total_seconds() * 1000 for item in ordered]
    return CollectionMetrics(len(ordered), sum(item.duplicate_of is not None for item in ordered), ordered[0].received_at if ordered else None, ordered[-1].received_at if ordered else None, max(gaps) if gaps else None, median(latencies) if latencies else None, sum(value < 0 for value in latencies))
