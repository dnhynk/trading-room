from datetime import datetime, timezone
from decimal import Decimal
import unittest

from track_special.arx_campaign.contracts import (
    AccountMode,
    AccountSnapshot,
    ApiFamily,
    InstrumentSpec,
    MarginMode,
    PositionMode,
    decimal,
)


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


class ContractTests(unittest.TestCase):
    def test_financial_decimal_rejects_binary_float(self) -> None:
        with self.assertRaises(TypeError):
            decimal(0.1)

    def test_arx_linear_perpetual_identity_is_exact(self) -> None:
        instrument = InstrumentSpec(
            venue="bitget",
            api_family=ApiFamily.UTA_V3,
            symbol="ARXUSDT",
            category="USDT-FUTURES",
            base_coin="ARX",
            quote_coin="USDT",
            settlement_coin="USDT",
            contract_type="perpetual",
            is_linear=True,
            contract_multiplier=Decimal("1"),
            status="online",
            price_tick=Decimal("0.00001"),
            quantity_step=Decimal("1"),
            min_order_quantity=Decimal("1"),
            min_order_notional=Decimal("5"),
            max_limit_quantity=Decimal("1600000"),
            max_market_quantity=Decimal("330000"),
            min_leverage=Decimal("1"),
            max_leverage=Decimal("20"),
            funding_interval_hours=4,
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0006"),
            observed_at=NOW,
            raw_hash="fixture",
        )
        self.assertTrue(instrument.live_identity_verified)

    def test_entry_account_preconditions_fail_closed(self) -> None:
        account = AccountSnapshot(
            observed_at=NOW,
            api_family=ApiFamily.UNVERIFIED,
            account_mode=AccountMode.UNVERIFIED,
            margin_mode=MarginMode.UNVERIFIED,
            position_mode=PositionMode.UNVERIFIED,
            margin_coin="USDT",
            strategy_equity_usdt=None,
            available_usdt=None,
            reconciled=False,
            dedicated_or_verifiably_separated=False,
            external_exposure_detected=False,
            auto_margin_top_up_disabled=None,
            asset_mode="unverified",
        )
        self.assertFalse(account.entry_preconditions_verified)

    def test_classic_or_advanced_account_cannot_pass_uta_entry_gate(self) -> None:
        for api_family, account_mode in (
            (ApiFamily.CLASSIC_V2, AccountMode.CLASSIC),
            (ApiFamily.UTA_V3, AccountMode.UTA_ADVANCED),
        ):
            account = AccountSnapshot(
                observed_at=NOW,
                api_family=api_family,
                account_mode=account_mode,
                margin_mode=MarginMode.ISOLATED,
                position_mode=PositionMode.ONE_WAY,
                margin_coin="USDT",
                strategy_equity_usdt=Decimal("100"),
                available_usdt=Decimal("100"),
                reconciled=True,
                dedicated_or_verifiably_separated=True,
                external_exposure_detected=False,
                auto_margin_top_up_disabled=True,
                asset_mode="single_asset",
            )
            self.assertFalse(account.entry_preconditions_verified)


if __name__ == "__main__":
    unittest.main()
