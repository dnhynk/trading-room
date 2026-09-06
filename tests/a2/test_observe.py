import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from track_a_2.observe import Capture, coins, prepare_observation, record_public
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
        capture.write(received_ms=1_800_000_000_001, event="SOCKET_OPEN")
        capture.write(
            received_ms=1_800_000_000_002,
            raw=json.dumps({"response_type": "CONNECTED", "data": {"session_id": "test"}}),
        )
        for offset, channel in enumerate(("ORDERBOOK", "TRADE"), 3):
            capture.write(
                received_ms=1_800_000_000_000 + offset,
                raw=json.dumps({
                    "response_type": "SUBSCRIBED", "channel": channel,
                    "data": {"quote_currency": "KRW", "target_currency": "AAA"},
                }),
            )
        for offset, channel in enumerate(("ORDERBOOK", "TRADE"), 5):
            capture.write(
                received_ms=1_800_000_000_000 + offset,
                raw=json.dumps({
                    "response_type": "DATA", "channel": channel,
                    "data": {"quote_currency": "KRW", "target_currency": "AAA"},
                }),
            )
        capture.close()
        observation = Observation(path)
        self.assertEqual(observation.manifest["message_count"], 6)
        self.assertTrue(observation.connection_quality()["contiguous"])

    def test_connection_quality_rejects_subscription_error_or_missing_data(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path, _ = prepare_observation(
            load(), ["AAA"], PublicClient(), folder.name,
            now_ms=1_800_000_000_000, session="bad-quality-test",
        )
        capture = Capture(path, load(), ["AAA"])
        capture.write(received_ms=1_800_000_000_001, event="SOCKET_OPEN")
        capture.write(
            received_ms=1_800_000_000_002,
            raw=json.dumps({"response_type": "CONNECTED", "data": {}}),
        )
        capture.write(
            received_ms=1_800_000_000_003,
            raw=json.dumps({"response_type": "ERROR", "error_code": 160012}),
        )
        capture.close()
        quality = Observation(path).connection_quality()
        self.assertFalse(quality["contiguous"])
        self.assertFalse(quality["coverage_complete"])
        self.assertEqual(quality["errors"], 1)

    def test_recorder_sends_json_ping_and_requires_pong(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path, _ = prepare_observation(
            load(), ["AAA"], PublicClient(), folder.name,
            now_ms=time.time_ns() // 1_000_000, session="ping-test",
        )
        capture = Capture(path, load(), ["AAA"])

        class Socket:
            def __init__(self):
                self.sent = []
                self.pongs = 0
                self.messages = [
                    {"response_type": "CONNECTED", "data": {}},
                    {"response_type": "SUBSCRIBED", "channel": "ORDERBOOK", "data": {"target_currency": "AAA"}},
                    {"response_type": "SUBSCRIBED", "channel": "TRADE", "data": {"target_currency": "AAA"}},
                    {"response_type": "DATA", "channel": "ORDERBOOK", "data": {"target_currency": "AAA"}},
                    {"response_type": "DATA", "channel": "TRADE", "data": {"target_currency": "AAA"}},
                ]

            async def send(self, value):
                self.sent.append(value)

            async def recv(self):
                if self.messages:
                    return json.dumps(self.messages.pop(0))
                ping_count = sum('"PING"' in value for value in self.sent)
                if ping_count > self.pongs:
                    self.pongs += 1
                    return '{"response_type":"PONG"}'
                await asyncio.sleep(1)

        socket = Socket()

        class Connection:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, *_):
                return False

        asyncio.run(record_public(
            capture, 0.2, connector=lambda *_args, **_kwargs: Connection(),
            ping_interval_s=0.01, pong_timeout_s=0.1,
            first_data_timeout_s=0.1,
        ))
        capture.close()
        self.assertTrue(
            any('{"request_type":"PING"}' == value for value in socket.sent),
            socket.sent,
        )
        envelopes = list(Observation(path).envelopes())
        self.assertTrue(any(row.get("event") == "PING_SENT" for row in envelopes))
        self.assertTrue(any('"PONG"' in row.get("raw", "") for row in envelopes))


if __name__ == "__main__":
    unittest.main()
