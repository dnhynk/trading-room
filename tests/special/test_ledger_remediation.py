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
