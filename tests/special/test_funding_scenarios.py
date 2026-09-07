from decimal import Decimal
import unittest

from track_special.arx_campaign.risk import project_funding_scenarios


class FundingScenarioTests(unittest.TestCase):
    def test_scenarios_use_observed_interval_and_report_e0_and_notional_ratios(self):
        results = project_funding_scenarios(
            notional_usdt=Decimal("200"),
            e0_usdt=Decimal("100"),
            holding_hours=Decimal("9"),
            observed_interval_hours=Decimal("4"),
            rates={
                "base": Decimal("0.0001"),
                "adverse": Decimal("0.001"),
                "extreme": Decimal("0.01"),
            },
        )
        self.assertEqual(3, results[0].settlements)
        self.assertEqual(Decimal("6.00"), results[2].cost_usdt)
        self.assertEqual(Decimal("6.00"), results[2].pct_of_e0)
        self.assertEqual(Decimal("3.00"), results[2].pct_of_notional)

    def test_missing_interval_and_favorable_projection_fail_safe(self):
        with self.assertRaises(ValueError):
            project_funding_scenarios(
                notional_usdt=Decimal("1"),
                e0_usdt=Decimal("1"),
                holding_hours=Decimal("1"),
                observed_interval_hours=None,
                rates={"base": Decimal("0"), "adverse": Decimal("0"), "extreme": Decimal("0")},
            )
        result = project_funding_scenarios(
            notional_usdt=Decimal("100"),
            e0_usdt=Decimal("100"),
            holding_hours=Decimal("4"),
            observed_interval_hours=Decimal("4"),
            rates={"base": Decimal("-0.01"), "adverse": Decimal("0"), "extreme": Decimal("0.01")},
        )
        self.assertEqual(Decimal("0"), result[0].cost_usdt)


if __name__ == "__main__":
    unittest.main()
