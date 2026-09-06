from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from track_special.arx_campaign.strategy import Bar, CampaignStrategy, StrategyConfig


class StrategyRemediationTests(unittest.TestCase):
    def test_ohlc_is_validated_and_box_excludes_signal_bar(self):
        now=datetime(2026,1,1,tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            Bar(now,now+timedelta(hours=1),Decimal("10"),Decimal("9"),Decimal("8"),Decimal("10"),Decimal("100"))
        config=StrategyConfig(box_lookback=2,pivot_left=1,pivot_right=1,volatility_lookback=1,relative_strength_lookback=1)
        bars=[Bar(now+timedelta(hours=i),now+timedelta(hours=i+1),Decimal("10"),Decimal("11"),Decimal("9"),Decimal("10"),Decimal("100")) for i in range(3)]
        self.assertIsNone(CampaignStrategy(config).evaluate(bars,now+timedelta(hours=4),Decimal("0")).purpose)
