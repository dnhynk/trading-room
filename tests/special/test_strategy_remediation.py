from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from track_special.arx_campaign.strategy import Bar, CampaignStrategy, StrategyConfig
from track_special.arx_campaign.contracts import CampaignState, OrderPurpose


class StrategyRemediationTests(unittest.TestCase):
    def test_ohlc_is_validated_and_box_excludes_signal_bar(self):
        now=datetime(2026,1,1,tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            Bar(now,now+timedelta(hours=1),Decimal("10"),Decimal("9"),Decimal("8"),Decimal("10"),Decimal("100"))
        config=StrategyConfig(box_lookback=2,pivot_left=1,pivot_right=1,volatility_lookback=1,relative_strength_lookback=1)
        bars=[Bar(now+timedelta(hours=i),now+timedelta(hours=i+1),Decimal("10"),Decimal("11"),Decimal("9"),Decimal("10"),Decimal("100")) for i in range(3)]
        self.assertIsNone(CampaignStrategy(config).evaluate(bars,now+timedelta(hours=4),Decimal("0")).purpose)

    def test_probe_is_pre_breakout_and_add_requires_new_executable_profit(self):
        now=datetime(2026,1,1,tzinfo=timezone.utc)
        config=StrategyConfig(box_lookback=2,pivot_left=1,pivot_right=1,volatility_lookback=1,relative_strength_lookback=1)
        def make(i,open_,high,low,close):
            return Bar(now+timedelta(hours=i),now+timedelta(hours=i+1),*(Decimal(str(x)) for x in (open_,high,low,close)),Decimal("100"))
        stabilized=[make(0,9,10,9,10),make(1,9,10,8,9),make(2,9,10,9,10)]
        strategy=CampaignStrategy(config)
        probe=strategy.evaluate(stabilized,now+timedelta(hours=4),Decimal("0"))
        self.assertEqual(OrderPurpose.PROBE_ENTRY,probe.purpose)
        self.assertIn("PRE_BREAKOUT_STABILIZATION",probe.reason_codes)
        breakout=stabilized+[make(3,10,12,10,12)]
        no_price_evidence=strategy.evaluate(
            breakout,now+timedelta(hours=5),Decimal("2"),next_stage=2,
            existing_profitable_after_costs=True,
        )
        self.assertIsNone(no_price_evidence.purpose)
        add=strategy.evaluate(
            breakout,now+timedelta(hours=5,minutes=1),Decimal("2"),next_stage=2,
            executable_exit_price=Decimal("11"),position_cost_basis=Decimal("9"),
        )
        self.assertEqual(OrderPurpose.PYRAMID_ENTRY,add.purpose)

    def test_harvest_reference_is_frozen_and_partial_fill_is_reissued(self):
        now=datetime(2026,1,1,tzinfo=timezone.utc)
        strategy=CampaignStrategy(StrategyConfig())
        strategy.state=CampaignState.RIDE
        strategy.state_since=now
        strategy.reference_quantity=Decimal("100")
        first=strategy.evaluate([],now+timedelta(hours=1),Decimal("100"),net_pnl_usdt=Decimal("10"),initial_risk_unit_usdt=Decimal("10"))
        self.assertEqual(Decimal("15.00"),first.reduce_quantity)
        remainder=strategy.evaluate([],now+timedelta(hours=1,minutes=1),Decimal("93"),filled_reduction_quantity=Decimal("7"),net_pnl_usdt=Decimal("10"),initial_risk_unit_usdt=Decimal("10"))
        self.assertEqual(Decimal("8.00"),remainder.reduce_quantity)
        second=strategy.evaluate([],now+timedelta(hours=1,minutes=2),Decimal("85"),filled_reduction_quantity=Decimal("8"),net_pnl_usdt=Decimal("20"),initial_risk_unit_usdt=Decimal("10"))
        self.assertEqual(Decimal("15.00"),second.reduce_quantity)
