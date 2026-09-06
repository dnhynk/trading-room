"""Durable fail-closed execution state machine; no live exchange transport exists."""
from __future__ import annotations
import hashlib, json, os, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from ..contracts import OrderIntent, OrderPurpose, OrderStatus, RiskState

TERMINAL = {"filled", "canceled", "rejected"}
TRANSITIONS = {"reserved":{"submitting","canceled","rejected"}, "submitting":{"acknowledged","result_unknown","rejected","open","partially_filled","filled"}, "acknowledged":{"open","partially_filled","filled","cancel_pending","canceled","rejected"}, "result_unknown":{"open","partially_filled","filled","canceled","rejected"}, "open":{"partially_filled","filled","cancel_pending","canceled","rejected"}, "partially_filled":{"partially_filled","filled","cancel_pending","canceled","rejected"}, "cancel_pending":{"partially_filled","filled","canceled","rejected"}, "filled":{"filled"}, "canceled":{"canceled"}, "rejected":{"rejected"}}
def now() -> str: return datetime.now(timezone.utc).isoformat()

class ProcessLock:
    """An existing lock, even stale, is evidence and is never deleted automatically."""
    def __init__(self, state: Path): self.path=state.with_suffix(state.suffix+".lock"); self.fd: int|None=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY); os.write(self.fd,f"pid={os.getpid()}\ncreated={now()}\n".encode()); return self
        except FileExistsError as exc: raise RuntimeError("ARX state lock exists; reconcile owner manually (stale locks are preserved)") from exc
    def __exit__(self,*_: Any):
        if self.fd is not None:
            os.close(self.fd); self.fd=None
            try: self.path.unlink()
            except FileNotFoundError: pass

class LiveTransport:
    writes=0
    def submit(self,*_: Any,**__: Any)->None: raise RuntimeError("live transport is intentionally non-operational")
    def cancel(self,*_: Any,**__: Any)->None: raise RuntimeError("live transport is intentionally non-operational")
class PaperTransport:
    writes=0
    def submit(self,client_oid: str,*_: Any,**__: Any)->dict[str,str]: return {"clientOid":client_oid,"status":"acknowledged"}
    def cancel(self,client_oid: str)->dict[str,str]: return {"clientOid":client_oid,"status":"cancel_pending"}

def uta_v3_order_payload(*, client_oid: str, side: str, quantity: Decimal, reduce_only: bool, order_type: str="market", price: Decimal|None=None)->dict[str,str]:
    """Pure serializer only: this module has no endpoint/signing code."""
    if len(client_oid)>32 or not client_oid.replace("-","").isalnum(): raise ValueError("invalid clientOid")
    if side not in {"buy","sell"} or quantity<=0: raise ValueError("invalid UTA v3 order")
    p={"category":"USDT-FUTURES","symbol":"ARXUSDT","side":side,"orderType":order_type,"size":str(quantity),"clientOid":client_oid,"reduceOnly":"YES" if reduce_only else "NO"}
    if price is not None: p["price"]=str(price)
    return p

class CampaignEngine:
    def __init__(self,state: str|Path,transport: Any|None=None):
        self.path=Path(state); self.transport=transport or PaperTransport(); self._closed=False; self._lock=ProcessLock(self.path).__enter__()
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True); self.db=sqlite3.connect(self.path); self.db.row_factory=sqlite3.Row
            self.db.executescript("""create table if not exists orders (intention_id text primary key,client_oid text unique,purpose text,side text,qty text,status text,exchange_id text,filled text default '0',protection text default 'unverified',created text,updated text,reason text,config_hash text,approval_expires text,worst_fill text,reserved_qty text,reconciliation_required integer default 0,protection_order_id text,protection_active integer default 0,protection_trigger_reference text,protection_covered_qty text default '0',protection_deadline text); create table if not exists events (id integer primary key,at text,kind text,detail text); create table if not exists controls (key text primary key,value text);""")
            existing={r[1] for r in self.db.execute("pragma table_info(orders)")}
            for n,s in {"config_hash":"text","approval_expires":"text","worst_fill":"text","reserved_qty":"text","reconciliation_required":"integer default 0","protection_order_id":"text","protection_active":"integer default 0","protection_trigger_reference":"text","protection_covered_qty":"text default '0'","protection_deadline":"text"}.items():
                if n not in existing: self.db.execute(f"alter table orders add column {n} {s}")
            self.db.commit()
        except Exception: self._lock.__exit__(); raise
    def __enter__(self): return self
    def __exit__(self,*_: Any): self.close()
    @contextmanager
    def transaction(self):
        try:
            with self.db: yield
        except sqlite3.Error as exc: self.halt("storage_failure"); raise RuntimeError("durable reservation failed; entries halted") from exc
    def event(self,kind: str,**detail: Any): self.db.execute("insert into events(at,kind,detail) values(?,?,?)",(now(),kind,json.dumps(detail,default=str,sort_keys=True)))
    def control(self,key: str="risk_state",default: str="NORMAL")->str:
        r=self.db.execute("select value from controls where key=?",(key,)).fetchone(); return r[0] if r else default
    def set_control(self,state: RiskState,reason: str):
        with self.transaction(): self.db.execute("insert into controls values('risk_state',?) on conflict(key) do update set value=excluded.value",(state.value,)); self.event("control",state=state.value,reason=reason)
    def halt(self,reason: str):
        if self.control()!=RiskState.EMERGENCY_HALT.value: self.set_control(RiskState.EMERGENCY_HALT,reason)
    @staticmethod
    def client_oid(intent: OrderIntent)->str: return "arx-"+hashlib.sha256(f"{intent.campaign_id}|{intent.intention_id}|{intent.config_hash}".encode()).hexdigest()[:28]
    def reserve(self,intent: OrderIntent,*,current_config_hash: str|None=None,approval_expires_at: datetime|None=None,worst_fill_price: Decimal|None=None,reconciled_long_exposure: Decimal|None=None)->str:
        entry=intent.purpose in {OrderPurpose.PROBE_ENTRY,OrderPurpose.PYRAMID_ENTRY}
        if entry and self.control()!=RiskState.NORMAL.value: raise RuntimeError(f"entries blocked: {self.control()}")
        if current_config_hash is not None and current_config_hash!=intent.config_hash: raise RuntimeError("stale configuration approval")
        if datetime.now(timezone.utc)>=intent.expires_at: raise RuntimeError("stale market approval")
        if approval_expires_at is not None and datetime.now(timezone.utc)>=approval_expires_at: raise RuntimeError("stale account approval")
        if intent.side=="sell":
            if not intent.reduce_only: raise ValueError("one-way no-short invariant")
            if reconciled_long_exposure is not None and intent.quantity_base>reconciled_long_exposure: raise ValueError("reduce quantity exceeds reconciled long exposure")
            # TP, stop and emergency reductions share one long exposure; reservations cannot overlap it.
            if reconciled_long_exposure is not None:
                active=self.db.execute("select qty from orders where side='sell' and status not in ('filled','canceled','rejected')").fetchall()
                if sum((Decimal(x[0]) for x in active), Decimal("0"))+intent.quantity_base > reconciled_long_exposure: raise ValueError("overlapping reduce reservations exceed reconciled long exposure")
        oid=self.client_oid(intent); prior=self.db.execute("select client_oid from orders where intention_id=?",(intent.intention_id,)).fetchone()
        if prior: return prior[0]
        worst=worst_fill_price or intent.limit_price or Decimal("1")
        with self.transaction():
            self.db.execute("insert into orders(intention_id,client_oid,purpose,side,qty,status,exchange_id,filled,protection,created,updated,reason,config_hash,approval_expires,worst_fill,reserved_qty) values(?,?,?,?,?,'reserved',null,'0','unverified',?,?,?,?,?,?,?)",(intent.intention_id,oid,intent.purpose.value,intent.side,str(intent.quantity_base),now(),now(),",".join(intent.reason_codes),intent.config_hash,approval_expires_at.isoformat() if approval_expires_at else None,str(worst),str(intent.quantity_base)))
            self.event("reserved",intention=intent.intention_id,clientOid=oid,worst_fill=str(worst))
        return oid
    def submit(self,intention_id: str)->str:
        r=self.db.execute("select * from orders where intention_id=?",(intention_id,)).fetchone()
        if not r: raise KeyError(intention_id)
        if r["status"]=="result_unknown" or r["reconciliation_required"]: raise RuntimeError("RESULT_UNKNOWN requires clientOid reconciliation before retry")
        if r["status"]!="reserved": return r["client_oid"]
        with self.transaction(): self.db.execute("update orders set status='submitting',updated=? where intention_id=?",(now(),intention_id)); self.event("submitting",intention=intention_id)
        try: reply=self.transport.submit(r["client_oid"],r["side"],Decimal(r["qty"]),r["purpose"])
        except TimeoutError:
            with self.transaction(): self.db.execute("update orders set status='result_unknown',reconciliation_required=1,updated=? where intention_id=?",(now(),intention_id)); self.event("ambiguous_timeout",intention=intention_id,clientOid=r["client_oid"])
            return r["client_oid"]
        with self.transaction(): self.db.execute("update orders set status='acknowledged',exchange_id=?,updated=? where intention_id=?",(reply.get("orderId"),now(),intention_id)); self.event("ack",intention=intention_id)
        return r["client_oid"]
    def reconcile(self,client_oid: str,status: str,filled: Decimal=Decimal("0"),protection_qty: Decimal=Decimal("0"),*,exchange_order_id: str|None=None,protection_order_id: str|None=None,protection_active: bool=False,trigger_reference: str|None=None,protection_deadline: datetime|None=None,not_found: bool=False,bounded_not_found: bool=False,reconciled_long_exposure: Decimal|None=None)->bool:
        if status not in {x.value for x in OrderStatus}: raise ValueError("unknown order state")
        r=self.db.execute("select * from orders where client_oid=?",(client_oid,)).fetchone()
        if not r: return False
        old,prior=r["status"],Decimal(r["filled"])
        if filled<prior: raise ValueError("filled quantity cannot decrease")
        if filled>Decimal(r["qty"]): raise ValueError("fill exceeds order quantity")
        if not_found:
            if old!="result_unknown" or not bounded_not_found: raise RuntimeError("not-found needs bounded reconciliation policy")
            status="rejected"; self.event("bounded_not_found",clientOid=client_oid)
        if filled>0: status="filled" if filled>=Decimal(r["qty"]) else "partially_filled"
        if status not in TRANSITIONS.get(old,set()): raise ValueError(f"invalid order transition {old}->{status}")
        if r["side"]=="sell" and reconciled_long_exposure is not None and filled>reconciled_long_exposure: raise ValueError("sell fill exceeds reconciled long exposure")
        active=bool(protection_active and protection_order_id and trigger_reference and protection_deadline and protection_deadline>datetime.now(timezone.utc)); protected=Decimal(protection_qty) if active else Decimal("0")
        protection="active" if filled>0 and protected>=filled else ("insufficient" if filled>0 else r["protection"])
        with self.transaction():
            self.db.execute("update orders set status=?,filled=?,exchange_id=coalesce(?,exchange_id),protection=?,protection_order_id=?,protection_active=?,protection_trigger_reference=?,protection_covered_qty=?,protection_deadline=?,reconciliation_required=?,updated=? where client_oid=?",(status,str(filled),exchange_order_id,protection,protection_order_id if active else None,int(active),trigger_reference if active else None,str(protected),protection_deadline.isoformat() if protection_deadline else None,0 if old=="result_unknown" else r["reconciliation_required"],now(),client_oid))
            self.event("reconciled",clientOid=client_oid,status=status,filled=str(filled),protection=protection,active=active)
            if protection=="insufficient": self.set_control(RiskState.PAUSE_ENTRIES,"protection_gap")
        return True
    def cancel_entry_orders(self):
        with self.transaction():
            rows=self.db.execute("select client_oid from orders where purpose in ('probe_entry','pyramid_entry') and status not in ('filled','canceled','rejected')").fetchall()
            for r in rows: self.db.execute("update orders set status='cancel_pending',updated=? where client_oid=?",(now(),r[0])); self.event("cancel_requested",clientOid=r[0])
        return len(rows)
    def request_exit(self): self.set_control(RiskState.EXIT_ONLY,"operator_requested_exit")
    def reconcile_restart(self, *, reconciled_long_exposure: Decimal, open_client_oids: set[str]) -> dict[str, int]:
        """Fail closed after restart: local in-flight state must be explained by an exchange snapshot."""
        unresolved=0
        with self.transaction():
            rows=self.db.execute("select client_oid,status,side,filled from orders where status not in ('filled','canceled','rejected')").fetchall()
            for r in rows:
                if r["client_oid"] not in open_client_oids and r["status"] not in {"result_unknown","cancel_pending"}:
                    self.db.execute("update orders set reconciliation_required=1,updated=? where client_oid=?",(now(),r["client_oid"])); unresolved+=1
                if r["side"]=="sell" and Decimal(r["filled"])>reconciled_long_exposure: unresolved+=1
            if unresolved: self.set_control(RiskState.PAUSE_ENTRIES,"restart_snapshot_requires_reconciliation")
            self.event("restart_snapshot",exposure=str(reconciled_long_exposure),unresolved=unresolved)
        return {"unresolved":unresolved,"reconciled_long_exposure":str(reconciled_long_exposure)}
    def status(self): return {"risk_state":self.control(),"orders":[dict(x) for x in self.db.execute("select * from orders order by created")]}
    def close(self):
        if not self._closed: self.db.close(); self._lock.__exit__(); self._closed=True
