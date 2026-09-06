"""Independent durable Slack relay for Track A-2.

The relay reads the A-2 SQLite ledger and status report only.  It never loads
Coinone credentials and cannot submit, cancel, or reconcile an exchange order.
"""
import argparse
import json
from pathlib import Path
import signal
import sqlite3
import time

from common.notify import NotificationError, send
from track_a_2.ops.notices import (
    DataUnavailable,
    REPLAY_VERSION,
    Replay,
    Source,
    event_payload,
    fill_payload,
    heartbeat,
    number,
    operating_fields,
    payload,
    protection_payload,
)


class Relay:
    def __init__(self, directory, env_path, *, sender=send, clock=time.time):
        self.directory = Path(directory).resolve()
        self.folder = self.directory / "notifications"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.source = Source(self.directory)
        self.env_path = str(env_path)
        self.sender = sender
        self.clock = clock
        self.guard = sqlite3.connect(
            self.folder / "writer.lock.sqlite", timeout=0
        )
        try:
            self.guard.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            self.guard.close()
            raise RuntimeError("A-2 notification relay already running") from None
        self.db = sqlite3.connect(self.folder / "relay.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                created REAL NOT NULL,
                ready REAL NOT NULL,
                payload TEXT NOT NULL,
                fact TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0,
                sent_at REAL,
                last_error TEXT
            );
            """
        )
        with self.db:
            # Slack incoming webhooks provide no idempotency token.  A crash
            # after acceptance and before this commit is explicitly uncertain;
            # retain the durable retry instead of silently losing the notice.
            self.db.execute(
                "UPDATE queue SET state='pending',"
                "last_error='delivery uncertain after restart' "
                "WHERE state='sending'"
            )

    def close(self):
        self.db.close()
        self.guard.close()

    def get(self, key, default=None):
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def enqueue(self, key, card, now, *, fact=None, ready=None):
        self.db.execute(
            "INSERT OR IGNORE INTO queue(key,created,ready,payload,fact) "
            "VALUES (?,?,?,?,?)",
            (
                key,
                now,
                now if ready is None else ready,
                json.dumps(card, ensure_ascii=False),
                json.dumps(fact, ensure_ascii=False) if fact else None,
            ),
        )

    def _merge_fill(self, fact, state, now):
        key = "fill:" + fact["cid"]
        pending = self.db.execute(
            "SELECT * FROM queue WHERE key LIKE ? AND state='pending' "
            "AND attempts=0 ORDER BY id DESC LIMIT 1",
            (key + ":%",),
        ).fetchone()
        if pending:
            merged = json.loads(pending["fact"])
            for field in ("qty", "gross", "fee", "pnl"):
                merged[field] = str(number(merged[field]) + number(fact[field]))
            merged.update(
                remaining=fact["remaining"],
                campaign_realized=fact["campaign_realized"],
                t_ms=fact["t_ms"],
            )
            self.db.execute(
                "UPDATE queue SET payload=?,fact=?,ready=? WHERE id=?",
                (
                    json.dumps(fill_payload(merged, state), ensure_ascii=False),
                    json.dumps(merged, ensure_ascii=False),
                    min(now + 2, pending["created"] + 5),
                    pending["id"],
                ),
            )
            return
        self.enqueue(
            key + ":" + str(fact["seq"]),
            fill_payload(fact, state),
            now,
            fact=fact,
            ready=now + 2,
        )

    def ingest(self, rows, context, now, state, status):
        replay = Replay(context)
        for row in rows:
            fact = replay.apply(row)
            if fact:
                card = None
                key = None
                if fact["type"] in ("fill", "correction"):
                    self._merge_fill(fact, state, now)
                elif fact["type"] == "protection":
                    key = "protection:" + str(fact["seq"])
                    card = protection_payload(fact)
                else:
                    key = "event:" + str(fact["seq"])
                    card = event_payload(fact)
                    if card and card["kind"] == "이상":
                        reason = ""
                        if fact["type"] == "HALT":
                            reason = str((fact.get("body") or {}).get("reason") or "")
                        throttle = "alert:" + fact["type"] + ":" + reason
                        last = self.get(throttle)
                        if last is not None and now - last < 60:
                            card = None
                        else:
                            self.put(throttle, now)
                            card["fields"] += operating_fields(state, status)
                if card:
                    self.enqueue(key, card, now, fact=fact)
            self.put("cursor", row[0])
        self.put("context", replay.context)

    @staticmethod
    def _inventory_unprotected(state):
        active = [
            order
            for order in state.get("orders", {}).values()
            if order.get("status") not in {
                "FILLED", "CANCELED", "NOT_TRIGGERED_CANCELED",
                "CANCELED_NO_ORDER", "CANCELED_LIMIT_PRICE_EXCEED",
                "CANCELED_UNDER_PRODUCT_UNIT", "REJECTED",
            }
        ]
        for coin, book in state.get("books", {}).items():
            amount = sum((number(lot[0]) for lot in book.get("lots", [])), number(0))
            if amount <= 0:
                continue
            protection = [
                order
                for order in active
                if order.get("coin") == coin
                and order.get("role") == "protect"
                and order.get("status") == "NOT_TRIGGERED"
                and not order.get("cancel_requested")
            ]
            replacement = [
                order
                for order in active
                if order.get("coin") == coin
                and order.get("role") in ("trim", "exit")
                and order.get("status") in ("SUBMITTED", "LIVE", "PARTIALLY_FILLED")
            ]
            if len(protection) != 1 and not replacement:
                return True
        return False

    def health(self, issue, state, status, now):
        prior = self.get("health_issue")
        candidate = self.get("health_candidate")
        if issue and issue != candidate:
            self.put("health_candidate", issue)
            self.put("health_since", now)
        elif not issue:
            self.put("health_candidate", None)
        grace = 30 if issue == "공개·개인 시세 연결 끊김" else 15 if issue == "보유 재고 보호 상태 확인 필요" else 0
        if issue and now - self.get("health_since", now) < grace:
            return
        if issue != prior:
            self.put("health_issue", issue)
            if issue:
                card = payload(
                    "이상",
                    "감시 상태 확인",
                    operating_fields(state, status) if state and status else None,
                    [issue + " · 엔진 장부와 거래소 상태 확인이 필요합니다."],
                    t_ms=now * 1000,
                )
            else:
                card = payload(
                    "복구",
                    "감시 데이터 정상화",
                    lines=["최신 A-2 장부·연결·보호 상태를 다시 확인했습니다."],
                    t_ms=now * 1000,
                )
            sequence = self.get("health_sequence", 0) + 1
            self.put("health_sequence", sequence)
            self.enqueue("health:" + str(sequence), card, now)

    def _health_issue(self, state, status, now):
        if state.get("halt"):
            return "A-2 엔진 HALT"
        if now - float(number(status["t_ms"]) / number(1000)) > 120:
            return "A-2 상태 보고가 2분 이상 지연됨"
        if not status.get("connected") or not status.get("private_connected"):
            return "공개·개인 시세 연결 끊김"
        if not (status.get("storage") or {}).get("ok", True):
            return "저장 공간 상태 확인 필요"
        if self._inventory_unprotected(state):
            return "보유 재고 보호 상태 확인 필요"
        return None

    def poll(self):
        now = self.clock()
        cursor = self.get("cursor")
        try:
            state, status, rows, maximum = self.source.read(cursor)
            if cursor is not None and maximum < cursor:
                raise DataUnavailable("A-2 ledger sequence moved backwards")
            with self.db:
                if cursor is None:
                    replay = Replay()
                    for row in rows:
                        replay.apply(row)
                    self.put("cursor", maximum)
                    self.put("context", replay.context)
                    self.put("context_version", REPLAY_VERSION)
                    self.put("last_hb", now)
                    self.enqueue(
                        "initial-connection",
                        heartbeat(state, status, now, boot=True),
                        now,
                    )
                else:
                    if self.get("context_version") != REPLAY_VERSION:
                        _, _, history, _ = self.source.read()
                        replay = Replay()
                        for row in history:
                            if row[0] <= cursor:
                                replay.apply(row)
                        self.put("context", replay.context)
                        self.put("context_version", REPLAY_VERSION)
                    self.ingest(rows, self.get("context"), now, state, status)
                self.health(self._health_issue(state, status, now), state, status, now)
                if now - self.get("last_hb", now) >= 3600:
                    self.enqueue(
                        "heartbeat:" + str(int(now)),
                        heartbeat(state, status, now),
                        now,
                    )
                    self.put("last_hb", now)
                self.put("last_poll", now)
                self.put("source_error", None)
        except (DataUnavailable, ValueError, TypeError, KeyError, ArithmeticError):
            with self.db:
                self.put("source_error", "A-2 ledger/status unavailable")
                self.health(
                    "A-2 장부·상태 파일을 읽을 수 없음", None, None, now
                )
        self.deliver(now)
        self.write_status()

    def deliver(self, now):
        row = self.db.execute(
            "SELECT * FROM queue WHERE state IN ('pending','sending') "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if not row or max(row["ready"], row["next_at"]) > now:
            return
        with self.db:
            self.db.execute(
                "UPDATE queue SET state='sending',attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
        card = json.loads(row["payload"])
        try:
            result = self.sender(
                **card,
                data_dir=str(self.directory),
                env_path=self.env_path,
            )
            if result != (200, "ok"):
                raise NotificationError(
                    "notify: delivery acknowledgement unavailable", uncertain=True
                )
        except NotificationError as exc:
            error = "Slack HTTP " + str(exc.status) if exc.status else "Slack delivery unavailable"
            if exc.uncertain:
                error += " (delivery uncertain)"
            with self.db:
                delay = min(300, 5 * 2 ** min(row["attempts"], 6))
                self.db.execute(
                    "UPDATE queue SET state='pending',next_at=?,last_error=? "
                    "WHERE id=?",
                    (now + delay, error, row["id"]),
                )
                self.put(
                    "last_failure", dict(t_ms=int(now * 1000), error=error)
                )
            print(
                json.dumps(
                    dict(kind="NOTIFY_ERROR", error=error, retry_seconds=delay)
                ),
                flush=True,
            )
        else:
            with self.db:
                self.db.execute(
                    "UPDATE queue SET state='sent',sent_at=?,last_error=NULL "
                    "WHERE id=?",
                    (now, row["id"]),
                )
                self.put(
                    "last_sent",
                    dict(
                        t_ms=int(now * 1000),
                        http_status=200,
                        ack="ok",
                        kind=card["kind"],
                    ),
                )
                self.put("last_failure", None)
            print(
                json.dumps(
                    dict(
                        kind="NOTIFY_SENT",
                        http_status=200,
                        ack="ok",
                        notice=card["kind"],
                    )
                ),
                flush=True,
            )

    def write_status(self):
        report = dict(
            t_ms=int(self.clock() * 1000),
            cursor=self.get("cursor"),
            last_poll=self.get("last_poll"),
            last_sent=self.get("last_sent"),
            last_failure=self.get("last_failure"),
            source_error=self.get("source_error"),
            health_issue=self.get("health_issue"),
            trade_notifications="fills_and_protection",
            pending=self.db.execute(
                "SELECT COUNT(*) FROM queue WHERE state IN ('pending','sending')"
            ).fetchone()[0],
        )
        temporary = self.folder / "status.json.tmp"
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.folder / "status.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument(
        "--once", action="store_true", help="Poll and deliver once, then exit."
    )
    args = parser.parse_args()
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    relay = Relay(args.data_dir, args.env)
    try:
        while not stopping:
            relay.poll()
            if args.once:
                break
            time.sleep(1)
    finally:
        relay.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print exception text that might contain a URL or local secret.
        print(
            json.dumps(dict(kind="NOTIFY_FATAL", error_type=type(exc).__name__)),
            flush=True,
        )
        raise SystemExit(1) from None
