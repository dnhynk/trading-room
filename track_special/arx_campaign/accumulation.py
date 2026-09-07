"""Persistent adaptive accumulation paper runner and separate account monitor.

Only the Classic read client reaches the network. No exchange order or account
setting is written. Public observations and simulated fills are labelled apart
from authenticated observations of the real account.
"""

from __future__ import annotations

from dataclasses import asdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .execution.bitget_classic_v2 import ClassicReadOnlyClient
from .execution.engine import ProcessLock
from .strategy.accumulation import (
    D, ZERO, Frame, decide, liquidation_estimate, market_frame, number,
    simulated_fill, validate_config,
)


DEFAULT_SETTINGS = Path(__file__).resolve().parents[1] / "configs" / "accumulation.paper.json"


def encode(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)


def outside_repository(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("ABSOLUTE_EXTERNAL_STATE_DIRECTORY_REQUIRED")
    resolved = path.resolve()
    repository = Path(__file__).resolve().parents[2]
    if resolved == repository or resolved.is_relative_to(repository):
        raise ValueError("ACCUMULATION_STATE_MUST_BE_OUTSIDE_REPOSITORY")
    return resolved


def account_observation(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("api_family") != "classic_v2":
        raise ValueError("CLASSIC_ACCOUNT_REQUIRED")
    account, positions = raw["account"], raw["positions"]
    if (not isinstance(account, dict) or account.get("marginCoin") != "USDT"
            or account.get("assetMode") != "single" or not isinstance(positions, list)):
        raise ValueError("CLASSIC_ACCOUNT_SCHEMA_MISMATCH")
    available = number(account["available"])
    if available < ZERO:
        raise ValueError("INVALID_AVAILABLE_BALANCE")
    held = [p for p in positions if number(p["total"]) != ZERO]
    long_quantity, long_cost = ZERO, ZERO
    for position in held:
        if position.get("symbol") == "ARXUSDT" and position.get("holdSide") == "long":
            quantity = number(position["total"])
            long_quantity += quantity
            long_cost += quantity * number(position["openPriceAvg"])
    order_count = sum(int(raw[k]) for k in (
        "regular_orders", "partial_orders", "trigger_orders", "protective_orders"
    ))
    return {
        "observed_at_ms": raw["finished_ms"], "api_family": "classic_v2",
        "available_usdt": str(available), "position_count": len(held),
        "open_order_count": order_count, "arx_long_quantity": str(long_quantity),
        "arx_long_entry_notional": str(long_cost),
        "margin_mode": account.get("marginMode"), "position_mode": account.get("posMode"),
        "long_leverage": str(account.get("isolatedLongLever", "unverified")),
        "ownership": "observed_only_not_adopted", "exchange_writes": 0,
    }


class AccumulationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = outside_repository(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "accumulation.sqlite", timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, time_ms INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(
                id INTEGER PRIMARY KEY, time_ms INTEGER NOT NULL, payload TEXT NOT NULL);
        """)

    def get(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, encode(value)))

    def event(self, kind: str, payload: Any, timestamp: int) -> None:
        self.db.execute("INSERT INTO events(time_ms,kind,payload) VALUES(?,?,?)",
                        (timestamp, kind, encode(payload)))

    def initialize(self, account: dict[str, Any], binding: str, config_hash: str) -> dict[str, Any]:
        state = self.get("paper_state")
        if state is not None:
            if self.get("account_binding") != binding or self.get("config_hash") != config_hash:
                raise ValueError("CAMPAIGN_ACCOUNT_OR_CONFIG_CHANGED_USE_SEPARATE_STATE")
            return dict(state)
        if account["position_count"] or account["open_order_count"]:
            raise ValueError("CAPITAL_BOOTSTRAP_REQUIRES_FLAT_UNRESERVED_ACCOUNT")
        e0 = number(account["available_usdt"])
        if e0 <= ZERO:
            raise ValueError("NO_AVAILABLE_USDT")
        state = {
            "e0": str(e0), "quantity": "0", "notional": "0", "fees": "0",
            "funding": "0", "stop": "0", "last_fill_price": "0", "last_fill_ms": 0,
            "last_signal_bar_ms": None, "pending": False, "terminal": False,
            "next_funding_ms": None, "previous_funding_rate": "0", "fills": 0,
        }
        with self.db:
            self.put("account_binding", binding)
            self.put("config_hash", config_hash)
            self.put("paper_state", state)
            self.event("capital_snapshot_frozen", {"e0_usdt": str(e0)}, account["observed_at_ms"])
        return state

    def close(self) -> None:
        self.db.close()


def paper_step(frame: Frame, original: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[tuple[str, Any]]]:
    """A single atomic paper transition; persisted state prevents replayed clips."""
    state = dict(original)
    events: list[tuple[str, Any]] = []
    quantity = number(state["quantity"])
    deadline = state.get("next_funding_ms")
    if (deadline and frame.time_ms >= int(deadline)
            and int(deadline) > int(state.get("last_funding_ms", 0))
            and quantity > ZERO and not state["terminal"]):
        # Use the last observed rate and current mark, clearly an estimate.
        # A missed interval is never silently valued as zero funding.
        if frame.time_ms - int(deadline) > 60000:
            state["terminal"] = True
            events.append(("paper_halted", {"reason": "FUNDING_OBSERVATION_GAP"}))
        else:
            charge = quantity * frame.mark * number(state["previous_funding_rate"])
            state["funding"] = str(number(state["funding"]) + charge)
            state["last_funding_ms"] = int(deadline)
            events.append(("estimated_paper_funding", {"usdt": str(charge), "time_ms": deadline}))
    state["next_funding_ms"] = frame.next_funding_ms
    state["previous_funding_rate"] = str(frame.funding_rate)
    liquidation = liquidation_estimate(frame, state)
    stop = number(state["stop"])
    if quantity > ZERO and not state["terminal"] and frame.mark <= max(stop, liquidation):
        state["terminal"] = True
        state["exit_reason"] = "PAPER_LIQUIDATION" if frame.mark <= liquidation else "PAPER_STOP"
        state["exit_price"] = str(frame.bid)
        events.append(("simulated_exit_signal", {
            "reason": state["exit_reason"], "quantity": str(quantity),
            "reference_bid": str(frame.bid), "execution_guaranteed": False,
        }))
    decision = decide(frame, state, config)
    if decision.quantity > ZERO:
        filled, cost = simulated_fill(frame, decision)
        if filled >= frame.min_quantity and cost >= frame.min_notional:
            trial = dict(state)
            trial["quantity"] = str(quantity + filled)
            trial["notional"] = str(number(state["notional"]) + cost)
            trial["fees"] = str(number(state["fees"]) + cost * frame.taker_fee)
            # Never lower the stop when adding below average. No automatic top-up.
            trial_stop = max(
                stop, frame.range_low - frame.atr * number(config["structural_stop_atr"]),
                liquidation_estimate(frame, trial) + frame.atr * number(config["liquidation_buffer_atr"]),
            )
            if trial_stop >= frame.bid:
                events.append(("paper_entry_blocked", {"reason": "NO_STOP_LIQUIDATION_BUFFER"}))
            else:
                events.append(("paper_intent", asdict(decision)))
                state = trial
                state.update(stop=str(trial_stop), last_fill_price=str(cost / filled),
                             last_fill_ms=frame.time_ms, last_signal_bar_ms=frame.bar_ms,
                             fills=int(state["fills"]) + 1)
                events.append(("simulated_fill", {
                    "quantity": str(filled), "notional_usdt": str(cost),
                    "average_price": str(cost / filled), "fee_usdt": str(cost * frame.taker_fee),
                    "fill_model": "snapshot_ask_walk_not_actual_execution",
                    "e0_usdt": state["e0"], "total_quantity": state["quantity"],
                    "total_notional": state["notional"], "total_fees": state["fees"],
                    "total_funding": state["funding"], "close_fee_rate": str(frame.taker_fee),
                    "fill_number": state["fills"],
                }))
    held, cost = number(state["quantity"]), number(state["notional"])
    target = number(state["e0"]) * 10
    report = {
        "decision": asdict(decision), "paper_quantity": str(held),
        "paper_average_price": str(cost / held) if held else None,
        "paper_entry_notional_usdt": str(cost), "target_notional_usdt": str(target),
        "paper_target_fraction": str(cost / target), "paper_fill_count": state["fills"],
        "paper_stop": state["stop"], "paper_liquidation_estimate": str(liquidation_estimate(frame, state)),
        "paper_terminal": state["terminal"], "exchange_writes": 0,
        "signal": {"buy_share": str(frame.buy_share), "book_share": str(frame.book_share),
                   "contraction": str(frame.contraction), "range_low": str(frame.range_low),
                   "range_high": str(frame.range_high), "ask": str(frame.ask)},
        "evidence_kind": "public_market_with_simulated_execution",
    }
    return state, report, events


def status(directory: Path) -> dict[str, Any]:
    directory = outside_repository(directory)
    path = directory / "accumulation.sqlite"
    if not path.exists():
        return {"running": False, "status": "NOT_STARTED", "exchange_writes": 0}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='status'").fetchone()
    result = json.loads(row[0]) if row else {"status": "INITIALIZING"}
    age = int(time.time() * 1000) - int(result.get("heartbeat_ms", 0))
    result["heartbeat_age_seconds"] = age // 1000
    result["running"] = result.get("loop_state") == "running" and age < 90
    return dict(result)


def run(directory: Path, env_file: Path, settings: Path = DEFAULT_SETTINGS,
        samples: int = 0, notify_slack: bool = False) -> dict[str, Any]:
    config = json.loads(settings.read_text(encoding="utf-8"))
    validate_config(config)
    if samples < 0:
        raise ValueError("SAMPLES_MUST_BE_NONNEGATIVE")
    directory = outside_repository(directory)
    if (directory / "STOP").exists():
        raise ValueError("ACCUMULATION_LOCAL_STOP_PRESENT")
    config_hash = hashlib.sha256(encode(config).encode()).hexdigest()
    client = ClassicReadOnlyClient.from_env(env_file)
    with ProcessLock(directory / "accumulation.sqlite"):
        store = AccumulationStore(directory)
        report: dict[str, Any] = {}
        try:
            actual = account_observation(client.snapshot())
            state = store.initialize(actual, client.account_binding, config_hash)
            outbox = None
            if notify_slack:
                from .reporting.accumulation import AccumulationOutbox

                outbox = AccumulationOutbox(store.db, str(env_file))
                with store.db:
                    outbox.enqueue("paper-connected", {
                        "kind": "부팅", "head": "ARXUSDT 매집 관측 · 모의 운전 시작",
                        "fields": [["전략", "소량 확보 → 눌림 분할 매집 → 돌파 전 가속"],
                                   ["최초 시드 기준", f"{number(state['e0']):,.2f} USDT · 10배 목표"],
                                   ["실제 계좌 ARX 롱", f"{actual['arx_long_quantity']} ARX"],
                                   ["실제 주문 전송", "0건 · 모의 체결 알림만 활성"]],
                        "lines": ["*모의 운전입니다. 실제 선물 자동매매는 가동하지 않았습니다.*"],
                        "track": "SPECIAL-ARX", "balance": False,
                    })
            account_check = time.monotonic()
            count = 0
            while not (directory / "STOP").exists():
                tick = time.monotonic()
                timestamp = int(time.time() * 1000)
                try:
                    if tick - account_check >= 60:
                        actual = account_observation(client.snapshot())
                        account_check = tick
                    raw = client.market_snapshot()
                    frame = market_frame(raw, config, number(state["e0"]))
                    updated, paper, events = paper_step(frame, state, config)
                    report = {
                        "status": "OBSERVING_WITH_PAPER_ACCUMULATION", "loop_state": "running",
                        "pid": os.getpid(), "mode": "paper", "live_operational": False,
                        "heartbeat_ms": timestamp, "config_hash": config_hash,
                        "e0_usdt": state["e0"], "actual_account": actual, **paper,
                    }
                    with store.db:
                        store.db.execute("INSERT INTO observations(time_ms,payload) VALUES(?,?)",
                                         (frame.time_ms, encode(raw)))
                        for kind, payload in events:
                            store.event(kind, payload, frame.time_ms)
                            if outbox is not None and kind == "simulated_fill":
                                from .reporting.accumulation import fill_card

                                outbox.enqueue("paper-fill:" + str(payload["fill_number"]),
                                               fill_card(payload, frame.time_ms))
                        store.put("paper_state", updated)
                        store.put("status", report)
                    state = updated
                except (ValueError, KeyError, TypeError, ArithmeticError, RuntimeError) as exc:
                    # No raw response or exception text reaches logs. Stale proposals
                    # are replaced with a failed observation rather than reused.
                    reason = str(exc) if type(exc) is ValueError and str(exc).replace("_", "").isalnum() else type(exc).__name__
                    report = {
                        "status": "OBSERVATION_BLOCKED", "loop_state": "running", "pid": os.getpid(),
                        "mode": "paper", "live_operational": False, "exchange_writes": 0,
                        "heartbeat_ms": timestamp, "reason": reason, "e0_usdt": state["e0"],
                        "actual_account": actual, "paper_state_unchanged": True,
                    }
                    with store.db:
                        store.event("observation_failed", {"reason": reason}, timestamp)
                        store.put("status", report)
                if outbox is not None:
                    report["slack_notifications"] = outbox.flush(time.time())
                    with store.db:
                        store.put("status", report)
                print(encode(report), flush=True)
                count += 1
                if samples and count >= samples:
                    break
                until = time.monotonic() + max(0, config["poll_seconds"] - (time.monotonic() - tick))
                while time.monotonic() < until and not (directory / "STOP").exists():
                    time.sleep(min(1, max(0, until - time.monotonic())))
            return report
        finally:
            if report:
                report.update(loop_state="stopped", heartbeat_ms=int(time.time() * 1000))
                with store.db:
                    store.put("status", report)
            store.close()
