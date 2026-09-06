from datetime import datetime, timezone
from decimal import Decimal
import unittest

from track_special.arx_campaign.contracts import ApiFamily
from track_special.arx_campaign.execution.bitget_uta_v3 import (
    DOCUMENTED_WRITE_ENDPOINTS_NOT_IMPLEMENTED,
    READ_CAPABILITIES,
    ReadOnlyPrivateProbe,
    account_snapshot_from_uta_settings,
)


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


class UtaAccountBoundaryTests(unittest.TestCase):
    def test_exact_uta_basic_isolated_one_way_can_pass_when_other_inputs_verified(self):
        payload = {
            "code": "00000",
            "data": {
                "accountMode": "unified",
                "accountLevel": "basic",
                "assetMode": "single_asset",
                "holdMode": "one_way_mode",
                "symbolConfigList": [
                    {
                        "category": "USDT-FUTURES",
                        "symbol": "ARXUSDT",
                        "marginMode": "isolated",
                        "leverage": "3",
                    }
                ],
            },
        }
        snapshot = account_snapshot_from_uta_settings(
            payload,
            observed_at=NOW,
            strategy_equity_usdt=Decimal("100"),
            available_usdt=Decimal("90"),
            margin_coin="USDT",
            reconciled=True,
            dedicated_or_verifiably_separated=True,
            external_exposure_detected=False,
            auto_margin_top_up_disabled=True,
        )
        self.assertEqual(ApiFamily.UTA_V3, snapshot.api_family)
        self.assertTrue(snapshot.entry_preconditions_verified)

    def test_advanced_or_classic_shaped_response_fails_closed(self):
        for account_mode, level in (("unified", "advanced"), ("classic", "basic")):
            snapshot = account_snapshot_from_uta_settings(
                {
                    "code": "00000",
                    "data": {
                        "accountMode": account_mode,
                        "accountLevel": level,
                        "holdMode": "one_way_mode",
                        "symbolConfigList": [],
                    },
                },
                observed_at=NOW,
                strategy_equity_usdt=Decimal("100"),
                available_usdt=Decimal("100"),
                margin_coin="USDT",
                reconciled=True,
                dedicated_or_verifiably_separated=True,
                external_exposure_detected=False,
                auto_margin_top_up_disabled=True,
            )
            self.assertFalse(snapshot.entry_preconditions_verified)

    def test_private_probe_has_only_documented_gets_and_no_write_method(self):
        calls = []
        probe = ReadOnlyPrivateProbe(lambda path, params: calls.append((path, params)) or {})
        probe.account_settings()
        probe.positions()
        self.assertTrue(all(capability.method == "GET" for capability in READ_CAPABILITIES.values()))
        self.assertFalse(hasattr(probe, "submit"))
        self.assertIn("place_order", DOCUMENTED_WRITE_ENDPOINTS_NOT_IMPLEMENTED)
        self.assertEqual("ARXUSDT", calls[1][1]["symbol"])


if __name__ == "__main__":
    unittest.main()
