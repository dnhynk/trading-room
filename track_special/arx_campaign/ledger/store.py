"""Transactional USDT-linear campaign ledger with typed double-entry postings."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..contracts import OrderStatus, utc


ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Allocation:
    reusable: Decimal
    reserve: Decimal
    high_water: Decimal


class LedgerStore:
    """Every mutation is one IMMEDIATE transaction and every posting balances."""

    def __init__(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(target), isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                amount TEXT NOT NULL,
                reference TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                UNIQUE(event_id, kind)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS events_exchange_identity
                ON events(kind, reference) WHERE reference IS NOT NULL;
            CREATE TABLE IF NOT EXISTS reservations(
                intention_id TEXT PRIMARY KEY,
                client_order_id TEXT UNIQUE NOT NULL,
                quantity TEXT NOT NULL,
                worst_fill TEXT NOT NULL,
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS allocations(
                campaign_id TEXT PRIMARY KEY,
                high_water TEXT NOT NULL,
                reusable TEXT NOT NULL,
                reserve TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS postings(
                id INTEGER PRIMARY KEY,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                debit_account TEXT NOT NULL,
                credit_account TEXT NOT NULL,
                amount TEXT NOT NULL CHECK(CAST(amount AS REAL) > 0),
                UNIQUE(event_id, event_type)
            );
            """
        )

    def __enter__(self) -> "LedgerStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    def _begin(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def _rollback(self) -> None:
        if self.db.in_transaction:
            self.db.execute("ROLLBACK")

    def reserve(
        self,
        intention_id: str,
        client_order_id: str,
        quantity: Decimal,
        worst_fill: Decimal,
        expires_at: datetime,
        event_id: str,
    ) -> bool:
        if quantity <= ZERO or worst_fill <= ZERO:
            raise ValueError("positive reservation required")
        amount = quantity * worst_fill
        try:
            self._begin()
            self.db.execute(
                "INSERT INTO reservations VALUES(?,?,?,?,?,?)",
                (
                    intention_id,
                    client_order_id,
                    str(quantity),
                    str(worst_fill),
                    OrderStatus.RESERVED.value,
                    utc(expires_at).isoformat(),
                ),
            )
            self.db.execute(
                "INSERT INTO events(event_id,at,kind,amount,reference) VALUES(?,?,?,?,?)",
                (
                    event_id,
                    utc(expires_at).isoformat(),
                    "ENTRY_RESERVED",
                    str(amount),
                    intention_id,
                ),
            )
            self.db.execute(
                "INSERT INTO postings(event_id,event_type,debit_account,credit_account,amount) VALUES(?,?,?,?,?)",
                (
                    event_id,
                    "ENTRY_RESERVED",
                    "reserved_notional_memo",
                    "entry_authorization_memo",
                    str(amount),
                ),
            )
            self.db.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self._rollback()
            return False
        except Exception:
            self._rollback()
            raise

    def transition_reservation(
        self,
        intention_id: str,
        status: str,
        at: datetime,
        event_id: str,
    ) -> bool:
        try:
            target = OrderStatus(status)
        except ValueError as exc:
            raise ValueError("unknown reservation state") from exc
        allowed = {
            OrderStatus.RESERVED: {
                OrderStatus.SUBMITTING,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
            },
            OrderStatus.SUBMITTING: {
                OrderStatus.ACKNOWLEDGED,
                OrderStatus.RESULT_UNKNOWN,
                OrderStatus.REJECTED,
            },
            OrderStatus.ACKNOWLEDGED: {
                OrderStatus.OPEN,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.REJECTED,
            },
            OrderStatus.RESULT_UNKNOWN: {
                OrderStatus.ACKNOWLEDGED,
                OrderStatus.OPEN,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
            },
            OrderStatus.OPEN: {
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.CANCELED,
            },
            OrderStatus.PARTIALLY_FILLED: {
                OrderStatus.FILLED,
                OrderStatus.CANCEL_PENDING,
                OrderStatus.CANCELED,
            },
            OrderStatus.CANCEL_PENDING: {
                OrderStatus.CANCELED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
            },
        }
        try:
            self._begin()
            row = self.db.execute(
                "SELECT status FROM reservations WHERE intention_id=?", (intention_id,)
            ).fetchone()
            if not row:
                self._rollback()
                return False
            current = OrderStatus(row[0])
            if target == current:
                self._rollback()
                return False
            if target not in allowed.get(current, set()):
                raise ValueError(
                    f"invalid reservation transition {current.value}->{target.value}"
                )
            self.db.execute(
                "UPDATE reservations SET status=? WHERE intention_id=?",
                (target.value, intention_id),
            )
            self.db.execute(
                "INSERT INTO events(event_id,at,kind,amount,reference) VALUES(?,?,?,?,NULL)",
                (
                    event_id,
                    utc(at).isoformat(),
                    "RESERVATION_" + target.value.upper(),
                    "0",
                ),
            )
            self.db.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self._rollback()
            return False
        except Exception:
            self._rollback()
            raise

    @staticmethod
    def _accounts(kind: str, amount: Decimal) -> tuple[str, str]:
        if kind == "REALIZED_PNL":
            return (
                ("cash_usdt", "realized_trading_pnl")
                if amount > ZERO
                else ("realized_trading_pnl", "cash_usdt")
            )
        if kind == "FEE":
            return (
                ("cash_usdt", "fee_rebate")
                if amount > ZERO
                else ("trading_fee_expense", "cash_usdt")
            )
        return (
            ("cash_usdt", "funding_pnl")
            if amount > ZERO
            else ("funding_expense", "cash_usdt")
        )

    def post_realized(
        self,
        event_id: str,
        at: datetime,
        kind: str,
        amount: Decimal,
        reference: str,
    ) -> bool:
        """Post a signed exchange bill once per event type and exchange identity."""

        if kind not in {"REALIZED_PNL", "FEE", "FUNDING"}:
            raise ValueError("unsupported realized event")
        if amount == ZERO:
            raise ValueError("zero realized posting is not meaningful")
        debit, credit = self._accounts(kind, amount)
        try:
            self._begin()
            self.db.execute(
                "INSERT INTO events(event_id,at,kind,amount,reference) VALUES(?,?,?,?,?)",
                (event_id, utc(at).isoformat(), kind, str(amount), reference),
            )
            self.db.execute(
                "INSERT INTO postings(event_id,event_type,debit_account,credit_account,amount) VALUES(?,?,?,?,?)",
                (event_id, kind, debit, credit, str(abs(amount))),
            )
            self.db.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self._rollback()
            return False
        except Exception:
            self._rollback()
            raise

    def post_margin_memo(
        self,
        event_id: str,
        at: datetime,
        amount: Decimal,
        committed: bool,
        reference: str,
    ) -> bool:
        """Record margin commitment/return without treating return as income."""

        if amount <= ZERO:
            raise ValueError("margin memo amount must be positive")
        kind = "MARGIN_COMMITTED" if committed else "MARGIN_RETURNED"
        debit, credit = (
            ("isolated_margin_memo", "available_collateral_memo")
            if committed
            else ("available_collateral_memo", "isolated_margin_memo")
        )
        try:
            self._begin()
            self.db.execute(
                "INSERT INTO events(event_id,at,kind,amount,reference) VALUES(?,?,?,?,?)",
                (event_id, utc(at).isoformat(), kind, str(amount), reference),
            )
            self.db.execute(
                "INSERT INTO postings(event_id,event_type,debit_account,credit_account,amount) VALUES(?,?,?,?,?)",
                (event_id, kind, debit, credit, str(amount)),
            )
            self.db.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self._rollback()
            return False
        except Exception:
            self._rollback()
            raise

    def allocate_new_high_water(
        self, campaign_id: str, cumulative_realized_net: Decimal
    ) -> Allocation:
        self._begin()
        try:
            row = self.db.execute(
                "SELECT high_water,reusable,reserve FROM allocations WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            high, reuse, reserve = (
                tuple(map(Decimal, row)) if row else (ZERO, ZERO, ZERO)
            )
            increment = max(ZERO, cumulative_realized_net - high)
            high = max(high, cumulative_realized_net)
            reuse += increment * Decimal("0.25")
            reserve += increment * Decimal("0.75")
            self.db.execute(
                "INSERT INTO allocations VALUES(?,?,?,?) "
                "ON CONFLICT(campaign_id) DO UPDATE SET "
                "high_water=excluded.high_water,reusable=excluded.reusable,reserve=excluded.reserve",
                (campaign_id, str(high), str(reuse), str(reserve)),
            )
            self.db.execute("COMMIT")
            return Allocation(reuse, reserve, high)
        except Exception:
            self._rollback()
            raise
