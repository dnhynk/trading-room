from datetime import datetime, timezone
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from track_special.arx_campaign.contracts import CampaignBook, OrderStatus, Reservation
from track_special.arx_campaign.risk import DurableRiskState, EntryCandidate, RiskEngine, RiskLimits


class RiskRemediationTests(unittest.TestCase):
    def test_arbitrary_step_and_explicit_unknown_liquidation_fail_closed(self):
        limits = RiskLimits(*map(Decimal, ("2", "100", "100", "100", "100", "100", "1", "100", "0.25", "0.25")))
        book = CampaignBook("c", Decimal("100"), (), (), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
        candidate = EntryCandidate(Decimal("1.12"), Decimal("10"), Decimal("8"), Decimal("10"))
        self.assertEqual(RiskEngine().assess(book, candidate, limits).approved_quantity, Decimal("1.00"))
        self.assertEqual(RiskEngine().assess(book, EntryCandidate(**{**candidate.__dict__}) if False else EntryCandidate(Decimal("1"),Decimal("10"),Decimal("8"),Decimal("10"),liquidation_verified=True), limits).approved_quantity, Decimal("0"))

    def test_durable_state_survives_restart_and_exit_only(self):
        state = DurableRiskState(timezone_name="Asia/Seoul")
        limits = RiskLimits(*map(Decimal, ("2", "100", "100", "100", "100", "100", "1", "100", "1", "1")), loss_streak_limit=1)
        state.apply_cycle(Decimal("-1"), limits)
        with TemporaryDirectory() as directory:
            path = f"{directory}/risk.json"; state.save(path)
            self.assertEqual(DurableRiskState.load(path).state.value, "EXIT_ONLY")

