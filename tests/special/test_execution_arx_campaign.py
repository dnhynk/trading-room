from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from track_special.arx_campaign.contracts import OrderIntent, OrderPurpose
from track_special.arx_campaign.execution.engine import CampaignEngine, LiveTransport, ProcessLock

NOW=datetime(2026,9,7,tzinfo=timezone.utc)
def intent(i="i1"):
 return OrderIntent(i,"c",OrderPurpose.PROBE_ENTRY,"buy",Decimal("2"),None,"GTC",False,1,"h",NOW,NOW,NOW+timedelta(minutes=1),("test",))
class TimeoutTransport:
 def submit(self,*_): raise TimeoutError()
class ExecutionArxCampaignTests(unittest.TestCase):
 def test_timeout_reconciles_before_retry_and_duplicate_is_idempotent(self):
  with TemporaryDirectory() as d:
   e=CampaignEngine(Path(d)/"x.db",TimeoutTransport()); oid=e.reserve(intent()); e.submit("i1"); self.assertEqual(e.status()["orders"][0]["status"],"result_unknown")
   e.reconcile(oid,"open"); e.reconcile(oid,"partially_filled",Decimal("1")); e.reconcile(oid,"partially_filled",Decimal("1")); self.assertEqual(e.status()["orders"][0]["filled"],"1"); e.close()
 def test_cancel_race_fill_and_protection_gap_pause_entries(self):
  with TemporaryDirectory() as d:
   e=CampaignEngine(Path(d)/"x.db"); oid=e.reserve(intent()); e.cancel_entry_orders(); e.reconcile(oid,"canceled",Decimal("2"),Decimal("0")); self.assertEqual(e.status()["orders"][0]["status"],"filled"); self.assertEqual(e.status()["risk_state"],"PAUSE_ENTRIES"); e.close()
 def test_live_is_non_operational_and_process_lock_rejects_duplicate(self):
  with TemporaryDirectory() as d:
   p=Path(d)/"x.db"
   with self.assertRaises(RuntimeError): LiveTransport().submit("x")
   with ProcessLock(p):
    with self.assertRaises(RuntimeError): ProcessLock(p).__enter__()
