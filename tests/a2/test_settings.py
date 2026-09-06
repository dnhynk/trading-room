import copy
import json
from pathlib import Path
import tempfile
import unittest

from track_a_2.execution.preflight import block_reasons, require_live
from track_a_2.settings import CONFIG, load, resolved_state_directory


class TrackA2SettingsTests(unittest.TestCase):
    def setUp(self):
        self.base = load(CONFIG)

    def write(self, config):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_repository_configuration_is_paused_coinone_spot_long_only(self):
        config = self.base
        self.assertEqual(config["version"], 2)
        self.assertEqual((config["venue"], config["market"], config["quote_currency"]),
                         ("coinone", "spot", "KRW"))
        self.assertEqual(config["sides"], ["long"])
        self.assertEqual((config["status"], config["mode"]), ("paused", "observe"))
        self.assertFalse(config["execution_enabled"])
        self.assertFalse(config["portfolio_isolation_confirmed"])
        self.assertEqual(config["universe"], [])

    def test_identity_and_derivative_fields_fail_closed(self):
        for change in ({"sides": ["long", "short"]}, {"venue": "bitget"},
                       {"market": "margin"}, {"quote_currency": "USDT"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "safety contract"):
                load(self.write({**self.base, **change}))
        config = copy.deepcopy(self.base)
        config["strategy"]["lever"] = 2
        with self.assertRaisesRegex(ValueError, "strategy fields"):
            load(self.write(config))

    def test_live_mode_and_execution_flag_are_atomic(self):
        for change in ({"execution_enabled": True}, {"mode": "live"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "change together"):
                load(self.write({**self.base, **change}))
        config = {**self.base, "mode": "live", "execution_enabled": True}
        self.assertTrue(load(self.write(config))["execution_enabled"])

    def test_unknown_or_secret_fields_fail_closed(self):
        for field in ("api_key", "secret", "parameters"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "fields"):
                load(self.write({**self.base, field: "must-not-live-here"}))

    def test_state_directory_cannot_mix_with_track_c_or_repository(self):
        for value in ("../trading-room-state/track-c", "track-a-2", "../trading-room-state"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "dedicated sibling"):
                load(self.write({**self.base, "state_directory": value}))
        self.assertEqual(resolved_state_directory(self.base).name, "track-a-2")

    def test_random_measurement_baseline_cannot_execute(self):
        config = copy.deepcopy(self.base)
        config["strategy"]["entry_random"] = 0.1
        with self.assertRaisesRegex(ValueError, "baselines"):
            load(self.write(config))

    def test_invalid_risk_relationships_are_rejected(self):
        for change in (
            {"unit_fraction": 0.3, "book_notional_fraction": 0.2},
            {"book_notional_fraction": 0.7, "portfolio_notional_fraction": 0.6},
            {"book_risk_fraction": 0.03, "daily_loss_fraction": 0.02},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                load(self.write({**self.base, **change}))


class TrackA2PreflightTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        parent = Path(self.folder.name)
        self.root = parent / "trading-room"
        (self.root / "config").mkdir(parents=True)
        self.config = {
            **load(CONFIG),
            "status": "active",
            "mode": "live",
            "execution_enabled": True,
            "portfolio_isolation_confirmed": True,
            "live_approval_id": "a2-eval-regression-v1",
            "expected_egress_ip": "203.0.113.7",
            "universe": ["BTC", "ETH"],
        }
        self.path = self.root / "track_a_2" / "config.json"
        self.path.parent.mkdir()
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        (self.root / "config" / "tracks.json").write_text(json.dumps(dict(
            tracks={"A-2": {"status": "active", "execution_enabled": True}}
        )), encoding="utf-8")

    def test_all_independent_activation_gates_can_pass(self):
        config = load(self.path, root=self.root)
        self.assertEqual(block_reasons(config, root=self.root, egress="203.0.113.7"), [])
        self.assertTrue(require_live(config, root=self.root, egress="203.0.113.7"))

    def test_controls_and_egress_fail_closed(self):
        config = load(self.path, root=self.root)
        (self.root / "PAUSE").touch()
        reasons = block_reasons(config, root=self.root, egress="203.0.113.8")
        self.assertIn("repository_pause", reasons)
        self.assertIn("egress_ip_mismatch", reasons)
        with self.assertRaisesRegex(RuntimeError, "preflight blocked"):
            require_live(config, root=self.root, egress="203.0.113.8")

    def test_registry_and_local_configuration_are_separate_locks(self):
        config = load(self.path, root=self.root)
        (self.root / "config" / "tracks.json").write_text(json.dumps(dict(
            tracks={"A-2": {"status": "paused", "execution_enabled": False}}
        )), encoding="utf-8")
        reasons = block_reasons(config, root=self.root, egress="203.0.113.7")
        self.assertIn("track_registry_not_active", reasons)
        self.assertIn("track_registry_execution_disabled", reasons)


if __name__ == "__main__":
    unittest.main()
