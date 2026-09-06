import unittest
from track_special.arx_campaign.cli import main
class CliArxCampaignTests(unittest.TestCase):
 def test_collect_has_actionable_failure(self):
  with self.assertRaises(SystemExit): main(["collect"])
