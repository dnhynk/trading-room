from __future__ import annotations
import json, os, sqlite3, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from ..contracts import OrderIntent, OrderPurpose, OrderStatus, ProtectionState, RiskState

ACTIVE_ENTRY = {"reserved", "submitting", "acknowledged", "result_unknown", "open", "partially_filled"}
TERMINAL = {"filled", "canceled", "rejected"}
def now() -> str: return datetime.now(timezone.utc).isoformat()

class ProcessLock:
    def __init__(self, state: Path): self.path = state.with_suffix(".lock"); self.fd: int | None = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try: self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(self.fd, str(os.getpid()).encode()); return self
        except FileExistsError as exc: raise RuntimeError("another ARX campaign process owns this state") from exc
    def __exit__(self, *_):
        if self.fd is not None: os.close(self.fd); self.path.unlink(missing_ok=True)

class LiveTransport:
    """A non-operational guard: no development route can write to an exchange."""
    writes = 0
    def submit(self, *_: Any, **__: Any) -> None: raise RuntimeError("live transport is intentionally non-operational")
    def cancel(self, *_: Any, **__: Any) -> None: raise RuntimeError("live transport is intentionally non-operational")

class PaperTransport:
    writes = 0
    def submit(self, client_oid: str, *_: Any, **__: Any) -> dict[str, str]: return {"clientOid": client_oid, "status": "acknowledged"}
    def cancel(self, client_oid: str) -> dict[str, str]: return {"clientOid": client_oid, "status": "cancel_pending"}

class CampaignEngine:
    def __init__(self, state: str | Path, transport: Any | None = None):
        self.path = Path(state); self.path.parent.mkdir(parents=True, exist_ok=True); self.transport = transport or PaperTransport()
        try: self.db = sqlite3.connect(self.path); self.db.row_factory = sqlite3.Row
        except sqlite3.Error as exc: raise RuntimeError("state database unavailable; emergency-halt required") from exc
        self.db.executescript("""create table if not exists orders (intention_id text unique, client_oid text unique, purpose text, side text, qty text, status text, exchange_id text, filled text default '0', protection text default 'unverified', created text, updated text, reason text); create table if not exists events (id integer primary key, at text, kind text, detail text); create table if not exists controls (key text primary key, value text);""")
        self.db.commit()
    @contextmanager
    def transaction(self):
        try:
            with self.db: yield
        except sqlite3.Error as exc: self.halt("storage_failure"); raise RuntimeError("durable reservation failed; entries halted") from exc
    def event(self, kind: str, **detail: Any): self.db.execute("insert into events(at,kind,detail) values(?,?,?)", (now(),kind,json.dumps(detail,default=str)))
    def control(self, key: str, default: str = "NORMAL") -> str:
        row=self.db.execute("select value from controls where key=?",(key,)).fetchone(); return row[0] if row else default
    def set_control(self, state: RiskState, reason: str):
        with self.transaction(): self.db.execute("insert into controls values('risk_state',?) on conflict(key) do update set value=excluded.value",(state.value,)); self.event("control",state=state.value,reason=reason)
    def halt(self, reason: str): self.set_control(RiskState.EMERGENCY_HALT, reason)
    def reserve(self, intent: OrderIntent) -> str:
        state=self.control("risk_state")
        entry=intent.purpose in {OrderPurpose.PROBE_ENTRY,OrderPurpose.PYRAMID_ENTRY}
        if entry and state != RiskState.NORMAL.value: raise RuntimeError(f"entries blocked: {state}")
        if intent.side == "sell" and not intent.reduce_only: raise ValueError("one-way no-short invariant")
        oid="arx-"+uuid.uuid4().hex
        with self.transaction():
            self.db.execute("insert into orders values(?,?,?,?,?,'reserved',null,'0','unverified',?,?,?)",(intent.intention_id,oid,intent.purpose.value,intent.side,str(intent.quantity_base),now(),now(),",".join(intent.reason_codes)))
            self.event("reserved",intention=intent.intention_id,clientOid=oid)
        return oid
    def submit(self, intention_id: str) -> str:
        row=self.db.execute("select * from orders where intention_id=?",(intention_id,)).fetchone()
        if not row: raise KeyError(intention_id)
        if row["status"] not in {"reserved","result_unknown"}: return row["client_oid"]
        with self.transaction(): self.db.execute("update orders set status='submitting',updated=? where intention_id=?",(now(),intention_id)); self.event("submitting",intention=intention_id)
        try: reply=self.transport.submit(row["client_oid"], row["side"], Decimal(row["qty"]), row["purpose"])
        except TimeoutError:
            with self.transaction(): self.db.execute("update orders set status='result_unknown',updated=? where intention_id=?",(now(),intention_id)); self.event("ambiguous_timeout",intention=intention_id)
            return row["client_oid"]
        with self.transaction(): self.db.execute("update orders set status='acknowledged',exchange_id=?,updated=? where intention_id=?",(reply.get("orderId"),now(),intention_id)); self.event("ack",intention=intention_id)
        return row["client_oid"]
    def reconcile(self, client_oid: str, status: str, filled: Decimal = Decimal("0"), protection_qty: Decimal = Decimal("0")):
        if status not in {x.value for x in OrderStatus}: raise ValueError("unknown order state")
        row=self.db.execute("select * from orders where client_oid=?",(client_oid,)).fetchone()
        if not row: return False
        # A cancel race cannot erase a fill.
        if filled > Decimal(row["filled"]): status = "filled" if filled >= Decimal(row["qty"]) else "partially_filled"
        protection = "active" if protection_qty >= filled and filled > 0 else ("insufficient" if filled > 0 else row["protection"])
        with self.transaction():
            self.db.execute("update orders set status=?,filled=?,protection=?,updated=? where client_oid=?",(status,str(max(filled,Decimal(row["filled"]))),protection,now(),client_oid)); self.event("reconciled",clientOid=client_oid,status=status,filled=str(filled),protection=protection)
            if protection == "insufficient": self.set_control(RiskState.PAUSE_ENTRIES,"protection_gap")
        return True
    def cancel_entry_orders(self):
        with self.transaction():
            rows=self.db.execute("select client_oid from orders where purpose in ('probe_entry','pyramid_entry') and status not in ('filled','canceled','rejected')").fetchall()
            for r in rows: self.db.execute("update orders set status='cancel_pending',updated=? where client_oid=?",(now(),r[0])); self.event("cancel_requested",clientOid=r[0])
        return len(rows)
    def request_exit(self): self.set_control(RiskState.EXIT_ONLY,"operator_requested_exit")
    def status(self): return {"risk_state":self.control("risk_state"),"orders":[dict(x) for x in self.db.execute("select * from orders order by created")]}
    def close(self): self.db.close()
