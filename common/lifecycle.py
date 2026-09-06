"""Explicit A/B startup state; imports/replays and already-running books are untouched."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def startup_block_reason(job, root=ROOT):
    try:
        status = json.loads((Path(root) / "config" / "tracks.json").read_text(encoding="utf-8-sig"))
        if status.get("version") != 1:
            return "unsupported track state"
        if job == "hunt":
            track = "B"
        elif job == "select":
            track = "A"
        else:
            params = json.loads((Path(root) / "config" / "bitget.json").read_text(encoding="utf-8-sig"))
            on = params.get("hunt", {}).get("on", 0)
            if type(on) not in (int, bool) or on not in (0, 1):
                return "invalid track selector"
            track = "B" if on else "A"
        if status["tracks"][track]["status"] != "active":
            return f"track {track} is not active; see README.md"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "track state or params missing/invalid"
    return None


def require_active(job, root=ROOT):
    reason = startup_block_reason(job, root)
    if reason:
        raise SystemExit(f"START_BLOCKED: {reason}")
