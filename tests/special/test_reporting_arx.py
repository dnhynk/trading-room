from decimal import Decimal
import unittest

from track_special.arx_campaign.reporting.korean import daily_report


class KoreanReportingTests(unittest.TestCase):
    def test_e0_equity_and_unknown_exchange_values_are_separate(self):
        report = daily_report(
            {
                "e0_usdt": Decimal("1000"),
                "strategy_equity_usdt": Decimal("975.25"),
                "campaign_net_pnl_usdt": Decimal("-24.75"),
                "risk_state": "PAUSE_ENTRIES",
            }
        )
        self.assertIn("승인 시작자본 E0(USDT): 1,000.0", report)
        self.assertIn("전략 순자산(USDT): 975.2", report)
        self.assertIn("캠페인 순손익(USDT): -24.8", report)
        self.assertIn("추정 청산가/완충: UNKNOWN / UNKNOWN", report)


if __name__ == "__main__":
    unittest.main()
