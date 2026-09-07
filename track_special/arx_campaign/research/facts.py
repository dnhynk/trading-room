from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
from pathlib import Path
from urllib.parse import urlparse


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
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("research facts require an HTTPS source URL")
        if not self.claim or not self.raw_hash:
            raise ValueError("research claim and raw hash are required")
        if self.expires_at is not None and self.expires_at <= self.fetched_at:
            raise ValueError("research expiry must follow fetch time")

    def usable_at(self, decision_at: datetime) -> bool:
        """Future observations and expired facts cannot enter historical decisions."""

        decision_at = _utc(decision_at)
        return self.first_observed_at <= decision_at and (
            self.expires_at is None or decision_at < self.expires_at
        )

    def json_dict(self) -> dict[str, str | None]:
        return {
            "claim": self.claim,
            "source_url": self.source_url,
            "event_at": self.event_at.isoformat() if self.event_at else None,
            "first_observed_at": self.first_observed_at.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "raw_hash": self.raw_hash,
            "classification": self.classification.value,
        }


class ResearchFactStore:
    """Append-only evidence store with no execution or configuration authority."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, fact: ResearchFact) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(fact.json_dict(), sort_keys=True) + "\n")

    def readback(self) -> tuple[dict[str, object], ...]:
        if not self.path.exists():
            return ()
        with self.path.open(encoding="utf-8") as handle:
            return tuple(json.loads(line) for line in handle if line.strip())
