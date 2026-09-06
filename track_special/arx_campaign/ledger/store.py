from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..contracts import utc

ZERO = Decimal("0")
@dataclass(frozen=True, slots=True)
class Allocation:
    reusable: Decimal
    reserve: Decimal
    high_water: Decimal

class LedgerStore:
    """Small durable slice: every write is one IMMEDIATE SQLite transaction."""
    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL, amount TEXT NOT NULL, reference TEXT UNIQUE, metadata TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS reservations(intention_id TEXT PRIMARY KEY, client_order_id TEXT UNIQUE NOT NULL, quantity TEXT NOT NULL, worst_fill TEXT NOT NULL, status TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS allocations(campaign_id TEXT PRIMARY KEY, high_water TEXT NOT NULL, reusable TEXT NOT NULL, reserve TEXT NOT NULL);
        """)
    def close(self) -> None: self.db.close()
    def reserve(self, intention_id: str, client_order_id: str, quantity: Decimal, worst_fill: Decimal, expires_at: datetime, event_id: str) -> bool:
        if quantity <= ZERO or worst_fill <= ZERO: raise ValueError("positive reservation required")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?)", (intention_id, client_order_id, str(quantity), str(worst_fill), "reserved", utc(expires_at).isoformat()))
            self.db.execute("INSERT INTO events(event_id,at,kind,amount,reference) VALUES(?,?,?,?,?)", (event_id, utc(expires_at).isoformat(), "ENTRY_RESERVED", str(quantity*worst_fill), intention_id))
            self.db.execute("COMMIT"); return True
        except sqlite3.IntegrityError:
            self.db.execute("ROLLBACK"); return False
    def transition_reservation(self, intention_id: str, status: str, at: datetime, event_id: str) -> bool:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row=self.db.execute("SELECT status FROM reservations WHERE intention_id=?", (intention_id,)).fetchone()
            if not row: self.db.execute("ROLLBACK"); return False
            self.db.execute("UPDATE reservations SET status=? WHERE intention_id=?", (status, intention_id))
            self.db.execute("INSERT INTO events(event_id,at,kind,amount,reference) VALUES(?,?,?,?,?)", (event_id, utc(at).isoformat(), "RESERVATION_"+status.upper(), "0", intention_id))
            self.db.execute("COMMIT"); return True
        except sqlite3.IntegrityError:
            self.db.execute("ROLLBACK"); return False
    def post_realized(self, event_id: str, at: datetime, kind: str, amount: Decimal, reference: str) -> bool:
        """Posts fee/funding/trading realization once; unique reference prevents double count."""
        if kind not in {"REALIZED_PNL", "FEE", "FUNDING"}: raise ValueError("unsupported realized event")
        try:
            self.db.execute("INSERT INTO events(event_id,at,kind,amount,reference) VALUES(?,?,?,?,?)", (event_id,utc(at).isoformat(),kind,str(amount),reference)); return True
        except sqlite3.IntegrityError: return False
    def allocate_new_high_water(self, campaign_id: str, cumulative_realized_net: Decimal) -> Allocation:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row=self.db.execute("SELECT high_water,reusable,reserve FROM allocations WHERE campaign_id=?", (campaign_id,)).fetchone()
            high,reuse,reserve=(map(Decimal,row) if row else (ZERO,ZERO,ZERO))
            increment=max(ZERO,cumulative_realized_net-high)
            high=max(high,cumulative_realized_net)
            reuse += increment*Decimal("0.25"); reserve += increment*Decimal("0.75")
            self.db.execute("INSERT INTO allocations VALUES(?,?,?,?) ON CONFLICT(campaign_id) DO UPDATE SET high_water=excluded.high_water,reusable=excluded.reusable,reserve=excluded.reserve",(campaign_id,str(high),str(reuse),str(reserve)))
            self.db.execute("COMMIT"); return Allocation(reuse,reserve,high)
        except Exception:
            self.db.execute("ROLLBACK"); raise
