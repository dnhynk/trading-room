"""Durable fail-closed order lifecycle; no operational live transport exists."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from ..contracts import (
    OperatingMode,
    OrderIntent,
    OrderPurpose,
    OrderStatus,
    RiskApproval,
    RiskState,
    utc,
)


ZERO = Decimal("0")
TERMINAL = {"filled", "canceled", "rejected"}
ENTRY_PURPOSES = {OrderPurpose.PROBE_ENTRY.value, OrderPurpose.PYRAMID_ENTRY.value}
TRANSITIONS = {
    "reserved": {"submitting", "canceled", "rejected"},
    "submitting": {
        "acknowledged",
        "result_unknown",
        "rejected",
        "open",
        "partially_filled",
        "filled",
    },
    "acknowledged": {
        "open",
        "partially_filled",
        "filled",
        "cancel_pending",
        "canceled",
        "rejected",
    },
    "result_unknown": {
        "acknowledged",
        "open",
        "partially_filled",
        "filled",
        "canceled",
        "rejected",
    },
    "open": {"partially_filled", "filled", "cancel_pending", "canceled", "rejected"},
    "partially_filled": {
        "partially_filled",
        "filled",
        "cancel_pending",
        "canceled",
        "rejected",
    },
    "cancel_pending": {"partially_filled", "filled", "canceled", "rejected"},
    "filled": {"filled"},
    "canceled": {"canceled"},
    "rejected": {"rejected"},
}
RISK_STATE_RANK = {
    RiskState.NORMAL: 0,
    RiskState.PAUSE_ENTRIES: 1,
    RiskState.EXIT_ONLY: 2,
    RiskState.EMERGENCY_HALT: 3,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProcessLock:
    """An existing lock is evidence; stale locks are never deleted automatically."""

    def __init__(self, state: Path) -> None:
        self.path = state.with_suffix(state.suffix + ".lock")
        self.fd: int | None = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, f"pid={os.getpid()}\ncreated={now()}\n".encode())
            return self
        except FileExistsError as exc:
            raise RuntimeError(
                "ARX state lock exists; reconcile its owner manually (stale locks are preserved)"
            ) from exc

    def __exit__(self, *_: object) -> None:
        if self.fd is None:
            return
        os.close(self.fd)
        self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class LiveTransport:
    """Development hard-stop. It deliberately has no signer or HTTP write path."""

    writes = 0

    def submit(self, *_: Any, **__: Any) -> None:
        raise RuntimeError("live transport is intentionally non-operational")

    def cancel(self, *_: Any, **__: Any) -> None:
        raise RuntimeError("live transport is intentionally non-operational")


class PaperTransport:
    writes = 0

    def submit(self, client_oid: str, *_: Any, **__: Any) -> dict[str, str]:
        return {"clientOid": client_oid, "status": "acknowledged"}

    def cancel(self, client_oid: str) -> dict[str, str]:
        return {"clientOid": client_oid, "status": "cancel_pending"}


def uta_v3_order_payload(
    *,
    client_oid: str,
    side: str,
    quantity: Decimal,
    reduce_only: bool,
    order_type: str = "market",
    price: Decimal | None = None,
    time_in_force: str | None = None,
) -> dict[str, str]:
    """Pure UTA-v3 serializer; it never signs or submits a request."""

    if len(client_oid) > 32 or not client_oid.replace("-", "").isalnum():
        raise ValueError("invalid UTA v3 clientOid")
    if side not in {"buy", "sell"} or quantity <= ZERO:
        raise ValueError("invalid UTA v3 order")
    if side == "sell" and not reduce_only:
        raise ValueError("one-way long sell must be reduce-only")
    if side == "buy" and reduce_only:
        raise ValueError("long-only buy cannot be reduce-only")
    if order_type not in {"market", "limit"}:
        raise ValueError("unsupported UTA v3 order type")
    if order_type == "limit" and (price is None or price <= ZERO):
        raise ValueError("limit order requires a positive price")
    if order_type == "market" and price is not None:
        raise ValueError("UTA v3 market orders do not accept price")
    normalized_tif = time_in_force.lower() if time_in_force is not None else None
    if order_type == "limit" and normalized_tif is None:
        normalized_tif = "gtc"
    if normalized_tif not in {None, "ioc", "fok", "gtc", "post_only"}:
        raise ValueError("unsupported UTA v3 timeInForce")
    payload = {
        "category": "USDT-FUTURES",
        "symbol": "ARXUSDT",
        "marginMode": "isolated",
        "side": side,
        "orderType": order_type,
        "qty": str(quantity),
        "clientOid": client_oid,
        "reduceOnly": "yes" if reduce_only else "no",
    }
    if price is not None:
        payload["price"] = str(price)
    if normalized_tif is not None:
        payload["timeInForce"] = normalized_tif
    return payload


def uta_v3_protective_stop_payload(
    *,
    client_oid: str,
    quantity: Decimal,
    stop_price: Decimal,
) -> dict[str, str]:
    """Serialize a documented one-way partial TPSL; never sign or submit it.

    UTA strategy orders use a different contract from ordinary orders.  The
    position quantity is expressed in base coin, ``posSide`` is intentionally
    omitted in one-way mode, and the exit is explicitly reduce-only.  This
    mapper is not evidence that a particular account accepts the combination;
    that remains a private capability-test requirement for live validation.
    """

    if len(client_oid) > 32 or not client_oid.replace("-", "").isalnum():
        raise ValueError("invalid UTA v3 clientOid")
    if quantity <= ZERO or stop_price <= ZERO:
        raise ValueError("protective stop quantity and price must be positive")
    return {
        "category": "USDT-FUTURES",
        "symbol": "ARXUSDT",
        "type": "tpsl",
        "side": "sell",
        "qty": str(quantity),
        "clientOid": client_oid,
        "tpslMode": "partial",
        "reduceOnly": "yes",
        "slTriggerBy": "mark",
        "stopLoss": str(stop_price),
        "slOrderType": "market",
    }


class CampaignEngine:
    def __init__(
        self,
        state: str | Path,
        transport: Any | None = None,
        operating_mode: OperatingMode = OperatingMode.PAPER,
    ) -> None:
        if operating_mode is OperatingMode.LIVE:
            raise RuntimeError(
                "live execution is not available in this build; private writes remain disabled"
            )
        self.path = Path(state)
        if not self.path.is_absolute():
            raise ValueError("execution state path must be absolute")
        self.operating_mode = operating_mode
        self.transport = transport or PaperTransport()
        if isinstance(self.transport, LiveTransport):
            raise RuntimeError("live transport cannot be attached to a non-live development engine")
        self._closed = False
        self._lock = ProcessLock(self.path).__enter__()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.row_factory = sqlite3.Row
            self._create_schema()
            # Every process incarnation starts unverified. REST + websocket
            # reconciliation must explicitly unlock entries.
            self.db.execute(
                "INSERT INTO controls(key,value) VALUES('startup_reconciled','0') "
                "ON CONFLICT(key) DO UPDATE SET value='0'"
            )
            self.db.commit()
        except Exception:
            self._lock.__exit__()
            raise

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders(
                intention_id TEXT PRIMARY KEY,
                client_oid TEXT UNIQUE NOT NULL,
                campaign_id TEXT NOT NULL,
                purpose TEXT NOT NULL,
                side TEXT NOT NULL,
                qty TEXT NOT NULL,
                status TEXT NOT NULL,
                exchange_id TEXT,
                filled TEXT NOT NULL DEFAULT '0',
                protection TEXT NOT NULL DEFAULT 'unverified',
                created TEXT NOT NULL,
                updated TEXT NOT NULL,
                reason TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                approval_id TEXT,
                approval_expires TEXT,
                market_observed TEXT,
                account_observed TEXT,
                worst_fill TEXT,
                risk_principal_loss TEXT,
                risk_giveback TEXT,
                risk_gross_stop TEXT,
                risk_gross_notional TEXT,
                risk_isolated_margin TEXT,
                reconciliation_required INTEGER NOT NULL DEFAULT 0,
                protection_order_id TEXT,
                protection_active INTEGER NOT NULL DEFAULT 0,
                protection_trigger_reference TEXT,
                protection_covered_qty TEXT NOT NULL DEFAULT '0',
                protection_observed TEXT,
                protection_valid_until TEXT
            );
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS controls(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS protection_snapshots(
                id INTEGER PRIMARY KEY,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                exchange_order_id TEXT,
                active INTEGER NOT NULL,
                trigger_reference TEXT,
                stop_price TEXT,
                covered_quantity TEXT NOT NULL,
                aggregate_position_quantity TEXT NOT NULL,
                valid_until TEXT,
                sufficient INTEGER NOT NULL
            );
            """
        )

    def __enter__(self) -> "CampaignEngine":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.execute("COMMIT")
        except Exception as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                try:
                    self.db.execute(
                        "INSERT INTO controls(key,value) VALUES('risk_state',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (RiskState.EMERGENCY_HALT.value,),
                    )
                    self.db.commit()
                except sqlite3.Error:
                    pass
                raise RuntimeError(
                    "state database failure; entries are not safe until manual reconciliation"
                ) from exc
            raise

    def event(self, kind: str, **detail: Any) -> None:
        self.db.execute(
            "INSERT INTO events(at,kind,detail) VALUES(?,?,?)",
            (now(), kind, json.dumps(detail, default=str, sort_keys=True)),
        )

    def control(self, key: str = "risk_state", default: str = "NORMAL") -> str:
        row = self.db.execute(
            "SELECT value FROM controls WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def _set_control_no_transaction(self, state: RiskState, reason: str) -> None:
        current = RiskState(self.control())
        if RISK_STATE_RANK[state] < RISK_STATE_RANK[current]:
            self.event(
                "control_relaxation_rejected",
                current=current.value,
                requested=state.value,
                reason=reason,
            )
            return
        self.db.execute(
            "INSERT INTO controls(key,value) VALUES('risk_state',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (state.value,),
        )
        self.event("control", state=state.value, reason=reason)

    def set_control(self, state: RiskState, reason: str) -> None:
        with self.transaction():
            self._set_control_no_transaction(state, reason)

    def halt(self, reason: str) -> None:
        if self.control() != RiskState.EMERGENCY_HALT.value:
            self.set_control(RiskState.EMERGENCY_HALT, reason)

    @staticmethod
    def client_oid(intent: OrderIntent) -> str:
        digest = hashlib.sha256(
            f"{intent.campaign_id}|{intent.intention_id}|{intent.config_hash}".encode()
        ).hexdigest()
        return "arx-" + digest[:28]

    @staticmethod
    def _validate_approval(
        intent: OrderIntent,
        approval: RiskApproval | None,
        current_config_hash: str | None,
        at: datetime,
    ) -> RiskApproval:
        if approval is None:
            raise RuntimeError("entry has no aggregate risk approval")
        if current_config_hash is None or current_config_hash != intent.config_hash:
            raise RuntimeError("current configuration was not re-read or does not match")
        if approval.risk_state is not RiskState.NORMAL:
            raise RuntimeError("risk approval is not NORMAL")
        if (
            approval.intention_id != intent.intention_id
            or approval.campaign_id != intent.campaign_id
            or approval.config_hash != intent.config_hash
            or approval.approved_quantity_base != intent.quantity_base
            or approval.market_observed_at != intent.market_observed_at
        ):
            raise RuntimeError("risk approval does not bind the exact intent")
        if at >= approval.expires_at or at >= intent.expires_at:
            raise RuntimeError("risk or market approval is stale")
        return approval

    def reserve(
        self,
        intent: OrderIntent,
        *,
        approval: RiskApproval | None = None,
        current_config_hash: str | None = None,
        worst_fill_price: Decimal | None = None,
        reconciled_long_exposure: Decimal | None = None,
        at: datetime | None = None,
    ) -> str:
        observed_now = utc(at or datetime.now(timezone.utc))
        entry = intent.purpose.value in ENTRY_PURPOSES
        if entry:
            if self.control() != RiskState.NORMAL.value:
                raise RuntimeError(f"entries blocked: {self.control()}")
            if self.control("startup_reconciled", "0") != "1":
                raise RuntimeError("entries blocked until restart snapshot reconciliation")
            approval = self._validate_approval(
                intent, approval, current_config_hash, observed_now
            )
            if worst_fill_price is None or worst_fill_price <= ZERO:
                raise RuntimeError("entry reservation needs the approved worst fill")
        if intent.side == "sell":
            if reconciled_long_exposure is None:
                raise RuntimeError("reduction needs reconciled one-way long exposure")
            if intent.quantity_base > reconciled_long_exposure:
                raise ValueError("reduce quantity exceeds reconciled long exposure")
            active = self.db.execute(
                "SELECT qty,filled FROM orders WHERE side='sell' "
                "AND status NOT IN ('filled','canceled','rejected')"
            ).fetchall()
            reserved_remaining = sum(
                (max(ZERO, Decimal(row[0]) - Decimal(row[1])) for row in active), ZERO
            )
            if reserved_remaining + intent.quantity_base > reconciled_long_exposure:
                raise ValueError(
                    "overlapping reduce reservations exceed reconciled long exposure"
                )

        client_oid = self.client_oid(intent)
        prior = self.db.execute(
            "SELECT client_oid,config_hash,qty FROM orders WHERE intention_id=?",
            (intent.intention_id,),
        ).fetchone()
        if prior:
            if prior[1] != intent.config_hash or Decimal(prior[2]) != intent.quantity_base:
                raise RuntimeError("intention ID was reused for different content")
            return str(prior[0])

        worst_fill = worst_fill_price or intent.limit_price
        with self.transaction():
            self.db.execute(
                """
                INSERT INTO orders(
                    intention_id,client_oid,campaign_id,purpose,side,qty,status,
                    exchange_id,filled,protection,created,updated,reason,config_hash,
                    approval_id,approval_expires,market_observed,account_observed,
                    worst_fill,risk_principal_loss,risk_giveback,risk_gross_stop,
                    risk_gross_notional,risk_isolated_margin
                ) VALUES(?,?,?,?,?,?,'reserved',NULL,'0','unverified',?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    intent.intention_id,
                    client_oid,
                    intent.campaign_id,
                    intent.purpose.value,
                    intent.side,
                    str(intent.quantity_base),
                    observed_now.isoformat(),
                    observed_now.isoformat(),
                    ",".join(intent.reason_codes),
                    intent.config_hash,
                    approval.approval_id if approval else None,
                    approval.expires_at.isoformat() if approval else None,
                    intent.market_observed_at.isoformat(),
                    approval.account_observed_at.isoformat()
                    if approval and approval.account_observed_at
                    else None,
                    str(worst_fill) if worst_fill is not None else None,
                    str(approval.principal_loss_at_stop_usdt) if approval else None,
                    str(approval.giveback_at_stop_usdt) if approval else None,
                    str(approval.gross_stop_risk_usdt) if approval else None,
                    str(approval.gross_notional_usdt) if approval else None,
                    str(approval.isolated_margin_usdt) if approval else None,
                ),
            )
            self.event(
                "risk_approved_and_reserved",
                intention=intent.intention_id,
                approval=approval.approval_id if approval else None,
                clientOid=client_oid,
                worst_fill=str(worst_fill) if worst_fill is not None else None,
            )
        return client_oid

    def submit(
        self,
        intention_id: str,
        *,
        current_config_hash: str | None = None,
        market_observed_at: datetime | None = None,
        account_observed_at: datetime | None = None,
        at: datetime | None = None,
    ) -> str:
        row = self.db.execute(
            "SELECT * FROM orders WHERE intention_id=?", (intention_id,)
        ).fetchone()
        if not row:
            raise KeyError(intention_id)
        if row["status"] == "result_unknown" or row["reconciliation_required"]:
            raise RuntimeError("RESULT_UNKNOWN requires clientOid reconciliation before retry")
        if row["status"] != "reserved":
            return str(row["client_oid"])
        entry = row["purpose"] in ENTRY_PURPOSES
        sent_at = utc(at or datetime.now(timezone.utc))
        if entry:
            if self.control() != RiskState.NORMAL.value or self.control(
                "startup_reconciled", "0"
            ) != "1":
                raise RuntimeError("entry controls changed after reservation")
            if current_config_hash is None or current_config_hash != row["config_hash"]:
                raise RuntimeError("configuration was not freshly revalidated")
            if market_observed_at is None or utc(market_observed_at).isoformat() != row[
                "market_observed"
            ]:
                raise RuntimeError("market snapshot changed after risk approval")
            stored_account = row["account_observed"]
            supplied_account = (
                utc(account_observed_at).isoformat() if account_observed_at else None
            )
            if stored_account != supplied_account:
                raise RuntimeError("account snapshot changed after risk approval")
            if not row["approval_expires"] or sent_at >= datetime.fromisoformat(
                row["approval_expires"]
            ):
                raise RuntimeError("risk approval expired before send")

        with self.transaction():
            self.db.execute(
                "UPDATE orders SET status='submitting',updated=? WHERE intention_id=?",
                (sent_at.isoformat(), intention_id),
            )
            self.event("submitting", intention=intention_id)
        try:
            reply = self.transport.submit(
                row["client_oid"], row["side"], Decimal(row["qty"]), row["purpose"]
            )
        except (TimeoutError, ConnectionError):
            with self.transaction():
                self.db.execute(
                    "UPDATE orders SET status='result_unknown',reconciliation_required=1,updated=? "
                    "WHERE intention_id=?",
                    (now(), intention_id),
                )
                self.event(
                    "ambiguous_submit",
                    intention=intention_id,
                    clientOid=row["client_oid"],
                )
                self._set_control_no_transaction(
                    RiskState.PAUSE_ENTRIES, "ambiguous_entry_submit"
                )
            return str(row["client_oid"])
        if reply.get("clientOid") not in {None, row["client_oid"]}:
            self.halt("exchange_ack_identity_mismatch")
            raise RuntimeError("exchange ACK returned a different clientOid")
        with self.transaction():
            self.db.execute(
                "UPDATE orders SET status='acknowledged',exchange_id=?,updated=? "
                "WHERE intention_id=?",
                (reply.get("orderId"), now(), intention_id),
            )
            self.event("ack", intention=intention_id, final=False)
        return str(row["client_oid"])

    def reconcile(
        self,
        client_oid: str,
        status: str,
        filled: Decimal = ZERO,
        protection_qty: Decimal = ZERO,
        *,
        exchange_order_id: str | None = None,
        protection_order_id: str | None = None,
        protection_active: bool = False,
        trigger_reference: str | None = None,
        protection_observed_at: datetime | None = None,
        protection_valid_until: datetime | None = None,
        protection_source: str | None = None,
        protection_stop_price: Decimal | None = None,
        aggregate_position_qty: Decimal | None = None,
        not_found: bool = False,
        reconciliation_attempts: int = 0,
        reconciliation_window_expired: bool = False,
        reconciled_long_exposure: Decimal | None = None,
        at: datetime | None = None,
    ) -> bool:
        if status not in {item.value for item in OrderStatus}:
            raise ValueError("unknown order state")
        row = self.db.execute(
            "SELECT * FROM orders WHERE client_oid=?", (client_oid,)
        ).fetchone()
        if not row:
            return False
        old, prior = row["status"], Decimal(row["filled"])
        if filled < prior:
            raise ValueError("filled quantity cannot decrease")
        if filled > Decimal(row["qty"]):
            raise ValueError("fill exceeds order quantity")
        if not_found:
            if not (
                old == "result_unknown"
                and reconciliation_attempts >= 2
                and reconciliation_window_expired
            ):
                raise RuntimeError(
                    "not-found needs repeated clientOid queries and an expired reconciliation window"
                )
            status = "rejected"
        if filled > ZERO:
            status = "filled" if filled >= Decimal(row["qty"]) else "partially_filled"
        if status != old and status not in TRANSITIONS.get(old, set()):
            raise ValueError(f"invalid order transition {old}->{status}")
        if (
            row["side"] == "sell"
            and reconciled_long_exposure is not None
            and filled > reconciled_long_exposure
        ):
            raise ValueError("sell fill exceeds reconciled long exposure")

        reconciled_at = utc(at or datetime.now(timezone.utc))
        observed = utc(protection_observed_at) if protection_observed_at else None
        valid_until = utc(protection_valid_until) if protection_valid_until else None
        queried_active = bool(
            protection_active
            and protection_order_id
            and trigger_reference in {"mark_price", "last_price", "index_price"}
            and observed
            and valid_until
            and observed <= reconciled_at
            and reconciled_at - observed <= timedelta(seconds=30)
            and valid_until > reconciled_at
            and protection_source == "server_query"
            and protection_stop_price is not None
            and protection_stop_price > ZERO
        )
        protected = protection_qty if queried_active else ZERO
        required_protection = (
            aggregate_position_qty
            if row["purpose"] in ENTRY_PURPOSES and aggregate_position_qty is not None
            else filled
            if row["purpose"] in ENTRY_PURPOSES
            else ZERO
        )
        protection = row["protection"]
        if required_protection > ZERO:
            protection = (
                "active" if queried_active and protected >= required_protection else "insufficient"
            )

        with self.transaction():
            self.db.execute(
                """
                UPDATE orders SET status=?,filled=?,exchange_id=COALESCE(?,exchange_id),
                    protection=?,protection_order_id=?,protection_active=?,
                    protection_trigger_reference=?,protection_covered_qty=?,
                    protection_observed=?,protection_valid_until=?,
                    reconciliation_required=?,updated=?
                WHERE client_oid=?
                """,
                (
                    status,
                    str(filled),
                    exchange_order_id,
                    protection,
                    protection_order_id if queried_active else None,
                    int(queried_active),
                    trigger_reference if queried_active else None,
                    str(protected),
                    observed.isoformat() if observed else None,
                    valid_until.isoformat() if valid_until else None,
                    0 if old == "result_unknown" else row["reconciliation_required"],
                    now(),
                    client_oid,
                ),
            )
            self.event(
                "exchange_reconciled",
                clientOid=client_oid,
                status=status,
                filled=str(filled),
                protection=protection,
                server_query=queried_active,
            )
            if observed is not None:
                self.db.execute(
                    """
                    INSERT INTO protection_snapshots(
                        observed_at,source,exchange_order_id,active,
                        trigger_reference,stop_price,covered_quantity,
                        aggregate_position_quantity,valid_until,sufficient
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        observed.isoformat(),
                        protection_source or "unverified",
                        protection_order_id,
                        int(queried_active),
                        trigger_reference,
                        str(protection_stop_price) if protection_stop_price is not None else None,
                        str(protected),
                        str(required_protection),
                        valid_until.isoformat() if valid_until else None,
                        int(protection == "active"),
                    ),
                )
            if not_found:
                self.event(
                    "bounded_not_found",
                    clientOid=client_oid,
                    attempts=reconciliation_attempts,
                )
            if protection == "insufficient":
                self._set_control_no_transaction(RiskState.PAUSE_ENTRIES, "protection_gap")
        return True

    def cancel_entry_orders(self) -> int:
        with self.transaction():
            rows = self.db.execute(
                "SELECT client_oid,status FROM orders WHERE purpose IN "
                "('probe_entry','pyramid_entry') AND status NOT IN "
                "('filled','canceled','rejected')"
            ).fetchall()
            for row in rows:
                if row["status"] in {"submitting", "result_unknown"}:
                    self.db.execute(
                        "UPDATE orders SET reconciliation_required=1,updated=? WHERE client_oid=?",
                        (now(), row["client_oid"]),
                    )
                    self.event(
                        "entry_cancel_deferred_until_reconciliation",
                        clientOid=row["client_oid"],
                    )
                    continue
                target = "canceled" if row["status"] == "reserved" else "cancel_pending"
                self.db.execute(
                    "UPDATE orders SET status=?,updated=? WHERE client_oid=?",
                    (target, now(), row["client_oid"]),
                )
                self.event(
                    "entry_cancel_requested",
                    clientOid=row["client_oid"],
                    local_only=target == "canceled",
                )
        return len(rows)

    def request_exit(self) -> None:
        self.set_control(RiskState.EXIT_ONLY, "operator_requested_exit")

    def reconcile_restart(
        self,
        *,
        reconciled_long_exposure: Decimal,
        open_client_oids: set[str],
        protection_covered_quantity: Decimal = ZERO,
        account_snapshot_verified: bool = True,
        server_protection_verified: bool = False,
        foreign_open_orders_detected: bool = False,
        foreign_position_detected: bool = False,
    ) -> dict[str, int | str]:
        """Combine a caller-supplied REST snapshot with retained incremental state."""

        if reconciled_long_exposure < ZERO or protection_covered_quantity < ZERO:
            raise ValueError("reconciled exposure values cannot be negative")
        unresolved = 0
        with self.transaction():
            rows = self.db.execute(
                "SELECT client_oid,status,side,filled FROM orders "
                "WHERE status NOT IN ('filled','canceled','rejected')"
            ).fetchall()
            for row in rows:
                if row["client_oid"] not in open_client_oids:
                    self.db.execute(
                        "UPDATE orders SET reconciliation_required=1,updated=? WHERE client_oid=?",
                        (now(), row["client_oid"]),
                    )
                    unresolved += 1
                if row["side"] == "sell" and Decimal(row["filled"]) > reconciled_long_exposure:
                    unresolved += 1
            if reconciled_long_exposure > protection_covered_quantity:
                unresolved += 1
            if reconciled_long_exposure > ZERO and not server_protection_verified:
                unresolved += 1
            if not account_snapshot_verified:
                unresolved += 1
            if foreign_open_orders_detected or foreign_position_detected:
                unresolved += 1
            self.db.execute(
                "INSERT INTO controls(key,value) VALUES('startup_reconciled',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if unresolved == 0 else "0",),
            )
            if unresolved:
                self._set_control_no_transaction(
                    RiskState.PAUSE_ENTRIES, "restart_snapshot_requires_reconciliation"
                )
            self.event(
                "restart_snapshot",
                exposure=str(reconciled_long_exposure),
                protected=str(protection_covered_quantity),
                unresolved=unresolved,
            )
        return {
            "unresolved": unresolved,
            "reconciled_long_exposure": str(reconciled_long_exposure),
        }

    def status(self) -> dict[str, Any]:
        orders = [
            dict(row)
            for row in self.db.execute("SELECT * FROM orders ORDER BY created")
        ]
        return {
            "risk_state": self.control(),
            "startup_reconciled": self.control("startup_reconciled", "0") == "1",
            "result_unknown_count": sum(
                row["status"] == "result_unknown" for row in orders
            ),
            "unprotected_entry_count": sum(
                row["purpose"] in ENTRY_PURPOSES
                and Decimal(row["filled"]) > ZERO
                and row["protection"] != "active"
                for row in orders
            ),
            "latest_protection": (
                dict(latest)
                if (
                    latest := self.db.execute(
                        "SELECT * FROM protection_snapshots ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                )
                else None
            ),
            "orders": orders,
        }

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.db.execute(
                "INSERT INTO controls(key,value) VALUES('startup_reconciled','0') "
                "ON CONFLICT(key) DO UPDATE SET value='0'"
            )
            self.db.commit()
            self.db.close()
        finally:
            self._lock.__exit__()
            self._closed = True
