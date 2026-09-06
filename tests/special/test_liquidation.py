from decimal import Decimal
import unittest

from track_special.arx_campaign.risk import (
    check_liquidation_buffer,
    independent_long_bankruptcy_price,
    independent_long_liquidation_price,
)


class LiquidationTests(unittest.TestCase):
    def test_bankruptcy_is_distinct_from_maintenance_liquidation(self):
        bankruptcy = independent_long_bankruptcy_price(
            quantity_base=Decimal("10"),
            average_entry_price=Decimal("10"),
            isolated_margin_usdt=Decimal("20"),
        )
        liquidation = independent_long_liquidation_price(
            quantity_base=Decimal("10"),
            average_entry_price=Decimal("10"),
            isolated_margin_usdt=Decimal("20"),
            maintenance_margin_rate=Decimal("0.05"),
            liquidation_close_fee_rate=Decimal("0.01"),
        )
        self.assertEqual(Decimal("8"), bankruptcy)
        self.assertGreater(liquidation, bankruptcy)

    def test_independent_estimate_uses_margin_tier_fee_and_funding(self):
        price = independent_long_liquidation_price(
            quantity_base=Decimal("10"),
            average_entry_price=Decimal("10"),
            isolated_margin_usdt=Decimal("30"),
            maintenance_margin_rate=Decimal("0.025"),
            liquidation_close_fee_rate=Decimal("0.006"),
            unbooked_funding_cost_usdt=Decimal("1"),
        )
        self.assertEqual(Decimal("71") / Decimal("9.69"), price)

    def test_null_or_nonpositive_exchange_value_never_means_safe(self):
        result = check_liquidation_buffer(
            independent_price=Decimal("7"),
            exchange_price=Decimal("0"),
            protective_stop=Decimal("9"),
            volatility_buffer=Decimal("0.5"),
            gap_stress=Decimal("0.5"),
            expected_slippage=Decimal("0.1"),
            exchange_semantics_verified=False,
        )
        self.assertFalse(result.sufficient)
        self.assertIn("EXCHANGE_LIQUIDATION_UNVERIFIED", result.reason_codes)

    def test_stop_buffer_uses_more_conservative_liquidation_estimate(self):
        result = check_liquidation_buffer(
            independent_price=Decimal("7.5"),
            exchange_price=Decimal("8"),
            protective_stop=Decimal("9"),
            volatility_buffer=Decimal("0.4"),
            gap_stress=Decimal("0.4"),
            expected_slippage=Decimal("0.1"),
            exchange_semantics_verified=True,
        )
        self.assertTrue(result.sufficient)
        self.assertEqual(Decimal("1"), result.stop_to_liquidation_buffer)


if __name__ == "__main__":
    unittest.main()
