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

 def test_null_live_template_loads_for_diagnostics_but_is_never_permitted(self):
  config=load("track_special/configs/live.example.yaml")
  self.assertFalse(config.live_permitted)
  self.assertIn("LIVE_DISABLED",config.live_issues)
  self.assertIn("MISSING_CAPITAL_BUDGET_USDT",config.live_issues)

 def test_state_directory_cannot_point_inside_repository(self):
  raw=json.loads(Path("track_special/configs/observe.yaml").read_text())
  raw["state_directory"]=str(Path.cwd().resolve()/"forbidden-state")
  with TemporaryDirectory() as d:
   p=Path(d)/"c.yaml"; p.write_text(json.dumps(raw))
   with self.assertRaises(ValueError): load(p)
