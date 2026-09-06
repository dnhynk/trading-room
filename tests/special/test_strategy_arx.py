from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from track_special.arx_campaign.strategy import Bar, CampaignStrategy, StrategyConfig

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
def bar(i, low, high, close):
    return Bar(T+timedelta(hours=i), T+timedelta(hours=i+1), Decimal(str(low)), Decimal(str(high)), Decimal(str(low)), Decimal(str(close)), Decimal("100"))

class StrategyProperties(unittest.TestCase):
    def test_uncompleted_bar_cannot_create_probe(self):
        s=CampaignStrategy(StrategyConfig(box_lookback=2, pivot_left=1,pivot_right=1,volatility_lookback=1,relative_strength_lookback=1))
        bars=[bar(0,9,10,10),bar(1,8,10,9),bar(2,9,10,10),bar(3,9,12,12)]
        bars[-1]=Bar(bars[-1].opened_at,bars[-1].closed_at,bars[-1].open,bars[-1].high,bars[-1].low,bars[-1].close,bars[-1].benchmark_close,False)
        with_incomplete=s.evaluate(bars,T+timedelta(hours=5),Decimal("0")).purpose
        baseline=CampaignStrategy(s.config).evaluate(bars[:-1],T+timedelta(hours=5),Decimal("0")).purpose
        self.assertEqual(baseline,with_incomplete)
    def test_harvest_fractions_are_exact(self):
        self.assertEqual(sum(StrategyConfig().harvest_fractions),Decimal("1"))
