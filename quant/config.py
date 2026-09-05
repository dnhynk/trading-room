"""Explicit, frozen inputs. Never reads production params or account state."""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from bot.signal import SIG, STRAT


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_hashes():
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "quant").glob("*.py")) + [root / "bot" / n for n in ("signal.py", "risk.py")]
    return {p.relative_to(root).as_posix(): file_hash(p) for p in paths}


def positive(x, name, zero=False):
    if isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) or (x < 0 if zero else x <= 0):
        raise ValueError(f"invalid {name}")


@dataclass(frozen=True)
class Config:
    # Store canonical text so callers cannot mutate a nested configuration.
    text: str

    @property
    def data(self):
        return json.loads(self.text)

    @property
    def id(self):
        return hashlib.sha256(self.text.encode()).hexdigest()

    @classmethod
    def read(cls, path):
        return cls.create(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def create(cls, d):
        required = {"version", "name", "equity", "signal", "strategy", "books", "execution", "validation"}
        version = d.get("version")
        if version == 2:
            required.add("research")
        if set(d) != required or version not in (1, 2):
            raise ValueError("configuration keys/version do not match quant schema")
        positive(d["equity"], "equity")
        if set(d["signal"]) != set(SIG) or set(d["strategy"]) != set(STRAT) | {"mode"}:
            raise ValueError("all signal/strategy defaults must be explicitly frozen")
        for section in ("signal", "strategy"):
            for key, value in d[section].items():
                if isinstance(value, (int, float)) and not math.isfinite(value):
                    raise ValueError(f"non-finite {section}.{key}")
        p = d["strategy"]
        if p.get("mode") != "dry" or p.get("max_units") != 1 or p.get("cap_per_unit") != 1:
            raise ValueError("quant requires dry mode, one unit, and per-unit cap")
        for key, ceiling in (("unit_frac", .75), ("notional_frac", .75), ("cap_frac", .05), ("daily_loss_frac", .15)):
            positive(p[key], key)
            if p[key] > ceiling:
                raise ValueError(f"{key} exceeds the extracted reference budget")
        if p["cap_min_atr"] < 15 or not 1 <= p["max_stops_day"] <= 3:
            raise ValueError("risk geometry exceeds reference")
        if d["signal"]["s8_on"] or p.get("exit") or p.get("wind_down"):
            raise ValueError("shadow detector or operational exit flags in frozen policy")
        if not isinstance(d["books"], dict) or not d["books"]:
            raise ValueError("explicit contracts required")
        for sym, book in d["books"].items():
            keys = {"sides", "qstep", "tick"} | ({"min_order", "price_ladder"} if version == 2 else set())
            if not sym.isalnum() or set(book) != keys:
                raise ValueError("invalid contract")
            if not book["sides"] or len(set(book["sides"])) != len(book["sides"]) or not set(book["sides"]) <= {"long", "short"}:
                raise ValueError("invalid sides")
            positive(book["qstep"], "qstep")
            positive(book["tick"], "tick")
            if version == 2:
                positive(book["min_order"], "min_order")
                ladder = book["price_ladder"]
                if not ladder or ladder[0][0] != 0:
                    raise ValueError("price ladder must start at zero")
                for i, row in enumerate(ladder):
                    if len(row) != 2:
                        raise ValueError("invalid price ladder")
                    positive(row[0], "price floor", zero=True)
                    positive(row[1], "price unit")
                    if i and row[0] <= ladder[i - 1][0]:
                        raise ValueError("unordered price ladder")
        e = d["execution"]
        if set(e) != {"maker", "taker", "slip_bps", "latency_ms", "quote_max_age_ms", "max_participation"}:
            raise ValueError("invalid execution keys")
        for key in e:
            positive(e[key], key, zero=key in {"maker", "taker", "slip_bps"})
        if e["latency_ms"] < 1000 or e["quote_max_age_ms"] > 5000 or not 0 < e["max_participation"] <= 1:
            raise ValueError("invalid latency, freshness or participation")
        if max(e["maker"], e["taker"]) >= .01 or e["slip_bps"] > 500:
            raise ValueError("fees are fractions; slip is basis points")
        v = d["validation"]
        if set(v) != {"min_days", "min_campaigns", "max_drawdown_frac", "horizon_s", "embargo_s", "bootstrap_samples", "seed"}:
            raise ValueError("invalid validation keys")
        for key in v:
            positive(v[key], key, zero=key == "seed")
        for key in ("min_days", "min_campaigns", "horizon_s", "embargo_s", "bootstrap_samples", "seed"):
            if not isinstance(v[key], int):
                raise ValueError(f"{key} must be an integer")
        if v["embargo_s"] < v["horizon_s"] or v["min_days"] < 20 or v["min_campaigns"] < 100:
            raise ValueError("insufficient validation floor")
        if v["bootstrap_samples"] < 200 or not 0 < v["max_drawdown_frac"] <= .15:
            raise ValueError("invalid validation policy")
        if version == 2:
            r = d["research"]
            if set(r) != {"venue", "market", "policy", "regime", "entry_ttl_s", "max_hold_s", "invalidation_ticks", "invalidation_confirm_s", "min_trades_10s", "fee_schedule", "fee_stress_floor", "phase_max_age_s"}:
                raise ValueError("invalid research keys")
            if r["venue"] not in {"bitget", "coinone", "bithumb", "upbit", "korbit"} or r["market"] not in {"spot", "perpetual"}:
                raise ValueError("invalid venue/market")
            if (r["venue"] == "bitget") != (r["market"] == "perpetual"):
                raise ValueError("unsupported venue/market pair")
            if r["market"] == "spot" and any(b["sides"] != ["long"] for b in d["books"].values()):
                raise ValueError("spot research is cash-funded long only")
            if r["policy"] not in {"reference", "scalp_exit", "scalp"} or r["regime"] not in {"none", "structure", "recorded", "recorded_det"}:
                raise ValueError("invalid policy/regime")
            for key in ("entry_ttl_s", "max_hold_s", "invalidation_ticks", "invalidation_confirm_s", "min_trades_10s", "phase_max_age_s"):
                positive(r[key], key)
                if not isinstance(r[key], int):
                    raise ValueError(f"{key} must be an integer")
            if r["entry_ttl_s"] > r["max_hold_s"] or r["max_hold_s"] > 300 or r["invalidation_confirm_s"] < 2:
                raise ValueError("invalid scalp time geometry")
            schedule = r["fee_schedule"]
            if not schedule or schedule[0][0] != 0:
                raise ValueError("fee schedule must start at zero")
            for i, row in enumerate(schedule):
                if len(row) != 3 or row[0] < 0 or (i and row[0] <= schedule[i - 1][0]):
                    raise ValueError("invalid fee schedule")
                for rate in row[1:]:
                    positive(rate, "fee", zero=True)
                    if rate >= .01:
                        raise ValueError("fee is a fraction")
            positive(r["fee_stress_floor"], "fee stress floor", zero=True)
            if r["fee_stress_floor"] >= .005:
                raise ValueError("invalid fee stress floor")
        return cls(canonical(d))

    def stressed(self):
        d = self.data
        d["name"] += "-cost-stress"
        d["execution"]["maker"] *= 2
        d["execution"]["taker"] *= 2
        d["execution"]["slip_bps"] += 25
        d["execution"]["latency_ms"] *= 2
        if d["version"] == 2:
            r = d["research"]
            r["fee_schedule"] = [[t, max(2 * m, r["fee_stress_floor"]), max(2 * k, r["fee_stress_floor"])] for t, m, k in r["fee_schedule"]]
        return Config.create(d)
