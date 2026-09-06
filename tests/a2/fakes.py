import copy
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

from track_a_2 import EXECUTION_VERSION
from track_a_2.execution.preflight import evaluation_config_digest, evaluation_source_digest
from track_a_2.settings import resolved_state_directory
from track_c.execution.coinone import CoinoneError


def approved_config(root, config):
    """Create a pinned synthetic evaluation artifact inside a test workspace."""
    root = Path(root)
    source = root / "track_a_2" / "evaluated_source.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# synthetic evaluated source\n", encoding="utf-8")
    common = root / "common"
    common.mkdir(parents=True, exist_ok=True)
    for name in ("cycle.py", "signal.py", "risk.py"):
        (common / name).write_text("# synthetic common source\n", encoding="utf-8")
    shared = root / "track_c" / "execution"
    shared.mkdir(parents=True, exist_ok=True)
    for name in ("coinone.py", "http_pool.py", "rate_limit.py"):
        (shared / name).write_text("# synthetic shared execution source\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    approval = config["live_approval_id"]
    relative = f"evaluations/{approval}.json"
    configured = dict(config, evaluation_manifest=relative)
    manifest = dict(
        schema=2,
        track="A-2",
        approval_id=approval,
        execution_version=EXECUTION_VERSION,
        config_digest=evaluation_config_digest(configured),
        source_digest=evaluation_source_digest(root),
        universe=configured["universe"],
        data_digest="a" * 64,
        result="APPROVED",
        protocol=dict(
            holdout=True, feed_contiguous=True, stress_passed=True,
            ladder_beats_one_unit=True,
            fixed_selection_from_seed=True, user_approved=True,
        ),
        execution=dict(
            maker_fee="0", taker_fee="0", latency_ms=250, depth_fraction="0.1",
        ),
        metrics=dict(
            main=dict(net_pnl_krw="2", campaigns=1, halt=None),
            one_unit=dict(net_pnl_krw="1", campaigns=1, halt=None),
            stress=dict(net_pnl_krw="1", campaigns=1, halt=None),
        ),
    )
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = resolved_state_directory(configured, root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    configured["evaluation_sha256"] = hashlib.sha256(raw).hexdigest()
    return configured


class Clock:
    def __init__(self, value=1_800_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Client:
    def __init__(self):
        self.rows = {}
        self.submissions = []
        self.cancellations = []
        self.submit_error = None
        self.accept_before_error = False
        self.cancel_error = None
        self.balance_rows = [dict(currency="KRW", available="100000", limit="0")]

    @staticmethod
    def row(order):
        return dict(
            quote_currency="KRW",
            target_currency=order["coin"],
            user_order_id=order["cid"],
            order_id="exchange-" + order["cid"],
            side=order["side"],
            status="NOT_TRIGGERED" if order["role"] == "protect" else "LIVE",
            executed_qty="0",
            average_executed_price="0",
            fee="0",
            remain_qty=order["qty"],
        )

    def submit(self, order, *, before_send=None):
        if before_send:
            before_send()
        saved = copy.deepcopy(order)
        self.submissions.append(saved)
        if self.accept_before_error:
            self.rows[order["cid"]] = self.row(order)
        if self.submit_error:
            raise self.submit_error
        self.rows[order["cid"]] = self.row(order)
        return {"order_id": self.rows[order["cid"]]["order_id"]}

    def detail(self, coin, cid):
        if cid not in self.rows:
            raise CoinoneError("Coinone API error 104", code=104)
        return copy.deepcopy(self.rows[cid])

    def fill(self, cid, qty, price, *, fee="0", status="FILLED"):
        row = self.rows[cid]
        row.update(
            executed_qty=str(qty),
            average_executed_price=str(price),
            fee=str(fee),
            status=status,
            remain_qty="0" if status in ("FILLED", "CANCELED") else str(D(row["remain_qty"]) - D(str(qty))),
        )

    def cancel(self, coin, cid):
        self.cancellations.append(cid)
        if self.cancel_error:
            raise self.cancel_error
        row = self.rows[cid]
        row.update(status="CANCELED", remain_qty="0")
        return {}

    def balances(self):
        return copy.deepcopy(self.balance_rows)

    def active_orders(self):
        terminal = {"FILLED", "CANCELED", "REJECTED", "NOT_TRIGGERED_CANCELED"}
        return [copy.deepcopy(row) for row in self.rows.values() if row["status"] not in terminal]
