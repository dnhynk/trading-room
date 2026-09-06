from decimal import Decimal
from random import Random
import unittest

from track_special.arx_campaign.contracts import CampaignBook
from track_special.arx_campaign.risk import EntryCandidate, RiskEngine, RiskLimits


class RiskPropertyTests(unittest.TestCase):
    def test_randomized_approved_sizes_respect_step_and_all_independent_caps(self):
        random = Random(9282)
        engine = RiskEngine()
        book = CampaignBook(
            "campaign",
            Decimal("100"),
            (),
            (),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        )
        for _ in range(100):
            price = Decimal(random.randint(5, 20))
            stop = price - Decimal(random.randint(1, 4))
            limits = RiskLimits(
                leverage=Decimal("3"),
                e0=Decimal("100"),
                aggregate_loss_cap=Decimal(random.randint(2, 10)),
                gross_stop_cap=Decimal(random.randint(2, 10)),
                gross_notional_cap=Decimal(random.randint(10, 100)),
                isolated_margin_cap=Decimal(random.randint(5, 40)),
                liquidation_buffer_min=Decimal("0.5"),
                stage_notional_cap=Decimal(random.randint(10, 100)),
                quantity_step=Decimal("0.25"),
                min_quantity=Decimal("0.25"),
                min_notional=Decimal("1"),
            )
            result = engine.assess(
                book,
                EntryCandidate(
                    Decimal(random.randint(1, 40)) / Decimal("4"),
                    price,
                    stop,
                    price,
                    liquidation_price=stop - Decimal("1"),
                ),
                limits,
            )
            if result.approved_quantity > 0:
                self.assertEqual(Decimal("0"), result.approved_quantity % limits.quantity_step)
                self.assertLessEqual(result.principal_loss_at_stop, limits.aggregate_loss_cap)
                self.assertLessEqual(result.gross_stop_risk, limits.gross_stop_cap)
                self.assertLessEqual(result.gross_notional, limits.gross_notional_cap)
                self.assertLessEqual(result.isolated_margin, limits.isolated_margin_cap)


if __name__ == "__main__":
    unittest.main()
