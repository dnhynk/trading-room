from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from track_special.arx_campaign.ledger import LedgerStore

class LedgerTests(unittest.TestCase):
 def test_realized_high_water_and_idempotent_fee(self):
  with TemporaryDirectory() as d:
   x=LedgerStore(Path(d)/"x.db"); now=datetime.now(timezone.utc)
   self.assertTrue(x.post_realized("a",now,"FEE",Decimal("-1"),"fee-1")); self.assertFalse(x.post_realized("b",now,"FEE",Decimal("-1"),"fee-1"))
   self.assertEqual(x.allocate_new_high_water("c",Decimal("100")).reusable,Decimal("25"))
   self.assertEqual(x.allocate_new_high_water("c",Decimal("90")).reserve,Decimal("75")); x.close()
