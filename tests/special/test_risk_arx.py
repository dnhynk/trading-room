from datetime import datetime, timezone
from decimal import Decimal
import unittest
from track_special.arx_campaign.contracts import CampaignBook
from track_special.arx_campaign.risk import EntryCandidate, RiskEngine, RiskLimits

class RiskTests(unittest.TestCase):
 def test_rounds_down_to_independent_notional_cap(self):
  limits=RiskLimits(Decimal("2"),Decimal("100"),Decimal("100"),Decimal("100"),Decimal("20"),Decimal("20"),Decimal("1"),Decimal("20"),Decimal("1"),Decimal("1"))
  b=CampaignBook("x",Decimal("100"),(),(),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"))
  r=RiskEngine().assess(b,EntryCandidate(Decimal("5"),Decimal("10"),Decimal("8"),Decimal("10")),limits)
  self.assertEqual(r.approved_quantity,Decimal("2"))
