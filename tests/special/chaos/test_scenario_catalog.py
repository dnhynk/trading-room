import unittest

from track_special.arx_campaign.replay import SCENARIOS


class ScenarioCatalogTests(unittest.TestCase):
    def test_required_failure_catalog_is_complete_and_never_profit_evidence(self):
        expected = {
            "long_decline",
            "pump_absent",
            "probe_only_then_rally",
            "fake_breakout_after_add",
            "gap_collapse",
            "mark_last_divergence",
            "funding_spike_interval_change",
            "trading_halt",
            "server_stop_rejected",
            "residual_orders_after_liquidation",
            "adl_forced_reduction",
            "restart_duplicate_order",
        }
        self.assertEqual(expected, set(SCENARIOS))
        self.assertTrue(all(not scenario.profitability_evidence for scenario in SCENARIOS.values()))


if __name__ == "__main__":
    unittest.main()
