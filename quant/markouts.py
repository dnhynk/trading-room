"""Streaming executable-depth markouts. Signal diagnostics and fills stay separate."""
from collections import Counter, defaultdict
import heapq
import random
import statistics

HORIZONS = (1, 3, 5, 10, 30, 60)


def vwap(levels, qty):
    remaining, value = qty, 0.0
    for p, q in levels:
        taken = min(remaining, float(q))
        remaining -= taken
        value += taken * float(p)
        if remaining <= qty * 1e-10:
            return value / qty
    return None


class Markouts:
    def __init__(self, seed=20260905):
        self.seed, self.seq = seed, 0
        self.pending = defaultdict(list)
        self.values = defaultdict(list)
        self.censored = Counter()
        self.anchors = Counter()

    def queue(self, due, row, stage, horizon=0):
        self.seq += 1
        heapq.heappush(self.pending[row["symbol"]], (due, self.seq, row, stage, horizon))

    def add(self, group, symbol, side, t, qty, engine, price=None, entry_fee=None):
        if qty <= 0:
            return
        self.anchors[group] += 1
        row = dict(group=group, symbol=symbol, side=side, t=t, qty=qty, engine=engine)
        if price is None:
            self.queue(t + engine.execution["latency_ms"], row, "entry")
        else:
            row.update(entry=price, entry_fee=entry_fee, entry_t=t)
            self.exits(row)

    def exits(self, row):
        engine = row["engine"]
        for h in HORIZONS:
            self.queue(row["entry_t"] + h * 1000 + engine.execution["latency_ms"], row, "exit", h)

    def quote(self, event):
        if event.channel != "books15":
            return
        book = event.message["data"][0]
        pending = self.pending[event.symbol]
        while pending and pending[0][0] <= event.t:
            due, _, row, stage, h = heapq.heappop(pending)
            engine, group = row["engine"], row["group"]
            if event.t - due > 1000 or event.t - int(book.get("ts") or event.t) > engine.execution["quote_max_age_ms"]:
                self.censored[f"{group}:{h}:{stage}_late_quote"] += 1
                continue
            s = 1 if row["side"] == "long" else -1
            buy = (stage == "entry") == (s > 0)
            price = vwap(book["asks" if buy else "bids"], row["qty"])
            if price is None:
                self.censored[f"{group}:{h}:{stage}_depth"] += 1
                continue
            price *= 1 + (1 if buy else -1) * engine.execution["slip_bps"] / 10000
            if stage == "entry":
                if row["qty"] * price < engine.cfg["books"][event.symbol]["min_order"]:
                    self.censored[f"{group}:0:entry_minimum"] += 1
                    continue
                row.update(entry=price, entry_fee=engine.rates(row["t"])[1], entry_t=event.t)
                self.exits(row)
            else:
                if price * row["qty"] < engine.cfg["books"][event.symbol]["min_order"]:
                    self.censored[f"{group}:{h}:exit_minimum"] += 1
                    continue
                rate = engine.rates(row["entry_t"] + h * 1000)[1]
                bp = 10000 * (s * (price / row["entry"] - 1) - row["entry_fee"] - rate * price / row["entry"])
                self.values[(group, h)].append(dict(t=row["t"], symbol=row["symbol"], side=row["side"], bp=bp, qty=row["qty"], entry=row["entry"]))

    def result(self):
        censored = self.censored.copy()
        for heap in self.pending.values():
            for _, _, row, stage, h in heap:
                censored[f"{row['group']}:{h}:{stage}_unfinished"] += 1
        groups = {}
        for group in self.anchors:
            groups[group] = {}
            for h in HORIZONS:
                rows = self.values[group, h]
                weight = sum(x["qty"] * x["entry"] for x in rows)
                groups[group][h] = dict(n=len(rows), mean_bp=statistics.mean(x["bp"] for x in rows) if rows else None,
                                       notional_weighted_bp=sum(x["bp"] * x["qty"] * x["entry"] for x in rows) / weight if weight else None)
        matched = {}
        rng = random.Random(self.seed)
        for h in HORIZONS:
            controls = defaultdict(list)
            for x in self.values["control", h]:
                controls[x["t"] // 86400000, x["symbol"], x["side"]].append(x["bp"])
            pairs = []
            unmatched = 0
            for x in self.values["signal", h]:
                pool = controls[x["t"] // 86400000, x["symbol"], x["side"]]
                if pool:
                    pairs.append(x["bp"] - rng.choice(pool))
                else:
                    unmatched += 1
            matched[h] = dict(n=len(pairs), unmatched=unmatched, mean_excess_bp=statistics.mean(pairs) if pairs else None)
        return dict(horizons_s=HORIZONS, anchors=dict(self.anchors), groups=groups, matched_signal_excess=matched, censored=dict(censored),
                    interpretation="Signal/control: taker entry after latency. Fill: actual simulated maker/partial fills. Exit: depth VWAP after horizon plus exit latency. Endpoints >1s late are censored. Matched controls use the same UTC day/symbol/side; overlapping samples and selection are not independent alpha proof. Funding excluded.")
