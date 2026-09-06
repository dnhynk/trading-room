import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from common import notify
from track_a_2.ops.notices import (
    DataUnavailable,
    KST,
    Replay,
    Source,
    fill_payload,
    summary_fields,
)
from track_a_2.ops.notify import Relay


def at(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=KST).timestamp()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.now = at("2026-09-07T12:00:00")
        self.db = sqlite3.connect(self.path / "a2-ledger.sqlite")
        self.addCleanup(self.db.close)
        self.db.executescript(
            "CREATE TABLE state (id INTEGER PRIMARY KEY,body TEXT);"
            "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "t_ms INTEGER,kind TEXT,body TEXT);"
        )
        self.state = dict(
            version=1,
            capital_initialized=True,
            cash_krw="300000",
            realized="0",
            books={},
            orders={},
            halt=None,
        )
        self.status = dict(
            track="A-2",
            execution_version="test-v1",
            t_ms=int(self.now * 1000),
            mode="live",
            connected=True,
            private_connected=True,
            entry_paused=False,
            selected=["ETH", "XRP"],
            equity_krw="300000",
            free_cash_krw="300000",
            day_equity_pnl_krw="0",
            storage=dict(ok=True),
        )
        self.save()

    def save(self):
        self.db.execute(
            "INSERT OR REPLACE INTO state VALUES (1,?)", (json.dumps(self.state),)
        )
        self.db.commit()
        (self.path / "status.json").write_text(
            json.dumps(self.status), encoding="utf-8"
        )

    def event(self, kind, body, *, offset=0):
        self.db.execute(
            "INSERT INTO events(t_ms,kind,body) VALUES (?,?,?)",
            (int((self.now + offset) * 1000), kind, json.dumps(body)),
        )
        self.db.commit()

    def intent(
        self,
        *,
        cid="private-buy-id",
        campaign="private-campaign-id",
        coin="ETH",
        side="BUY",
        role="buy",
        qty="1",
        price="100000",
        trigger=None,
    ):
        order = dict(
            cid=cid,
            campaign_id=campaign,
            coin=coin,
            side=side,
            role=role,
            type="STOP_LIMIT" if role == "protect" else "LIMIT",
            qty=qty,
            price=price,
            trigger_price=trigger,
        )
        self.event("ORDER_INTENT", dict(order=order))
        return order

    def fill(
        self,
        *,
        cid="private-buy-id",
        campaign="private-campaign-id",
        coin="ETH",
        side="BUY",
        role="buy",
        qty="1",
        gross="100000",
        fee="0",
        pnl="0",
    ):
        self.event(
            "FILL",
            dict(
                cid=cid,
                campaign_id=campaign,
                coin=coin,
                side=side,
                role=role,
                qty=qty,
                gross_delta=gross,
                fee=fee,
                pnl=pnl,
                price=str(float(gross) / float(qty)) if float(qty) else None,
            ),
        )


class PresentationTests(Fixture):
    def test_a2_summary_never_falls_back_to_ab_and_uses_krw(self):
        with patch.object(
            notify, "_engine_day", side_effect=AssertionError("A/B read")
        ), patch("track_a_2.ops.notices.time.time", return_value=self.now):
            fields = dict(
                notify.summary_fields(track="A2", data_dir=self.path)
            )
        self.assertEqual(fields["A-2 자본"], "300,000.0원")
        self.assertNotIn("USDT", json.dumps(fields, ensure_ascii=False))
        self.assertNotIn("$", json.dumps(fields, ensure_ascii=False))

    def test_signed_losses_and_late_campaign_correction_are_statistics(self):
        self.state["realized"] = "-10.5"
        self.status["day_equity_pnl_krw"] = "-10.5"
        self.save()
        self.event(
            "FLAT",
            dict(
                campaign=dict(
                    campaign_id="old-private", realized="1", budget="300"
                )
            ),
        )
        self.event(
            "CAMPAIGN_LATE_PNL",
            dict(campaign_id="old-private", coin="ETH", pnl="-2"),
        )
        fields = dict(summary_fields(self.path))
        self.assertEqual(fields["누적 손익"], "-10.5원")
        self.assertEqual(fields["승률"], "0.0% · 0승 1패 0보합")

    def test_fill_card_uses_a2_identity_and_hides_private_ids(self):
        self.intent()
        self.fill()
        replay = Replay()
        facts = [replay.apply(row) for row in Source(self.path).read()[2]]
        card = fill_payload(next(fact for fact in facts if fact), self.state)
        blocks = notify._blocks(
            **{key: value for key, value in card.items() if key != "track"},
            track="A2",
            data_dir=self.path,
        )
        rendered = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("A-2 · ETH 롱", rendered)
        self.assertNotIn("private-buy-id", rendered)
        self.assertNotIn("private-campaign-id", rendered)
        self.assertNotIn("C ·", rendered)

    def test_source_is_read_only_and_does_not_create_missing_ledger(self):
        absent = self.path / "missing"
        absent.mkdir()
        with self.assertRaises(DataUnavailable):
            Source(absent).read()
        self.assertFalse((absent / "a2-ledger.sqlite").exists())
        database = Source(self.path).connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                database.execute("DELETE FROM events")
        finally:
            database.close()


class RelayTests(Fixture):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.clock = lambda: self.now
        self.relay = Relay(
            self.path,
            self.path / "nonexistent.env",
            sender=self.send,
            clock=self.clock,
        )
        self.addCleanup(self._close)

    def _close(self):
        if self.relay is not None:
            self.relay.close()
            self.relay = None

    def send(self, **card):
        self.assertEqual(card["track"], "A2")
        self.assertEqual(card["data_dir"], str(self.path.resolve()))
        self.sent.append(card)
        return 200, "ok"

    def restart(self):
        self.relay.close()
        self.relay = Relay(
            self.path,
            self.path / "nonexistent.env",
            sender=self.send,
            clock=self.clock,
        )

    def test_first_start_tails_history_and_restart_does_not_repeat_boot(self):
        self.intent()
        self.fill()
        self.relay.poll()
        self.assertEqual([card["kind"] for card in self.sent], ["부팅"])
        self.restart()
        self.relay.poll()
        self.assertEqual(len(self.sent), 1)

    def test_partial_buy_combines_then_protection_is_reported(self):
        self.relay.poll()
        self.intent(qty="1")
        self.fill(qty="0.4", gross="40000")
        self.fill(qty="0.6", gross="60000")
        self.relay.poll()
        self.assertEqual(len(self.sent), 1)
        self.now += 3
        self.status["t_ms"] = int(self.now * 1000)
        self.save()
        self.relay.poll()
        self.assertEqual([card["kind"] for card in self.sent], ["부팅", "진입"])
        self.assertEqual(dict(self.sent[-1]["fields"])["체결 수량"], "1개")

        protect = self.intent(
            cid="private-protection-id",
            side="SELL",
            role="protect",
            qty="1",
            price="89900",
            trigger="90000",
        )
        protect.update(status="NOT_TRIGGERED", cancel_requested=False)
        self.state["orders"] = {protect["cid"]: protect}
        self.state["books"] = {
            "ETH": dict(
                lots=[["1", "100000", "private-buy-id"]],
                avg="100000",
                inventory_phase="protected",
                campaign_open=True,
            )
        }
        self.save()
        self.event(
            "ORDER_STATUS",
            dict(coin="ETH", cid=protect["cid"], status="NOT_TRIGGERED"),
        )
        self.relay.poll()
        self.assertEqual(self.sent[-1]["head"], "A-2 · ETH · 보호 주문 확인")
        rendered = json.dumps(self.sent[-1], ensure_ascii=False)
        self.assertNotIn("private-protection-id", rendered)

    def test_sell_fill_reports_campaign_pnl_and_no_private_identity(self):
        self.relay.poll()
        self.intent()
        self.fill()
        self.relay.poll()
        self.now += 3
        self.status["t_ms"] = int(self.now * 1000)
        self.save()
        self.relay.poll()
        self.intent(
            cid="private-sell-id", side="SELL", role="trim", price="101000"
        )
        self.fill(
            cid="private-sell-id",
            side="SELL",
            role="trim",
            gross="101000",
            pnl="1000",
        )
        self.relay.poll()
        self.now += 3
        self.status["t_ms"] = int(self.now * 1000)
        self.save()
        self.relay.poll()
        self.assertEqual(self.sent[-1]["kind"], "전량청산")
        self.assertEqual(
            dict(self.sent[-1]["fields"])["캠페인 실현손익"], "+1,000.0원"
        )
        self.assertNotIn("private-", json.dumps(self.sent[-1]))

    def test_unprotected_inventory_alert_has_grace_and_recovery(self):
        self.relay.poll()
        self.state["books"] = {
            "ETH": dict(
                lots=[["1", "100000", "private-buy-id"]],
                avg="100000",
                inventory_phase="reprotecting",
                campaign_open=True,
            )
        }
        self.save()
        self.relay.poll()
        self.assertEqual(len(self.sent), 1)
        self.now += 16
        self.status["t_ms"] = int(self.now * 1000)
        self.save()
        self.relay.poll()
        self.assertEqual(self.sent[-1]["kind"], "이상")
        self.state["books"] = {}
        self.save()
        self.relay.poll()
        self.assertEqual(self.sent[-1]["kind"], "복구")

    def test_failed_delivery_is_durable_and_sanitized(self):
        def fail(**_):
            raise notify.NotificationError("must not echo secret", status=429)

        self.relay.sender = fail
        self.relay.poll()
        report = json.loads(
            (self.path / "notifications" / "status.json").read_text()
        )
        self.assertEqual(report["pending"], 1)
        self.assertNotIn("secret", json.dumps(report))
        self.restart()
        self.relay.poll()
        self.assertEqual(self.sent, [])
        self.now += 5
        self.relay.poll()
        self.assertEqual(self.sent[-1]["kind"], "부팅")

    def test_second_worker_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "already running"):
            Relay(self.path, "unused")


if __name__ == "__main__":
    unittest.main()
