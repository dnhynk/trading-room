from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from track_special.arx_campaign.cli import main
from track_special.arx_campaign.marketdata.collector import SnapshotReceipt


class CliArxCampaignTests(unittest.TestCase):
    def test_collect_uses_unsigned_public_receipt_and_reports_zero_writes(self):
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            receipt = SnapshotReceipt(
                now,
                now,
                {"ticker": Path(directory) / "ticker.jsonl"},
                {"ticker": 1},
                {"ticker": 1},
                {"ticker": 1},
                {"ticker": None},
                {"ticker": 1.0},
                {"ticker": 0},
                {},
                {},
                True,
                True,
            )
            output = StringIO()
            with patch(
                "track_special.arx_campaign.marketdata.collector.collect_public_snapshot",
                return_value=receipt,
            ), redirect_stdout(output):
                result = main(["--state-directory", directory, "collect"])
        value = json.loads(output.getvalue())
        self.assertEqual(0, result)
        self.assertEqual(0, value["exchange_writes"])
        self.assertEqual("actual_unsigned_public_api", value["evidence_kind"])

    def test_live_template_is_reportable_but_remains_blocked(self):
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "--config",
                    "track_special/configs/live.example.yaml",
                    "validate-live",
                ]
            )
        value = json.loads(output.getvalue())
        self.assertEqual(2, result)
        self.assertFalse(value["live_operational"])
        self.assertIn("LIVE_ADAPTER_NOT_IMPLEMENTED", value["blockers"])
        self.assertEqual(0, value["private_requests"])

    def test_synthetic_replay_is_labeled_and_has_zero_exchange_writes(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main(["replay", "--scenario", "gap_collapse"]))
        value = json.loads(output.getvalue())
        self.assertIn("not_profitability_evidence", value["evidence_kind"])
        self.assertEqual(0, value["exchange_writes"])


if __name__ == "__main__":
    unittest.main()
