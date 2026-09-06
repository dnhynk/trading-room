import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from track_a_2.observe import Capture, coins, prepare_observation
from track_a_2.replay.loader import Observation
from track_a_2.settings import load


class PublicClient:
    def __init__(self):
        self.calls = []

    def universe(self):
        contract = dict(
            quote_currency="KRW", target_currency="AAA", trade_status=1,
            maintenance_status=0, order_types=["limit", "market", "stop_limit"],
        )
        ticker = dict(quote_currency="KRW", target_currency="AAA", last="100")
        return [contract], [ticker]

    def price_units(self, coin):
        self.calls.append(("units", coin))
        return [{"range_min": "0", "price_unit": "0.01"}]

    def orderbook(self, coin):
        self.calls.append(("book", coin))
        return dict(quote_currency="KRW", target_currency=coin, bids=[], asks=[])

    def candles(self, coin, interval, size):
        self.calls.append(("candles", coin, interval, size))
        return []


class ObserveTests(unittest.TestCase):
    def test_symbols_are_explicit_unique_and_bounded(self):
        self.assertEqual(coins(["btc", "ETH"]), ["BTC", "ETH"])
        with self.assertRaises(ValueError):
            coins([])
        with self.assertRaises(ValueError):
            coins(["BTC", "btc"])

    def test_seed_contains_every_causal_input_without_credentials(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        client = PublicClient()
        path, manifest = prepare_observation(
            load(), ["AAA"], client, folder.name,
            now_ms=1_800_000_000_000, session="unit-test",
        )
        raw = (path / "seed.json").read_bytes()
        seed = json.loads(raw)
        self.assertEqual(seed["captured_ms"], 1_800_000_000_000)
        self.assertEqual(set(seed["markets"]["AAA"]["candles"]), {"1m", "15m", "1d"})
        self.assertEqual(manifest["seed_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(manifest["fee_assumption"]["source"], "configured_ceiling_without_credentials")
        self.assertEqual(
            [call[2] for call in client.calls if call[0] == "candles"],
            ["1m", "15m", "1d"],
        )

    def test_close_finalizes_an_immutable_replayable_capture(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path, _ = prepare_observation(
            load(), ["AAA"], PublicClient(), folder.name,
            now_ms=1_800_000_000_000, session="finalized-test",
        )
        capture = Capture(path, load(), ["AAA"])
        capture.write(received_ms=1_800_000_000_001, event="CONNECTED")
        capture.write(
            received_ms=1_800_000_000_002,
            raw=json.dumps({"response_type": "PING"}),
        )
        capture.close()
        observation = Observation(path)
        self.assertEqual(observation.manifest["message_count"], 2)
        self.assertTrue(observation.connection_quality()["contiguous"])


if __name__ == "__main__":
    unittest.main()
