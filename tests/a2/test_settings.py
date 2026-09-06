import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.a2.fakes import approved_config
from track_a_2.execution.preflight import (
    block_reasons, recovery_reasons, require_live,
)
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

    def test_approval_identifier_must_match_its_manifest_path(self):
        config = {
            **self.base,
            "live_approval_id": "a2-eval-first",
            "evaluation_manifest": "evaluations/a2-eval-second.json",
            "evaluation_sha256": "a" * 64,
        }
        with self.assertRaisesRegex(ValueError, "identity differ"):
            load(self.write(config))


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
        self.config = approved_config(self.root, self.config)
        self.path = self.root / "track_a_2" / "config.json"
        self.path.parent.mkdir(exist_ok=True)
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

    def test_evaluation_is_invalidated_when_evaluated_source_changes(self):
        config = load(self.path, root=self.root)
        source = self.root / "track_a_2" / "evaluated_source.py"
        source.write_text("# changed after evaluation\n", encoding="utf-8")
        self.assertIn(
            "evaluation_manifest_identity",
            block_reasons(config, root=self.root, egress="203.0.113.7"),
        )

    def test_recovery_only_ignores_entry_approval_but_requires_owned_exposure(self):
        state = resolved_state_directory(self.config, self.root)
        database = state / "a2-ledger.sqlite"
        ledger = dict(
            version=1,
            books={"BTC": {"lots": [["0.001", "100000000", "ta2-buy-owned"]]}},
            orders={},
        )
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            connection.execute("INSERT INTO state VALUES (1,?)", (json.dumps(ledger),))
            connection.commit()
        finally:
            connection.close()
        paused = dict(
            self.config, status="paused", mode="observe", execution_enabled=False,
            live_approval_id=None, evaluation_manifest=None, evaluation_sha256=None,
            universe=[],
        )
        self.assertEqual(
            recovery_reasons(paused, root=self.root, egress="203.0.113.7"), []
        )
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE state SET body=? WHERE id=1",
                (json.dumps(dict(version=1, books={}, orders={})),),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertIn(
            "recovery_exposure_missing",
            recovery_reasons(paused, root=self.root, egress="203.0.113.7"),
        )


if __name__ == "__main__":
    unittest.main()
