import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from track_special.arx_campaign.config import load
class ConfigArxCampaignTests(unittest.TestCase):
 def test_observe_rejects_live_enabled(self):
  raw=json.loads(Path("track_special/configs/observe.yaml").read_text()); raw["live_enabled"]=True
  with TemporaryDirectory() as d:
   p=Path(d)/"c.yaml"; p.write_text(json.dumps(raw))
   with self.assertRaises(ValueError): load(p)
