from datetime import datetime, timezone
from decimal import Decimal
import unittest

from track_special.arx_campaign.marketdata import instrument_spec_from_observation
from track_special.arx_campaign.risk import load_research_profile


class ResearchProfileTests(unittest.TestCase):
    def test_exact_profile_builds_stage_cap_without_becoming_live_config(self):
        profile = load_research_profile("track_special/configs/research_profiles.yaml")
        instrument = instrument_spec_from_observation(
            {
                "symbol": "ARXUSDT",
                "category": "USDT-FUTURES",
                "baseCoin": "ARX",
                "quoteCoin": "USDT",
                "type": "perpetual",
                "status": "online",
                "quantityMultiplier": "1",
                "priceMultiplier": "0.00001",
                "minOrderQty": "1",
                "minOrderAmount": "5",
                "makerFeeRate": "0.0002",
                "takerFeeRate": "0.0006",
                "minLeverage": "1",
                "maxLeverage": "20",
                "fundInterval": "4",
            },
            datetime(2026, 9, 7, tzinfo=timezone.utc),
        )
        limits = profile.limits(
            e0_usdt=Decimal("1000"),
            stage=1,
            instrument=instrument,
            liquidation_buffer_min=Decimal("0.1"),
            giveback_cap_usdt=Decimal("50"),
            funding_cost_cap_usdt=Decimal("5"),
        )
        self.assertEqual(Decimal("3"), limits.leverage)
        self.assertEqual(Decimal("10.00"), limits.first_entry_loss_cap)
        self.assertEqual(Decimal("30.00"), limits.aggregate_loss_cap)
        self.assertEqual(Decimal("2000.0"), limits.gross_notional_cap)
        self.assertEqual(Decimal("400.000"), limits.stage_notional_cap)
        self.assertFalse(profile.live_approved)


if __name__ == "__main__":
    unittest.main()
