from datetime import datetime, timezone
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from track_special.arx_campaign.contracts import CampaignBook, OrderStatus, PositionLot, Reservation
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

    def test_stop_pnl_giveback_and_gross_risk_do_not_double_count_booked_costs(self):
        now=datetime(2026,9,7,tzinfo=timezone.utc)
        lot=PositionLot("lot",Decimal("2"),Decimal("10"),Decimal("9"),now,Decimal("4"))
        reservation=Reservation("pending","oid",Decimal("1"),Decimal("11"),Decimal("0.1"),Decimal("0.2"),now,now.replace(hour=1),OrderStatus.RESULT_UNKNOWN,2)
        book=CampaignBook("c",Decimal("100"),(lot,),(reservation,),Decimal("1"),Decimal("5"),Decimal("1"),Decimal("0"),Decimal("0"),{2:Decimal("10")})
        limits=RiskLimits(
            Decimal("3"),Decimal("100"),Decimal("50"),Decimal("50"),Decimal("100"),Decimal("50"),Decimal("1"),Decimal("50"),Decimal("1"),Decimal("1"),
            giveback_cap=Decimal("50"),funding_cost_cap=Decimal("5"),
        )
        candidate=EntryCandidate(
            Decimal("1"),Decimal("12"),Decimal("8"),Decimal("12"),
            entry_fee=Decimal("0.12"),stressed_funding=Decimal("0.08"),
            future_exit_fee=Decimal("0.3"),future_exit_fee_rate=Decimal("0.01"),
            existing_position_stressed_funding=Decimal("0.5"),
            existing_executable_exit_price=Decimal("20"),
            stage=2,is_pyramid=True,existing_position_profitable_after_costs=True,
        )
        result=RiskEngine().assess(book,candidate,limits)
        self.assertEqual(Decimal("1"),result.approved_quantity)
        self.assertEqual(Decimal("-11.62"),result.pnl_at_stop)
        self.assertEqual(Decimal("11.62"),result.principal_loss_at_stop)
        self.assertEqual(Decimal("16.62"),result.giveback_at_stop)
        self.assertEqual(Decimal("12.62"),result.gross_stop_risk)
        self.assertEqual(Decimal("0.78"),result.stressed_future_funding)

    def test_live_style_private_unknown_and_loss_add_are_blocked(self):
        limits=RiskLimits(*map(Decimal,("2","100","100","100","100","100","1","100","1","1")))
        book=CampaignBook("c",Decimal("100"),(),(),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"))
        private=EntryCandidate(Decimal("1"),Decimal("10"),Decimal("8"),Decimal("10"),require_private_verification=True)
        self.assertIn("PRIVATE_PREFLIGHT_INCOMPLETE",RiskEngine().assess(book,private,limits).reason_codes)
        losing_add=EntryCandidate(Decimal("1"),Decimal("10"),Decimal("8"),Decimal("10"),is_pyramid=True)
        self.assertIn("PYRAMID_PROFIT_INPUT_UNVERIFIED",RiskEngine().assess(book,losing_add,limits).reason_codes)

    def test_caller_profit_boolean_cannot_override_computed_losing_position(self):
        now=datetime(2026,9,7,tzinfo=timezone.utc)
        lot=PositionLot("lot",Decimal("1"),Decimal("10"),Decimal("0.1"),now,Decimal("2"))
        book=CampaignBook("c",Decimal("100"),(lot,),(),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"))
        limits=RiskLimits(*map(Decimal,("2","100","100","100","100","100","1","100","1","1")))
        spoofed=EntryCandidate(
            Decimal("1"),Decimal("10"),Decimal("8"),Decimal("10"),
            is_pyramid=True,existing_position_profitable_after_costs=True,
            existing_executable_exit_price=Decimal("9"),
        )
        result=RiskEngine().assess(book,spoofed,limits)
        self.assertIn("PYRAMID_NOT_PROFITABLE_AFTER_COSTS",result.reason_codes)
        self.assertEqual(Decimal("0"),result.approved_quantity)

    def test_external_deposit_cannot_hide_period_loss_or_clear_campaign_halt(self):
        now=datetime(2026,9,7,tzinfo=timezone.utc)
        limits=RiskLimits(
            *map(Decimal,("2","100","100","100","100","100","1","100","1","1")),
            daily_loss_cap=Decimal("3"),campaign_loss_cap=Decimal("10"),timezone_name="Asia/Seoul",
        )
        clean=CampaignBook("c",Decimal("100"),(),(),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"))
        state=DurableRiskState(timezone_name="Asia/Seoul")
        state.gate(now,Decimal("100"),clean,limits)
        state.gate(now.replace(hour=1),Decimal("200"),clean,limits,net_external_flow=Decimal("100"))
        _,reasons=state.gate(now.replace(hour=2),Decimal("196"),clean,limits)
        self.assertIn("DAILY_LOSS_LIMIT",reasons)
        halted=CampaignBook("c",Decimal("100"),(),(),Decimal("0"),Decimal("-10"),Decimal("0"),Decimal("0"),Decimal("0"))
        state.gate(now.replace(hour=3),Decimal("196"),halted,limits)
        self.assertTrue(state.permanent_halt)
        self.assertFalse(state.attempt_periodic_resume(now.replace(day=14)))
