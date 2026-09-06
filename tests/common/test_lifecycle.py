"""A paused checkout must refuse engine startup before account access or spawning."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common.lifecycle import require_active, startup_block_reason
from common import supervise


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        self.state = {"version": 1, "tracks": {"A": {"status": "paused"}, "B": {"status": "paused"}}}
        self.write()

    def write(self, hunter=1):
        (self.root / "config" / "tracks.json").write_text(json.dumps(self.state), encoding="utf-8")
        (self.root / "config" / "bitget.json").write_text(json.dumps({"hunt": {"on": hunter}}), encoding="utf-8")

    def test_every_supervisor_stays_paused(self):
        for job in supervise.JOBS:
            with self.subTest(job=job), patch.object(supervise, "ROOT", str(self.root)), patch.object(supervise.sys, "argv", ["supervise", job]), patch.object(supervise, "spawn") as spawn:
                with self.assertRaisesRegex(SystemExit, "START_BLOCKED"):
                    supervise.main()
                spawn.assert_not_called()
        self.assertFalse((self.root / "logs").exists())

    def test_only_explicitly_active_selected_track_may_start(self):
        self.state["tracks"]["A"]["status"] = "active"
        self.write(hunter=1)
        self.assertIsNone(startup_block_reason("select", self.root))
        self.assertIn("B", startup_block_reason("cycle", self.root))
        self.write(hunter=0)
        self.assertIsNone(startup_block_reason("cycle", self.root))
        self.assertIn("B", startup_block_reason("hunt", self.root))

    def test_missing_or_malformed_control_never_enables_trading(self):
        state = self.root / "config" / "tracks.json"
        for value in ("", "null", "[]", "{}", '{"version":1,"tracks":{}}'):
            state.write_text(value, encoding="utf-8")
            self.assertIsNotNone(startup_block_reason("cycle", self.root))
        state.unlink()
        with self.assertRaises(SystemExit):
            require_active("cycle", self.root)

    def test_malformed_selector_is_not_silently_track_b(self):
        self.state["tracks"]["B"]["status"] = "active"
        for selector in ("0", 2, None, [], 1.0):
            self.write(hunter=selector)
            self.assertIsNotNone(startup_block_reason("cycle", self.root))


if __name__ == "__main__":
    unittest.main()
