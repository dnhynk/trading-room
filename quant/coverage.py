"""Evidence sidecars. Completeness is checked against observations, never a flag."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
import json
from pathlib import Path

from .config import file_hash
from .data import epoch_ms
from .validation import block_interval


def load_sidecar(path):
    if path is None:
        return None, None
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if not d.get("source"):
        raise ValueError("evidence sidecar must identify its source")
    return d, dict(path=str(Path(path).resolve()), sha256=file_hash(path))


def universe_coverage(spec, quotes, start, end, contracts, max_gap):
    if spec is None:
        return dict(complete=False, reasons=["universe_manifest_missing"])
    intervals = sorted(spec["intervals"], key=lambda x: epoch_ms(x["start"]))
    cursor, reasons = start, []
    for row in intervals:
        a, z = epoch_ms(row["start"]), epoch_ms(row["end"])
        if z <= a or not row["symbols"] or len(set(row["symbols"])) != len(row["symbols"]):
            raise ValueError("invalid expected universe interval")
        a, z = max(a, start), min(z, end)
        if a >= z:
            continue
        if a != cursor:
            reasons.append("universe_interval_gap_or_overlap")
        cursor = max(cursor, z)
        for sym in row["symbols"]:
            if sym not in contracts:
                reasons.append(f"unconfigured_contract:{sym}")
                continue
            times = quotes.get(sym, [])
            i, j = bisect_left(times, a), bisect_right(times, z)
            span = times[i:j]
            if not span or span[0] - a > max_gap or z - span[-1] > max_gap or any(b - a > max_gap for a, b in zip(span, span[1:])):
                reasons.append(f"quote_coverage:{sym}")
    if cursor < end:
        reasons.append("universe_window_uncovered")
    # A manifest cannot silently exclude a configured/traded symbol from its universe.
    declared = set().union(*(set(r["symbols"]) for r in intervals)) if intervals else set()
    if not set(contracts) <= declared:
        reasons.append("configured_symbols_omitted")
    return dict(complete=not reasons, reasons=sorted(set(reasons)), source=spec["source"])


def attribute_episodes(spec, runs, policy):
    if spec is None:
        return dict(complete=False, reasons=["episode_manifest_missing"], interval=dict(blocks=0, lower=None, upper=None))
    episodes = defaultdict(list)
    for row in spec["episodes"]:
        if not row.get("id"):
            raise ValueError("episode id required")
        a, z = epoch_ms(row["start"]), epoch_ms(row["end"])
        if z <= a:
            raise ValueError("invalid episode range")
        episodes[row["symbol"]].append((a, z, row["id"]))
    reasons, totals = [], defaultdict(float)
    for name, run in runs.items():
        for c in run["campaigns"] + run["unfinished"]:
            matches = {eid for a, z, eid in episodes[c["symbol"]] if a <= c["t0"] and (c["t1"] or run["end"]) < z}
            if len(matches) != 1:
                reasons.append(f"unassigned_or_ambiguous:{name}:{c['symbol']}:{c['t0']}")
                continue
            c["episode"] = matches.pop()
            if name == "reference" and c["t1"]:
                totals[c["episode"]] += c["net"]
    return dict(complete=not reasons, reasons=reasons, source=spec["source"],
                interval=block_interval(totals, policy["seed"], policy["bootstrap_samples"]))
