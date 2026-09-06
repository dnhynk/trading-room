from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from track_special.arx_campaign.contracts import (
    OperatingMode,
    OrderIntent,
    OrderPurpose,
    RiskApproval,
    RiskState,
)
from track_special.arx_campaign.execution.engine import (
    CampaignEngine,
    LiveTransport,
    ProcessLock,
    uta_v3_order_payload,
    uta_v3_protective_stop_payload,
)


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def intent(identifier: str = "i1", purpose: OrderPurpose = OrderPurpose.PROBE_ENTRY):
    entry = purpose in {OrderPurpose.PROBE_ENTRY, OrderPurpose.PYRAMID_ENTRY}
    return OrderIntent(
        identifier,
        "campaign",
        purpose,
        "buy" if entry else "sell",
        Decimal("2"),
        Decimal("10"),
        "GTC",
        not entry,
        1 if purpose is OrderPurpose.PROBE_ENTRY else 2 if entry else None,
        "config-hash",
        NOW,
        NOW + timedelta(seconds=1),
        NOW + timedelta(minutes=1),
        ("TEST",),
    )


def approval(value: OrderIntent) -> RiskApproval:
    return RiskApproval(
        "approval-1",
        value.intention_id,
        value.campaign_id,
        value.config_hash,
        value.quantity_base,
        Decimal("4"),
        Decimal("4"),
        Decimal("4"),
        Decimal("20"),
        Decimal("10"),
        value.market_observed_at,
        NOW,
        NOW + timedelta(seconds=2),
        NOW + timedelta(seconds=45),
        RiskState.NORMAL,
    )


class TimeoutTransport:
    def submit(self, *_):
        raise TimeoutError()


class ExecutionArxCampaignTests(unittest.TestCase):
    def _reserve(self, engine: CampaignEngine, value: OrderIntent) -> str:
        engine.reconcile_restart(
            reconciled_long_exposure=Decimal("0"),
            open_client_oids=set(),
            protection_covered_quantity=Decimal("0"),
        )
        return engine.reserve(
            value,
            approval=approval(value),
            current_config_hash=value.config_hash,
            worst_fill_price=Decimal("10"),
            at=NOW + timedelta(seconds=3),
        )

    def _submit(self, engine: CampaignEngine, value: OrderIntent) -> str:
        return engine.submit(
            value.intention_id,
            current_config_hash=value.config_hash,
            market_observed_at=value.market_observed_at,
            account_observed_at=NOW,
            at=NOW + timedelta(seconds=4),
        )

    def test_timeout_requires_reconciliation_before_retry(self):
        with TemporaryDirectory() as directory:
            value = intent()
            with CampaignEngine(Path(directory) / "state.db", TimeoutTransport()) as engine:
                client_oid = self._reserve(engine, value)
                self._submit(engine, value)
                self.assertEqual(engine.status()["orders"][0]["status"], "result_unknown")
                with self.assertRaises(RuntimeError):
                    self._submit(engine, value)
                engine.reconcile(client_oid, "open")
                engine.reconcile(client_oid, "open")
                self.assertEqual(engine.status()["result_unknown_count"], 0)

    def test_cancel_race_fill_pauses_when_aggregate_position_is_unprotected(self):
        with TemporaryDirectory() as directory:
            value = intent()
            with CampaignEngine(Path(directory) / "state.db") as engine:
                client_oid = self._reserve(engine, value)
                self._submit(engine, value)
                engine.cancel_entry_orders()
                engine.reconcile(
                    client_oid,
                    "canceled",
                    Decimal("2"),
                    aggregate_position_qty=Decimal("2"),
                )
                status = engine.status()
                self.assertEqual(status["orders"][0]["status"], "filled")
                self.assertEqual(status["risk_state"], "PAUSE_ENTRIES")
                self.assertEqual(status["unprotected_entry_count"], 1)

    def test_only_queried_server_protection_can_cover_aggregate_position(self):
        with TemporaryDirectory() as directory:
            value = intent()
            with CampaignEngine(Path(directory) / "state.db") as engine:
                client_oid = self._reserve(engine, value)
                self._submit(engine, value)
                engine.reconcile(
                    client_oid,
                    "filled",
                    Decimal("2"),
                    protection_qty=Decimal("3"),
                    protection_order_id="server-stop-1",
                    protection_active=True,
                    trigger_reference="mark_price",
                    protection_observed_at=NOW + timedelta(seconds=5),
                    protection_valid_until=NOW + timedelta(minutes=1),
                    protection_source="server_query",
                    protection_stop_price=Decimal("8"),
                    aggregate_position_qty=Decimal("3"),
                    at=NOW + timedelta(seconds=6),
                )
                self.assertEqual(engine.status()["orders"][0]["protection"], "active")

    def test_entry_needs_same_transaction_risk_approval_and_restart_snapshot(self):
        with TemporaryDirectory() as directory:
            value = intent()
            with CampaignEngine(Path(directory) / "state.db") as engine:
                with self.assertRaises(RuntimeError):
                    engine.reserve(value)
                engine.reconcile_restart(
                    reconciled_long_exposure=Decimal("0"), open_client_oids=set()
                )
                with self.assertRaises(RuntimeError):
                    engine.reserve(
                        value,
                        current_config_hash=value.config_hash,
                        worst_fill_price=Decimal("10"),
                        at=NOW + timedelta(seconds=3),
                    )

    def test_live_is_non_operational_and_process_lock_rejects_duplicate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with self.assertRaises(RuntimeError):
                LiveTransport().submit("x")
            with self.assertRaises(RuntimeError):
                CampaignEngine(path, operating_mode=OperatingMode.LIVE)
            with ProcessLock(path):
                with self.assertRaises(RuntimeError):
                    ProcessLock(path).__enter__()

    def test_uta_mapper_is_separate_and_client_id_fits_exchange_limit(self):
        value = intent("a-very-long-intention-identifier")
        client_oid = CampaignEngine.client_oid(value)
        self.assertEqual(32, len(client_oid))
        payload = uta_v3_order_payload(
            client_oid=client_oid,
            side="sell",
            quantity=Decimal("2"),
            reduce_only=True,
        )
        self.assertEqual("yes", payload["reduceOnly"])
        self.assertEqual("2", payload["qty"])
        self.assertEqual("isolated", payload["marginMode"])
        self.assertNotIn("size", payload)
        with self.assertRaises(ValueError):
            uta_v3_order_payload(
                client_oid=client_oid,
                side="sell",
                quantity=Decimal("2"),
                reduce_only=False,
            )
        with self.assertRaises(ValueError):
            uta_v3_order_payload(
                client_oid=client_oid,
                side="buy",
                quantity=Decimal("2"),
                reduce_only=True,
            )
        limit_payload = uta_v3_order_payload(
            client_oid=client_oid,
            side="buy",
            quantity=Decimal("2"),
            reduce_only=False,
            order_type="limit",
            price=Decimal("0.80"),
        )
        self.assertEqual("gtc", limit_payload["timeInForce"])
        with self.assertRaises(ValueError):
            uta_v3_order_payload(
                client_oid=client_oid,
                side="buy",
                quantity=Decimal("2"),
                reduce_only=False,
                order_type="limit",
                price=Decimal("0.80"),
                time_in_force="forever",
            )

    def test_uta_protective_stop_uses_one_way_partial_tpsl_contract(self):
        payload = uta_v3_protective_stop_payload(
            client_oid="arx-stop-123",
            quantity=Decimal("25"),
            stop_price=Decimal("0.73125"),
        )
        self.assertEqual("tpsl", payload["type"])
        self.assertEqual("sell", payload["side"])
        self.assertEqual("yes", payload["reduceOnly"])
        self.assertEqual("partial", payload["tpslMode"])
        self.assertEqual("mark", payload["slTriggerBy"])
        self.assertEqual("market", payload["slOrderType"])
        self.assertEqual("25", payload["qty"])
        self.assertNotIn("posSide", payload)
        self.assertNotIn("marginMode", payload)
        with self.assertRaises(ValueError):
            uta_v3_protective_stop_payload(
                client_oid="arx-stop-123",
                quantity=Decimal("0"),
                stop_price=Decimal("0.73125"),
            )


if __name__ == "__main__":
    unittest.main()
