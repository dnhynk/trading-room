"""Shared risk arithmetic and the live engine's append-only daily fill ledger.

No exchange calls or writes. The ledger survives book/side rotation and does not
count deposits, hand trades, dry fills, or a stop's partial fills as new stops.
"""
import json
import math
import os
import time


def floor_qty(qty, step):
    """An executable quantity never above its risk ceiling; zero is meaningful."""
    if not math.isfinite(qty) or not math.isfinite(step) or step <= 0:
        raise ValueError("invalid quantity ceiling or step")
    return round(max(0, math.floor(qty / step + 1e-10)) * step, 12)


class FillLedger:
    """Incremental reader. Only complete lines commit; corruption fails closed.

    Events use the host's local wall time, as cycle.ev does. Dates here are UTC,
    matching the trading day's contract (not the Slack display day's KST).
    """
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.modes = {}
        self.totals = {}
        self.stop_ids = set()
        self.identity = None

    def poll(self):
        with open(self.path, "rb") as fh:
            stat = os.fstat(fh.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if stat.st_size < self.offset or (self.identity and identity != self.identity):
                raise ValueError("fill ledger replaced or truncated; reconciliation required")
            self.identity = identity
            fh.seek(self.offset)
            # Bound a poll to the file as it existed on open. Appends arrive next poll.
            data = fh.read(stat.st_size - self.offset)
        complete = data.rfind(b"\n") + 1
        for raw in data[:complete].splitlines(keepends=True):
            # Old event files contain a few orphaned SIGNAL fragments from
            # concurrent writers. They cannot contribute to a cash ledger.
            if any(token in raw for token in (b'"ev": "START"', b'"ev": "FILL"', b'"ev": "STOP_HIT"',
                                             b'"ev":"START"', b'"ev":"FILL"', b'"ev":"STOP_HIT"')):
                e = json.loads(raw)
                self.add(e)
            self.offset += len(raw)

    def add(self, e):
        kind, sym = e.get("ev"), e.get("symbol")
        if kind == "START" and sym:
            self.modes[sym] = e.get("mode")
            return
        if kind not in ("FILL", "STOP_HIT") or not sym:
            return
        if self.modes.get(sym) not in ("live", "dry"):
            raise ValueError("fill without a known live/dry START; reconciliation required")
        if self.modes.get(sym) != "live":
            return
        epoch = time.mktime(time.strptime(e["t"], "%Y-%m-%d %H:%M:%S"))
        day = time.strftime("%Y-%m-%d", time.gmtime(epoch))
        pnl = float(e["pnl"])
        if not math.isfinite(pnl):
            raise ValueError("non-finite fill P&L")
        key = (day, sym, e.get("side") or "long")
        values = self.totals.setdefault(key, [0.0, 0])
        values[0] += pnl
        if kind == "STOP_HIT":
            oid = e.get("oid")
            stop = (sym, key[2], oid)
            if (oid is not None and stop not in self.stop_ids) or (oid is None and not e.get("partial")):
                values[1] += 1
            if oid is not None:
                self.stop_ids.add(stop)

    def summary(self, day=None, exclude=None):
        self.poll()
        day = day or time.strftime("%Y-%m-%d", time.gmtime())
        rows = [v for (d, sym, _), v in self.totals.items() if d == day and sym != exclude]
        return sum((v[0] for v in rows), 0.0), sum(v[1] for v in rows)
