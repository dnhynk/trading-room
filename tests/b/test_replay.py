"""Matched offline input snapshots; no exchange access or source checkout writes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from track_b import replay
from track_b.replay import FrozenCache


class OfflineInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cache = FrozenCache(self.tmp.name)

    def write(self, name, value):
        with open(os.path.join(self.tmp.name, name), "w", encoding="utf-8") as fh: json.dump(value, fh)

    def test_missing_contract_is_an_error_not_default_precision(self):
        with self.assertRaises(FileNotFoundError): self.cache.contract("X")

    def test_missing_seed_is_frozen_for_both_variants_and_declared(self):
        self.assertEqual(self.cache.seed("X", 120), (None, None, None))
        self.write("seed-X-120.json", dict(c1=[], c15=[], daily=[]))
        self.assertEqual(self.cache.seed("X", 120), (None, None, None))
        self.assertIn("missing", self.cache.manifest["seed-X-120.json"])

    def test_caller_mutation_or_a_changed_file_cannot_change_the_other_variants_seed(self):
        name = "seed-X-120.json"
        self.write(name, dict(c1=[dict(ts=60000, c=1)], c15=[], daily=[]))
        first = self.cache.seed("X", 120); first[0][0]["c"] = 999
        self.write(name, dict(c1=[dict(ts=60000, c=50)], c15=[], daily=[]))
        self.assertEqual(self.cache.seed("X", 120)[0][0]["c"], 1)

    def test_a_candle_closed_after_the_replay_start_is_rejected(self):
        self.write("seed-X-120.json", dict(c1=[dict(ts=120000)], c15=[], daily=[]))
        with self.assertRaises(ValueError): self.cache.seed("X", 120)


class FrozenRevisionTests(unittest.TestCase):
    def test_reference_uses_committed_track_layout_and_restores_live_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'common').mkdir()
            (root/'track_a').mkdir()
            (root/'common/signal.py').write_text('VALUE = 7\n', encoding='utf-8')
            (root/'track_a/backtest.py').write_text('from common.signal import VALUE\nRESULT = VALUE * 2\n', encoding='utf-8')
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.PIPE)
            git('init', '-q')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture')
            (root/'common/signal.py').write_text('VALUE = 999\n', encoding='utf-8')
            original = sys.modules['common.signal']
            with patch.object(replay, 'ROOT', str(root)):
                frozen_signal, frozen_backtest = replay.reference('HEAD')
            self.assertEqual(frozen_signal.VALUE, 7)
            self.assertEqual(frozen_backtest.RESULT, 14)
            self.assertIs(sys.modules['common.signal'], original)


if __name__ == "__main__": unittest.main()
