"""Local-only passive fill probes for reference-state markout diagnostics.

These probes never relax the C execution gate and never submit orders. They
measure whether a minimum resting bid would have filled, then mark each fill at
fixed horizons using the first common decision snapshot at or after the target.
"""
from copy import deepcopy
from decimal import Decimal as D, ROUND_CEILING
import math

from track_c.market.microstructure import liquidate
from track_c_multivenue.contract import MARKOUT_HORIZONS_MS


REFERENCE_GROUPS = (
    "local_discount_external_stable",
    "common_market_fall",
    "external_reference_unavailable",
    "external_ready_no_local_discount",
    "local_observation_unavailable",
)


def reference_group(state, cfg):
    if not state or not state.get("entry_fresh"):
        return "local_observation_unavailable"
    reference = state.get("reference") or {}
    if not reference.get("ready"):
        return "external_reference_unavailable"
    if (
        reference.get("m10") is not None
        and reference["m10"] <= -cfg["common_drop_ticks"]
    ):
        return "common_market_fall"
    if reference.get("dev_ticks", -math.inf) >= cfg["entry_ticks"]:
        return "local_discount_external_stable"
    return "external_ready_no_local_discount"


def _ceil(value, step):
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def local_minimum_action(state, cfg, candidate_id):
    """Build a local-market measurement probe without external-price admission."""
    if not state or not state.get("entry_fresh"):
        return None, "local_observation_unavailable"
    if not state.get("bids") or not state.get("asks"):
        return None, "local_book_unavailable"
    bid, ask = D(str(state["bid"])), D(str(state["ask"]))
    if not (D(0) < bid < ask):
        return None, "invalid_local_book"
    contract = state.get("contract") or {}
    if (
        contract.get("trade_status", 1) != 1
        or contract.get("maintenance_status", 0) != 0
    ):
        return None, "market_unavailable"
    if "order_types" in contract and "limit" not in contract["order_types"]:
        return None, "limit_order_unavailable"
    try:
        step = D(str(contract["qty_unit"]))
        minimum = D(str(contract["min_order_amount"]))
        minimum_qty = D(str(contract.get("min_qty", "0")))
        maximum_qty = D(str(contract["max_qty"]))
        maximum_amount = D(str(contract.get("max_order_amount", "Infinity")))
    except (KeyError, ValueError, ArithmeticError):
        return None, "invalid_market_contract"
    if step <= 0 or minimum <= 0:
        return None, "invalid_market_contract"
    qty = _ceil(max(minimum_qty, minimum / bid), step)
    if qty <= 0 or qty > maximum_qty or qty * bid > maximum_amount:
        return None, "minimum_order_exceeds_exchange_cap"
    return {
        "schema": 1,
        "candidate_id": candidate_id,
        "coin": state["coin"],
        "t_ms": state["t_ms"],
        "price": float(bid),
        "qty": float(qty),
        "qty_step": float(step),
        "notional_krw": float(qty * bid),
        "ttl_s": cfg["ttl_s"],
        "latency_ms": cfg["latency_ms"],
        "cancel_latency_ms": cfg["cancel_latency_ms"],
    }, None


class FillMarkoutProbe:
    """Independent queue counterfactual; overlapping probes are non-additive."""

    def __init__(self, action, cfg, state, group):
        self.action = deepcopy(action)
        self.cfg = cfg
        self.group = group
        self.start_ms = action["t_ms"]
        self.arrival_ms = self.start_ms + action["latency_ms"]
        self.deadline_ms = self.start_ms + action["ttl_s"] * 1000
        self.cancel_settled_ms = self.deadline_ms + action["cancel_latency_ms"]
        self.book_ms = state["book_ms"]
        self.bids = [list(row) for row in state["bids"]]
        self.asks = [list(row) for row in state["asks"]]
        self.active = False
        self.entry_done = False
        self.entry_reason = None
        self.ahead = 0.0
        self.filled_qty = 0.0
        self.fills = []
        self.last_ms = self.start_ms

    def _fresh(self, now):
        return bool(
            self.bids
            and self.asks
            and 0 <= now - self.book_ms <= self.cfg["book_max_age_ms"]
        )

    def advance(self, now):
        if now < self.last_ms:
            raise ValueError("noncausal diagnostic clock")
        self.last_ms = now
        if not self.entry_done and not self.active and now >= self.arrival_ms:
            if not self._fresh(self.arrival_ms):
                self.entry_done = True
                self.entry_reason = "arrival_book_unavailable"
            elif self.action["price"] >= self.asks[0][0]:
                self.entry_done = True
                self.entry_reason = "arrival_post_only_reject"
            else:
                self.active = True
                self.ahead = sum(
                    qty for price, qty in self.bids
                    if price == self.action["price"]
                )
        if self.active and now >= self.cancel_settled_ms:
            self.active = False
            self.entry_done = True
            self.entry_reason = "filled" if self.filled_qty else "no_fill"

    def event(self, event):
        now = event["t"]
        self.advance(now)
        if event["kind"] == "book":
            self.book_ms = now
            self.bids = [list(row) for row in event["bids"]]
            self.asks = [list(row) for row in event["asks"]]
            return
        if (
            event["kind"] != "trade"
            or not self.active
            or event["buy"]
            or event["price"] > self.action["price"]
        ):
            return
        if event["price"] < self.action["price"]:
            self.ahead = 0.0
        ahead = min(self.ahead, event["qty"])
        self.ahead -= ahead
        available = max(0.0, event["qty"] - ahead)
        remaining = self.action["qty"] - self.filled_qty
        qty = min(remaining, available)
        step = self.action["qty_step"]
        qty = math.floor((qty + 1e-14) / step) * step
        if qty <= 0:
            return
        self.filled_qty += qty
        self.fills.append({
            "fill_ms": now,
            "price": self.action["price"],
            "qty": qty,
            "notional_krw": qty * self.action["price"],
            "markouts": {},
        })
        if self.filled_qty >= self.action["qty"] - step * 0.1:
            self.active = False
            self.entry_done = True
            self.entry_reason = "filled"

    def observe(self, now, state):
        self.advance(now)
        for fill in self.fills:
            for horizon in MARKOUT_HORIZONS_MS:
                key = str(horizon)
                if key in fill["markouts"] or now < fill["fill_ms"] + horizon:
                    continue
                status = "valued"
                vwap = None
                if not state or not state.get("entry_fresh"):
                    status = "observation_unavailable"
                else:
                    bids = [
                        (price, qty * self.cfg["depth_haircut"])
                        for price, qty in state["bids"]
                    ]
                    vwap = liquidate(bids, fill["qty"])
                    if vwap is None:
                        status = "depth_unavailable"
                fill["markouts"][key] = {
                    "status": status,
                    "target_ms": fill["fill_ms"] + horizon,
                    "observed_ms": now,
                    "observation_lag_ms": now - (fill["fill_ms"] + horizon),
                    "exit_vwap": vwap,
                    "markout_krw": (
                        (vwap - fill["price"]) * fill["qty"]
                        if vwap is not None else None
                    ),
                    "markout_bp": (
                        (vwap / fill["price"] - 1) * 10000
                        if vwap is not None else None
                    ),
                }

    def result(self, boundary_ms):
        self.advance(boundary_ms)
        entry_censored = not self.entry_done
        if entry_censored:
            self.entry_reason = "session_boundary"
        for fill in self.fills:
            for horizon in MARKOUT_HORIZONS_MS:
                key = str(horizon)
                if key not in fill["markouts"]:
                    fill["markouts"][key] = {
                        "status": "censored",
                        "target_ms": fill["fill_ms"] + horizon,
                        "observed_ms": None,
                        "observation_lag_ms": None,
                        "exit_vwap": None,
                        "markout_krw": None,
                        "markout_bp": None,
                    }
        return {
            "schema": 1,
            "kind": "local_minimum_passive_fill_markout",
            "orders_enabled": False,
            "candidate_id": self.action["candidate_id"],
            "coin": self.action["coin"],
            "reference_group": self.group,
            "action": deepcopy(self.action),
            "entry_done": self.entry_done,
            "entry_censored": entry_censored,
            "entry_reason": self.entry_reason,
            "filled_qty": self.filled_qty,
            "fills": deepcopy(self.fills),
            "boundary_ms": boundary_ms,
            "interpretation": "diagnostic_only_not_a_policy_or_portfolio_pnl",
        }


def markout_report(rows):
    result = {}
    for group in REFERENCE_GROUPS:
        probes = [row for row in rows if row["reference_group"] == group]
        fills = [fill for row in probes for fill in row["fills"]]
        horizons = {}
        for horizon in MARKOUT_HORIZONS_MS:
            pairs = [
                (fill, fill["markouts"][str(horizon)]) for fill in fills
            ]
            labels = [label for _, label in pairs]
            valued = [
                (fill, label) for fill, label in pairs
                if label["status"] == "valued"
            ]
            values = [label["markout_bp"] for _, label in valued]
            valued_notional = sum(fill["notional_krw"] for fill, _ in valued)
            horizons[str(horizon)] = {
                "labels": len(labels),
                "valued": len(valued),
                "observation_unavailable": sum(
                    row["status"] == "observation_unavailable" for row in labels
                ),
                "depth_unavailable": sum(
                    row["status"] == "depth_unavailable" for row in labels
                ),
                "censored": sum(row["status"] == "censored" for row in labels),
                "mean_markout_bp_per_fill": (
                    sum(values) / len(values) if values else None
                ),
                "notional_weighted_markout_bp": (
                    sum(label["markout_krw"] for _, label in valued)
                    / valued_notional * 10000
                    if valued_notional else None
                ),
            }
        result[group] = {
            "probes": len(probes),
            "filled_probes": sum(row["filled_qty"] > 0 for row in probes),
            "fill_lots": len(fills),
            "entry_censored": sum(row["entry_censored"] for row in probes),
            "horizons_ms": horizons,
        }
    return result
