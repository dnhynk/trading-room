from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class ResearchClassification(StrEnum):
    OFFICIAL_FACT = "official_fact"
    INFERENCE = "inference"
    UNVERIFIED = "unverified"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("research times must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ResearchFact:
    claim: str
    source_url: str
    event_at: datetime | None
    first_observed_at: datetime
    fetched_at: datetime
    expires_at: datetime | None
    raw_hash: str
    classification: ResearchClassification

    def __post_init__(self) -> None:
        for name in ("first_observed_at", "fetched_at"):
            object.__setattr__(self, name, _utc(getattr(self, name)))
        for name in ("event_at", "expires_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value))
