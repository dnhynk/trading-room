"""ARX accumulation cards using the existing Slack renderer and sender."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any, Callable

from common.notify import NotificationError, send
from ..strategy.accumulation import ZERO, number


def fill_card(fill: dict[str, Any], timestamp: int) -> dict[str, Any]:
    if fill.get("fill_model") != "snapshot_ask_walk_not_actual_execution":
        raise ValueError("PAPER_FILL_EVIDENCE_REQUIRED")
    e0 = number(fill["e0_usdt"])
    quantity, notional = number(fill["quantity"]), number(fill["notional_usdt"])
    total_quantity, total_notional = number(fill["total_quantity"]), number(fill["total_notional"])
    if min(e0, quantity, notional, total_quantity, total_notional) <= ZERO:
        raise ValueError("INVALID_FILL_NOTICE")
    margin = total_notional / 10
    fee = number(fill["total_fees"])
    remaining = max(ZERO, e0 - margin - fee - max(ZERO, number(fill["total_funding"]))
                    - total_notional * number(fill["close_fee_rate"]))
    kst = datetime.fromtimestamp(timestamp / 1000, timezone(timedelta(hours=9), "KST"))
    return {
        "kind": "진입" if int(fill["fill_number"]) == 1 else "추가",
        "head": f"ARXUSDT 롱 · 모의 체결 · {kst:%m-%d %H:%M} KST",
        "fields": [
            ["이번 매집", f"{quantity:,.0f} ARX @ {notional / quantity:.5f} USDT"],
            ["이번 증거금", f"{notional / 10:,.2f} USDT · 시드의 {notional / 10 / e0 * 100:.2f}%"],
            ["누적 시드 투입", f"{margin / e0 * 100:.2f}% · {margin:,.2f} / {e0:,.2f} USDT"],
            ["평균 매수가", f"{total_notional / total_quantity:.5f} USDT"],
            ["모의 보유 수량", f"{total_quantity:,.0f} ARX"],
            ["누적 명목 노출", f"{total_notional:,.2f} USDT · 최초 시드의 {total_notional / e0:.2f}배"],
            ["남은 매집 예산", f"{remaining:,.2f} USDT · 청산 수수료 예약 반영"],
            ["누적 진입 수수료", f"{fee:,.4f} USDT"],
        ],
        "lines": ["*모의 체결입니다. 실제 주문이나 계좌 체결이 아닙니다.*"],
        "ctx": "Special ARX · 격리 10배 가정 · 시드 투입률은 최초 시드 대비 증거금 비율 · 수수료 별도",
        "balance": False, "track": "SPECIAL-ARX",
    }


class AccumulationOutbox:
    """One outbox in the existing runner; no separate Slack relay process.

Unique event keys suppress known duplicates. Ambiguous deliveries are retained
for review instead of blindly repeating an incoming-webhook request.
"""

    def __init__(self, db: sqlite3.Connection, env_path: str,
                 sender: Callable[..., Any] = send) -> None:
        self.db, self.env_path, self.sender = db, env_path, sender
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS notification_outbox(
                event_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, last_error TEXT)""")
            db.execute("UPDATE notification_outbox SET state='unknown',last_error='DELIVERY_UNCERTAIN_AFTER_RESTART' WHERE state='sending'")

    def enqueue(self, key: str, card: dict[str, Any]) -> None:
        self.db.execute("INSERT OR IGNORE INTO notification_outbox(event_key,payload) VALUES(?,?)",
                        (key, json.dumps(card, ensure_ascii=False)))

    def flush(self, now: float) -> dict[str, int]:
        row = self.db.execute("SELECT event_key,payload,attempts FROM notification_outbox WHERE state='pending' AND next_at<=? ORDER BY rowid LIMIT 1", (now,)).fetchone()
        if row:
            key, payload, attempts = row
            with self.db:
                self.db.execute("UPDATE notification_outbox SET state='sending',attempts=attempts+1 WHERE event_key=?", (key,))
            try:
                receipt = self.sender(**json.loads(payload), env_path=self.env_path)
                state = "sent" if receipt == (200, "ok") else "unknown"
                error = None if state == "sent" else "DELIVERY_ACK_UNVERIFIED"
                next_at = 0
            except NotificationError as exc:
                uncertain = exc.uncertain or (exc.status is not None and exc.status >= 500)
                state = "unknown" if uncertain else "pending" if exc.status in (None, 429) else "failed"
                error = "DELIVERY_UNCERTAIN" if uncertain else "DELIVERY_REJECTED"
                next_at = now + min(300, 30 * 2 ** min(attempts, 4))
            except Exception:
                state, error, next_at = "unknown", "DELIVERY_RESULT_UNKNOWN", 0
            with self.db:
                self.db.execute("UPDATE notification_outbox SET state=?,next_at=?,last_error=? WHERE event_key=?",
                                (state, next_at, error, key))
        return {str(s): int(count) for s, count in self.db.execute(
            "SELECT state,COUNT(*) FROM notification_outbox GROUP BY state")}
