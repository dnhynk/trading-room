from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from track_special.arx_campaign.ledger import LedgerStore


class LedgerRemediationTests(unittest.TestCase):
    def test_reservation_states_are_typed_and_postings_balance(self):
        with TemporaryDirectory() as directory:
            ledger=LedgerStore(Path(directory)/"ledger.db"); now=datetime.now(timezone.utc)
            self.assertTrue(ledger.reserve("i","o",Decimal("1"),Decimal("10"),now,"reserve"))
            with self.assertRaises(ValueError): ledger.transition_reservation("i","anything",now,"bad")
            self.assertTrue(ledger.transition_reservation("i","submitting",now,"submit"))
            self.assertTrue(ledger.post_realized("fee",now,"FEE",Decimal("-1"),"f"))
            self.assertEqual(ledger.db.execute("select count(*) from postings where debit_account <> credit_account").fetchone()[0], 2)
            ledger.close()

    def test_event_types_do_not_collide_and_margin_return_is_not_profit(self):
        with TemporaryDirectory() as directory:
            ledger=LedgerStore(Path(directory)/"ledger.db"); now=datetime.now(timezone.utc)
            self.assertTrue(ledger.post_realized("trade",now,"REALIZED_PNL",Decimal("2"),"exchange-1"))
            self.assertTrue(ledger.post_realized("fee",now,"FEE",Decimal("-0.1"),"exchange-1"))
            self.assertTrue(ledger.post_margin_memo("margin",now,Decimal("10"),False,"margin-1"))
            kinds={row[0] for row in ledger.db.execute("select kind from events")}
            self.assertEqual({"REALIZED_PNL","FEE","MARGIN_RETURNED"},kinds)
            self.assertEqual(Decimal("0"),ledger.allocate_new_high_water("c",Decimal("0")).reusable)
            ledger.close()
