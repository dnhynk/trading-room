from decimal import Decimal
import unittest
from track_special.arx_campaign.replay import ReplayBar,replay
class ReplayArxCampaignTests(unittest.TestCase):
 def test_same_bar_stop_is_adverse_and_partial_fill_preserved(self):
  r=replay([ReplayBar(Decimal("10"),Decimal("12"),Decimal("8"),Decimal("11"),Decimal("9"),Decimal("10"),available_quantity=Decimal("2"))],Decimal("3"),stop=Decimal("9"),take_profit=Decimal("11"))
  self.assertEqual(r.filled_quantity,Decimal("2")); self.assertEqual(r.unfilled_quantity,Decimal("1")); self.assertIn("stop_before_profit_same_bar",r.notes)
