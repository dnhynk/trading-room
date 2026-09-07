import asyncio
import json
import tempfile
import unittest

from track_a_2.external import ExternalCapture, record_external, stream_fields
from track_a_2.observe import ArrivalClock, Capture, prepare_observation
from track_a_2.replay.loader import Observation
from track_a_2.settings import load


class PublicClient:
    def universe(self):
        return (
            [dict(target_currency="AAA")],
            [dict(target_currency="AAA")],
        )

    def price_units(self, _coin):
        return [{"range_min": "0", "price_unit": "0.01"}]

    def orderbook(self, coin):
        return {"target_currency": coin, "bids": [], "asks": []}

    def candles(self, _coin, _interval, _size):
        return []


def complete_coinone(capture, base):
    capture.write(received_ms=base + 1, event="SOCKET_OPEN")
    capture.write(
        received_ms=base + 2,
        raw=json.dumps({"response_type": "CONNECTED", "data": {}}),
    )
    for offset, channel in enumerate(("ORDERBOOK", "TRADE"), 3):
        capture.write(
            received_ms=base + offset,
            raw=json.dumps({
                "response_type": "SUBSCRIBED", "channel": channel,
                "data": {"target_currency": "AAA"},
            }),
        )
    for offset, channel in enumerate(("ORDERBOOK", "TRADE"), 5):
        capture.write(
            received_ms=base + offset,
            raw=json.dumps({
                "response_type": "DATA", "channel": channel,
                "data": {"target_currency": "AAA"},
            }),
        )


class ExternalObserveTests(unittest.TestCase):
    def test_exchange_timestamps_preserve_venue_units(self):
        self.assertEqual(
            stream_fields("upbit", {
                "type": "orderbook", "code": "KRW-AAA",
                "timestamp": 1_800_000_000_123,
            })["exchange_ms"],
            1_800_000_000_123,
        )
        self.assertEqual(
            stream_fields("bithumb", {
                "type": "orderbook", "code": "KRW-AAA",
                "timestamp": 1_800_000_000_123_456,
            })["exchange_ms"],
            1_800_000_000_123,
        )
        self.assertEqual(
            stream_fields("bithumb", {
                "type": "trade", "code": "KRW-AAA",
                "trade_timestamp": 1_800_000_000_124,
            })["exchange_ms"],
            1_800_000_000_124,
        )

    def test_multivenue_capture_is_raw_complete_and_causally_ordered(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = 1_800_000_000_000
        folder, _ = prepare_observation(
            load(), ["AAA"], PublicClient(), temporary.name,
            now_ms=base, session="external-test",
        )
        clock = ArrivalClock()
        coinone = Capture(folder, load(), ["AAA"], clock=clock)
        external = ExternalCapture(
            folder, load(), ["AAA"], ["upbit"], clock=clock,
        )
        complete_coinone(coinone, base)

        class Socket:
            def __init__(self):
                self.sent = []
                self.messages = [
                    {
                        "type": "orderbook", "code": "KRW-AAA",
                        "timestamp": base + 10,
                        "orderbook_units": [{
                            "bid_price": 99, "bid_size": 1,
                            "ask_price": 100, "ask_size": 1,
                        }],
                    },
                    {
                        "type": "trade", "code": "KRW-AAA",
                        "trade_timestamp": base + 11,
                        "trade_price": 100, "trade_volume": 1,
                    },
                ]

            async def send(self, value):
                self.sent.append(value)

            async def recv(self):
                if self.messages:
                    return json.dumps(self.messages.pop(0), separators=(",", ":"))
                await asyncio.sleep(1)

            async def ping(self):
                future = asyncio.get_running_loop().create_future()
                future.set_result(None)
                return future

        socket = Socket()

        class Connection:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, *_):
                return False

        asyncio.run(record_external(
            external, "upbit", 0.08,
            connector=lambda *_args, **_kwargs: Connection(),
            ping_interval_s=0.01,
            pong_timeout_s=0.05,
            first_data_timeout_s=0.05,
        ))
        coinone.close()
        external.close()

        observation = Observation(folder)
        rows = list(observation.external_envelopes())
        raw_rows = [row for row in rows if "raw" in row]
        self.assertEqual(len(raw_rows), 2)
        self.assertEqual(json.loads(raw_rows[0]["raw"])["type"], "orderbook")
        self.assertTrue(any(row.get("event") == "CONTROL_PING_SENT" for row in rows))
        self.assertTrue(any(row.get("event") == "CONTROL_PONG" for row in rows))
        quality = observation.multivenue_quality()
        self.assertTrue(quality["external"]["contiguous"], quality)
        self.assertTrue(quality["causal_order"], quality)
        self.assertTrue(quality["contiguous"], quality)
        request = json.loads(socket.sent[0])
        self.assertEqual(
            {row["type"] for row in request if "type" in row},
            {"orderbook", "trade"},
        )

    def test_missing_external_stream_is_preserved_as_bad_quality(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = 1_800_000_000_000
        folder, _ = prepare_observation(
            load(), ["AAA"], PublicClient(), temporary.name,
            now_ms=base, session="external-incomplete",
        )
        clock = ArrivalClock()
        coinone = Capture(folder, load(), ["AAA"], clock=clock)
        external = ExternalCapture(
            folder, load(), ["AAA"], ["bithumb"], clock=clock,
        )
        complete_coinone(coinone, base)
        external.write("bithumb", received_ms=base + 7, event="SOCKET_OPEN")
        external.write("bithumb", received_ms=base + 8, event="SUBSCRIPTION_SENT")
        raw = json.dumps({
            "type": "orderbook", "code": "KRW-AAA",
            "timestamp": (base + 9) * 1000,
        })
        external.write(
            "bithumb", received_ms=base + 9, raw=raw,
            stream=stream_fields("bithumb", json.loads(raw)),
        )
        external.write("bithumb", received_ms=base + 10, event="COMPLETED")
        coinone.close()
        external.close()
        quality = Observation(folder).multivenue_quality()
        self.assertFalse(quality["external"]["contiguous"])
        self.assertFalse(
            quality["external"]["venues"]["bithumb"]["coverage_complete"]
        )


if __name__ == "__main__":
    unittest.main()
