"""Causal recorder reader. Source order and reception time are authoritative."""
from collections import Counter, deque, defaultdict
from dataclasses import dataclass
import datetime as dt
import gzip
import json
import math
from pathlib import Path

from .config import file_hash


def epoch_ms(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return int(parsed.timestamp() * 1000)


@dataclass(frozen=True)
class Event:
    t: int
    symbol: str
    channel: str
    message: dict


class Reader:
    def __init__(self, symbols, track_coverage=False):
        self.symbols = set(symbols)
        self.quality = Counter()
        self.last_t = -1
        self.channel_ts = {}
        self.recent, self.seen = deque(), set()
        self.sources = []
        self.start = self.end = None
        self.track_coverage = track_coverage
        self.quotes = defaultdict(list)

    def parse(self, line):
        if not line.strip():
            self.quality["blank_lines"] += 1
            return None
        try:
            prefix, raw = line.split("\t", 1)
            t = int(prefix)
            m = json.loads(raw)
            if not isinstance(m, dict):
                raise ValueError("message")
            if "local" in m:
                return None
            arg = m.get("arg") or {}
            sym, channel = arg.get("instId"), arg.get("channel")
            if sym not in self.symbols or channel not in {"books15", "trade", "candle1m", "ticker"} or not m.get("data"):
                return None
            if t <= 0:
                raise ValueError("clock")
            if t < self.last_t:
                self.quality["reception_regressions"] += 1
                return None
            self.last_t = t
            exchange = int(m.get("ts") or t)
            if channel == "books15":
                exchange = int(m["data"][0].get("ts") or exchange)
            if exchange > t + 1000:
                self.quality["future_exchange_messages"] += 1
                return None
            key = (sym, channel)
            if exchange < self.channel_ts.get(key, 0):
                self.quality["late_channel_messages"] += 1
                return None
            self.channel_ts[key] = exchange
            identity = (sym, channel, raw.strip())
            if identity in self.seen:
                self.quality["duplicate_messages"] += 1
                return None
            self.seen.add(identity)
            self.recent.append(identity)
            if len(self.recent) > 10000:
                self.seen.discard(self.recent.popleft())
            if channel == "trade" and m.get("action") == "snapshot":
                return None
            if channel == "books15":
                data = m["data"][0]
                for name in ("bids", "asks"):
                    levels = [(float(p), float(q)) for p, q in data[name]]
                    if not levels or any(not math.isfinite(p + q) or p <= 0 or q < 0 for p, q in levels):
                        raise ValueError("levels")
                    if any((a[0] <= b[0] if name == "bids" else a[0] >= b[0]) for a, b in zip(levels, levels[1:])):
                        raise ValueError("unsorted book")
                if float(data["bids"][0][0]) >= float(data["asks"][0][0]):
                    raise ValueError("crossed book")
            elif channel == "trade":
                for row in m["data"]:
                    p, q = float(row["price"]), float(row["size"])
                    if not math.isfinite(p + q) or min(p, q) <= 0 or row["side"] not in {"buy", "sell"}:
                        raise ValueError("trade")
            elif channel == "candle1m":
                for row in m["data"]:
                    vals = [float(x) for x in row[:6]]
                    if len(vals) != 6 or not all(math.isfinite(x) for x in vals) or min(vals[1:5]) <= 0 or vals[0] > t:
                        raise ValueError("candle")
            else:
                mark = float(m["data"][0].get("markPrice") or 0)
                if not math.isfinite(mark) or mark <= 0:
                    raise ValueError("mark")
            self.start = t if self.start is None else self.start
            self.end = t
            self.quality["messages"] += 1
            if self.track_coverage and channel == "books15":
                self.quotes[sym].append(t)
            return Event(t, sym, channel, m)
        except (ValueError, TypeError, KeyError, IndexError):
            self.quality["invalid_messages"] += 1
            return None

    def files(self, paths):
        resolved = [Path(p).resolve() for p in paths]
        if len(set(resolved)) != len(resolved):
            raise ValueError("duplicate input file")
        for path in resolved:
            before = (path.stat().st_size, path.stat().st_mtime_ns)
            source = dict(path=str(path), bytes=before[0], sha256=file_hash(path))
            self.sources.append(source)
            op = gzip.open if path.suffix == ".gz" else open
            with op(path, "rt", encoding="utf-8") as stream:
                for line in stream:
                    event = self.parse(line)
                    if event:
                        yield event
            if before != (path.stat().st_size, path.stat().st_mtime_ns):
                raise ValueError("replay input changed while reading; freeze the tape first")


class Funding:
    """Explicit settlement series plus provenance and coverage, never ticker estimates."""
    def __init__(self, path=None):
        self.spec = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
        self.rows = []
        self.source = dict(path=str(Path(path).resolve()), sha256=file_hash(path)) if path else None
        if self.spec:
            s = self.spec
            if not s.get("source") or epoch_ms(s["coverage_end"]) <= epoch_ms(s["coverage_start"]):
                raise ValueError("funding coverage/source required")
            keys = set()
            for row in s["settlements"]:
                t, sym, rate, mark = epoch_ms(row["t"]), row["symbol"], float(row["rate"]), float(row["mark"])
                if sym not in s["symbols"] or not epoch_ms(s["coverage_start"]) <= t <= epoch_ms(s["coverage_end"]) or not math.isfinite(rate + mark) or abs(rate) >= 1 or mark <= 0 or (t, sym) in keys:
                    raise ValueError("invalid/duplicate settlement")
                keys.add((t, sym))
                self.rows.append(dict(t=t, symbol=sym, rate=rate, mark=mark))
            self.rows.sort(key=lambda r: (r["t"], r["symbol"]))

    def covers(self, start, end, symbols):
        s = self.spec
        return bool(s and start is not None and end is not None and epoch_ms(s["coverage_start"]) <= start <= end <= epoch_ms(s["coverage_end"]) and set(symbols) <= set(s["symbols"]))
