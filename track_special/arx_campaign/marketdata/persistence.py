"""Append-only external-state JSONL store; source trees never hold observations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .normalize import NormalizedRecord


class AppendOnlyJsonlStore:
    def __init__(self, state_directory: Path) -> None:
        self.state_directory = state_directory

    def _path(self, stream_key: str) -> Path:
        if not stream_key or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in stream_key):
            raise ValueError("stream key must be a safe filename")
        return self.state_directory / f"{stream_key}.jsonl"

    def append(self, stream_key: str, records: Iterable[NormalizedRecord]) -> int:
        path = self._path(stream_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record.json_dict(), sort_keys=True, separators=(",", ":"), default=str) + "\n")
                count += 1
        return count

    def readback(self, stream_key: str) -> tuple[dict, ...]:
        path = self._path(stream_key)
        if not path.exists():
            return ()
        with path.open(encoding="utf-8") as handle:
            return tuple(json.loads(line) for line in handle if line.strip())
