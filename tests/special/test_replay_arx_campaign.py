from decimal import Decimal
import unittest
from track_special.arx_campaign.replay import ReplayBar,replay
class ReplayArxCampaignTests(unittest.TestCase):
 def test_same_bar_stop_is_adverse_and_partial_fill_preserved(self):
  r=replay([ReplayBar(Decimal("10"),Decimal("12"),Decimal("8"),Decimal("11"),Decimal("9"),Decimal("10"),available_quantity=Decimal("2"))],Decimal("3"),stop=Decimal("9"),take_profit=Decimal("11"))
  self.assertEqual(r.filled_quantity,Decimal("2")); self.assertEqual(r.unfilled_quantity,Decimal("1")); self.assertIn("stop_before_profit_same_bar",r.notes)

 def test_funding_only_posts_on_settlement_and_mmr_is_not_liquidation_formula(self):
  bars=[ReplayBar(Decimal("10"),Decimal("11"),Decimal("9"),Decimal("10"),Decimal("9.9"),Decimal("10.1"),funding_rate=Decimal("0.01"),maintenance_rate=Decimal("0.9"),mark=Decimal("10"))]
  result=replay(bars,Decimal("1"))
  self.assertEqual(Decimal("0"),result.funding)
  self.assertFalse(result.liquidated)
  self.assertFalse(result.liquidation_verifiable)

 def test_explicit_liquidation_price_is_adverse_before_stop(self):
  bar=ReplayBar(Decimal("10"),Decimal("10"),Decimal("7"),Decimal("8"),Decimal("7.5"),Decimal("10"),mark=Decimal("7.8"),liquidation_price=Decimal("8"))
  result=replay([bar],Decimal("1"),stop=Decimal("8.5"))
  self.assertTrue(result.liquidated)
  self.assertIn("explicit_synthetic_liquidation_before_stop",result.notes)

 def test_disappearing_bid_depth_reports_remaining_exposure(self):
  bar=ReplayBar(
   Decimal("10"),Decimal("10"),Decimal("8"),Decimal("8.5"),Decimal("8.4"),Decimal("10"),
   ask_depth=Decimal("1"),bid_depth=Decimal("0"),mark=Decimal("8.5"),
  )
  result=replay([bar],Decimal("1"),stop=Decimal("9"),server_protection_verified=True)
  self.assertEqual(Decimal("0"),result.exit_filled_quantity)
  self.assertEqual(Decimal("1"),result.remaining_position_quantity)
  self.assertEqual(Decimal("0"),result.exit_value)
  self.assertIn("position_exposure_remains",result.notes)

 def test_stop_uses_adverse_intrabar_low_when_point_mark_misses_the_cross(self):
  bar=ReplayBar(
   Decimal("10"),Decimal("10.5"),Decimal("8"),Decimal("10"),Decimal("8.8"),Decimal("10"),
   mark=Decimal("10"),bid_depth=Decimal("1"),
  )
  result=replay([bar],Decimal("1"),stop=Decimal("9"))
  self.assertEqual(Decimal("1"),result.exit_filled_quantity)
  self.assertIn("stop_before_profit_same_bar",result.notes)
