from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import unittest

from track_special.arx_campaign.config import validate
from track_special.arx_campaign.contracts import CampaignBook, OrderStatus, Reservation
from track_special.arx_campaign.marketdata import instrument_spec_from_observation
from track_special.arx_campaign.risk import EntryCandidate, RiskEngine, load_research_profile


PROFILE = "full_seed_10x_preposition_research"
NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


class PrepositionProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_research_profile(
            "track_special/configs/research_profiles.yaml", PROFILE
        )
        instrument = instrument_spec_from_observation(
            {
                "symbol": "ARXUSDT", "category": "USDT-FUTURES",
                "baseCoin": "ARX", "quoteCoin": "USDT", "type": "perpetual",
                "status": "online", "quantityMultiplier": "1",
                "priceMultiplier": "0.00001", "minOrderQty": "1",
                "minOrderAmount": "5", "makerFeeRate": "0.0002",
                "takerFeeRate": "0.0006", "minLeverage": "1",
                "maxLeverage": "20", "fundInterval": "4",
            }, NOW,
        )
        self.limits = self.profile.limits(
            e0_usdt=D("100"), stage=1, instrument=instrument,
            liquidation_buffer_min=D("0.05"), giveback_cap_usdt=D("100"),
            funding_cost_cap_usdt=D("100"),
        )
        self.book = CampaignBook(
            "synthetic-preposition", D("100"), (), (),
            D("0"), D("0"), D("0"), D("0"), D("0"),
        )
        self.candidate = EntryCandidate(
            quantity=D("100"), worst_fill_price=D("10"), stop_price=D("9.4"),
            mark_price=D("10"), entry_fee=D("0.6"), future_exit_fee_rate=D("0.0006"),
            liquidation_price=D("9.23"), available_usdt=D("100"),
            equity_usdt=D("100"), stage=1,
        )

    def test_first_stage_can_use_full_target_but_entry_fees_reduce_quantity(self):
        result = RiskEngine().assess(self.book, self.candidate, self.limits)
        self.assertEqual(D("99"), result.approved_quantity)
        self.assertEqual(D("9.9"), result.effective_leverage)
        self.assertGreater(result.principal_loss_at_stop, D("50"))
        self.assertLessEqual(result.principal_loss_at_stop, D("100"))
        self.assertEqual(D("0.406"), result.available_funds_after_margin)
        self.assertIn("SIZE_REDUCED_TO_LIMITS", result.reason_codes)

    def test_unknown_entry_reservation_still_consumes_the_single_budget(self):
        reservation = Reservation(
            "reserved", "unknown-order", D("40"), D("10"), D("0.24"), D("0"),
            NOW, NOW + timedelta(minutes=1), OrderStatus.RESULT_UNKNOWN, 1,
        )
        book = replace(self.book, reservations=(reservation,))
        result = RiskEngine().assess(book, self.candidate, self.limits)
        self.assertEqual(D("59"), result.approved_quantity)
        self.assertEqual(D("990"), result.gross_notional)
        self.assertEqual(D("0.406"), result.available_funds_after_margin)

    def test_full_preposition_budget_does_not_create_a_second_entry_stage(self):
        result = RiskEngine().assess(
            self.book, replace(self.candidate, stage=2), self.limits
        )
        self.assertEqual(D("0"), result.approved_quantity)
        self.assertIn("ENTRY_STAGE_INVALID", result.reason_codes)

    def test_total_loss_tolerance_does_not_remove_account_verification(self):
        result = RiskEngine().assess(
            self.book, replace(self.candidate, require_private_verification=True),
            self.limits,
        )
        self.assertEqual(D("0"), result.approved_quantity)
        self.assertIn("PRIVATE_PREFLIGHT_INCOMPLETE", result.reason_codes)

    def test_named_research_profile_cannot_become_live_by_copying_its_name(self):
        raw = json.loads(Path("track_special/configs/live.example.yaml").read_text())
        raw["research_profile"] = PROFILE
        config = validate(raw)
        self.assertFalse(config.live_permitted)
        self.assertIn("RESEARCH_PROFILE_NOT_LIVE_APPROVED", config.live_issues)


if __name__ == "__main__":
    unittest.main()
