import gzip
import hashlib
import io
import json
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from track_a_2.replay.__main__ import main as replay_main, signed_decimal
from track_a_2.replay.loader import Observation
from track_a_2.replay.engine import run_observation
from track_a_2.replay.sim import ReplayClock, SimClient
from track_a_2.settings import load
from track_c.execution.coinone import CoinoneError


CAPTURED = 1_800_000_000_000


def observation_files(path, messages, *, coin="AAA"):
    contract = dict(
        quote_currency="KRW", target_currency=coin, trade_status=1,
        maintenance_status=0, order_types=["limit", "market", "stop_limit"],
        qty_unit="0.1", min_qty="0.1", max_qty="100000",
        min_order_amount="5000", max_order_amount="1000000000",
    )
    ticker = dict(
        quote_currency="KRW", target_currency=coin, quote_volume="10000000000",
        high="110", low="100", last="105",
        best_bids=[{"price": "104.9", "qty": "10000"}],
        best_asks=[{"price": "105", "qty": "10000"}],
    )
    orderbook = dict(
        quote_currency="KRW", target_currency=coin,
        bids=[{"price": "104.9", "qty": "10000"}],
        asks=[{"price": "105", "qty": "10000"}],
    )
    seed = dict(
        schema=1, captured_ms=CAPTURED,
        markets={coin: dict(
            contract=contract, ticker=ticker,
            units=[{"range_min": "0", "price_unit": "0.1"}],
            orderbook=orderbook,
            candles={"1m": [], "15m": [], "1d": []},
        )},
    )
    seed_raw = (json.dumps(seed, separators=(",", ":")) + "\n").encode()
    (path / "seed.json").write_bytes(seed_raw)
    manifest = dict(
        schema=1, track="A-2", format="coinone-public-v1",
        coins=[coin], seed_file="seed.json",
        seed_sha256=hashlib.sha256(seed_raw).hexdigest(),
        fee_assumption=dict(maker="0", taker="0", source="test"),
    )
    with gzip.open(path / "public.jsonl.gz", "wt", encoding="utf-8") as target:
        for row in messages:
            target.write(json.dumps(row) + "\n")
    manifest.update(
        completed_ms=CAPTURED + 1000,
        message_count=len(messages),
        public_sha256=hashlib.sha256((path / "public.jsonl.gz").read_bytes()).hexdigest(),
    )
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def order(cid, role, side, kind, qty, **fields):
    return dict(
        cid=cid, coin="AAA", role=role, side=side, type=kind,
        qty=str(qty), fee_rate="0.001", status="INTENT", filled="0",
        gross="0", fee="0", created=0, **fields,
    )


class ObservationTests(unittest.TestCase):
    def test_loader_verifies_seed_and_preserves_receive_order(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        seed_raw = b'{"captured_ms":1000,"markets":{"AAA":{}},"schema":1}\n'
        (path / "seed.json").write_bytes(seed_raw)
        manifest = dict(
            schema=1, track="A-2", format="coinone-public-v1",
            coins=["AAA"], seed_file="seed.json",
            seed_sha256=hashlib.sha256(seed_raw).hexdigest(),
            fee_assumption=dict(maker="0", taker="0", source="test"),
        )
        messages = [
            dict(received_ms=1000, event="SOCKET_OPEN", fields={}),
            dict(received_ms=1001, raw=json.dumps(dict(response_type="CONNECTED", data={}))),
            dict(received_ms=1002, raw=json.dumps(dict(
                response_type="SUBSCRIBED", channel="ORDERBOOK",
                data={"target_currency": "AAA"},
            ))),
            dict(received_ms=1003, raw=json.dumps(dict(
                response_type="SUBSCRIBED", channel="TRADE",
                data={"target_currency": "AAA"},
            ))),
            dict(received_ms=1004, raw=json.dumps(dict(
                response_type="DATA", channel="ORDERBOOK",
                data={"target_currency": "AAA"},
            ))),
            dict(received_ms=1005, raw=json.dumps(dict(
                response_type="DATA", channel="TRADE",
                data={"target_currency": "AAA"},
            ))),
        ]
        with gzip.open(path / "public.jsonl.gz", "wt", encoding="utf-8") as target:
            for row in messages:
                target.write(json.dumps(row) + "\n")
        manifest.update(
            completed_ms=2000,
            message_count=len(messages),
            public_sha256=hashlib.sha256((path / "public.jsonl.gz").read_bytes()).hexdigest(),
        )
        (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        observation = Observation(path)
        self.assertEqual(len(list(observation.rows())), 5)
        quality = observation.connection_quality()
        self.assertTrue(quality["contiguous"])
        self.assertTrue(quality["coverage_complete"])
        self.assertEqual(quality["data_streams"], 2)
        self.assertEqual(len(observation.data_digest()), 64)
        (path / "seed.json").write_bytes(seed_raw + b" ")
        with self.assertRaisesRegex(ValueError, "identity"):
            Observation(path)

    def test_engine_uses_production_runtime_without_inventing_a_signal(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        data = dict(
            quote_currency="KRW", target_currency="AAA", timestamp=CAPTURED + 100,
            id=1,
            bids=[{"price": "104.9", "qty": "10000"}],
            asks=[{"price": "105", "qty": "10000"}],
        )
        observation_files(path, [dict(
            received_ms=CAPTURED + 100,
            raw=json.dumps(dict(response_type="DATA", channel="ORDERBOOK", data=data)),
        )])
        result = run_observation(
            path, {**load(), "universe": ["AAA"]}, capital="100000"
        )
        self.assertEqual(result["selected"], ["AAA"])
        self.assertEqual(result["messages"], 1)
        self.assertEqual(result["fills"], 0)
        self.assertEqual(result["net_pnl_krw"], "0")
        self.assertIsNone(result["halt"])

    def test_negative_pnl_is_preserved_as_research_and_rejected_approval(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        parent = Path(folder.name)
        observation_path = parent / "observation"
        observation_path.mkdir()
        observation_files(observation_path, [])
        root = parent / "trading-room"
        config_path = root / "track_a_2" / "config.json"
        config_path.parent.mkdir(parents=True)
        config = {**load(), "universe": ["AAA"], "basket_size": 1, "max_open_books": 1}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        digest = Observation(observation_path).data_digest()

        def runner_factory():
            values = iter(("-1", "5", "-2"))

            def runner(*_args, **_kwargs):
                return dict(
                    net_pnl_krw=next(values), campaigns=1, halt=None,
                    data_digest=digest,
                )

            return runner

        with patch(
            "track_a_2.replay.__main__.evaluation_source_digest",
            return_value="a" * 64,
        ), redirect_stdout(io.StringIO()):
            replay_main([
                str(observation_path), "--config", str(config_path),
                "--approval-id", "a2-eval-negative-research",
            ], root=root, runner=runner_factory())
        research = json.loads((
            parent / "trading-room-state" / "track-a-2" / "evaluations"
            / "a2-eval-negative-research.json"
        ).read_text())
        self.assertEqual(research["result"], "RESEARCH_ONLY")
        self.assertEqual(research["metrics"]["main"]["net_pnl_krw"], "-1")

        with patch(
            "track_a_2.replay.__main__.evaluation_source_digest",
            return_value="a" * 64,
        ), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            replay_main([
                str(observation_path), "--config", str(config_path),
                "--approval-id", "a2-eval-negative-rejected", "--approve",
            ], root=root, runner=runner_factory())
        rejected = json.loads((
            parent / "trading-room-state" / "track-a-2" / "evaluations"
            / "a2-eval-negative-rejected.json"
        ).read_text())
        self.assertEqual(rejected["result"], "REJECTED")
        self.assertIn("main_not_profitable", rejected["rejection_reasons"])
        self.assertEqual(signed_decimal("-0.01"), D("-0.01"))


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.clock = ReplayClock()
        self.client = SimClient(
            "100000", {"AAA": {"maker": "0.001", "taker": "0.002"}},
            clock=self.clock, latency_ms=100, depth_fraction="0.5",
        )
        self.book = dict(
            bids=[{"price": "100", "qty": "20"}],
            asks=[{"price": "100.1", "qty": "20"}],
        )
        self.client.set_book("AAA", self.book)

    def test_maker_queue_and_fee_are_reflected_in_cumulative_detail(self):
        row = order("ta2-buy-replay01", "buy", "BUY", "LIMIT", 10, price="100")
        self.client.submit(row)
        self.clock.set_ms(100)
        self.client.on_trade("AAA", dict(price="100", qty="25", is_seller_maker=False))
        detail = self.client.detail("AAA", row["cid"])
        self.assertEqual(detail["executed_qty"], "5")
        self.assertEqual(detail["fee"], "0.500")
        self.assertEqual(self.client.assets["AAA"], 5)

    def test_market_sell_limit_cancels_remainder_below_its_floor(self):
        self.client.assets["AAA"] = 10
        row = order(
            "ta2-trim-replay01", "trim", "SELL", "MARKET", 10,
            limit_price="100",
        )
        self.client.submit(row)
        self.clock.set_ms(100)
        self.client.on_book("AAA", self.book)
        detail = self.client.detail("AAA", row["cid"])
        self.assertEqual(detail["executed_qty"], "10")
        self.assertEqual(detail["status"], "FILLED")
        self.assertEqual(self.client.cash, 100000 + 1000 - 2)
        self.assertEqual(self.client.assets["AAA"], 0)

    def test_cancel_is_delayed_and_can_race_with_a_fill(self):
        row = order("ta2-buy-replay02", "buy", "BUY", "LIMIT", 10, price="100")
        self.client.submit(row)
        self.client.cancel("AAA", row["cid"])
        self.clock.set_ms(100)
        self.client.on_trade("AAA", dict(price="99", qty="1", is_seller_maker=False))
        self.assertEqual(self.client.detail("AAA", row["cid"])["status"], "FILLED")

    def test_post_only_order_that_crosses_after_latency_is_rejected(self):
        row = order("ta2-buy-replay03", "buy", "BUY", "LIMIT", 10, price="100.2")
        self.client.submit(row)
        self.clock.set_ms(100)
        self.client.advance()
        detail = self.client.detail("AAA", row["cid"])
        self.assertEqual(detail["status"], "REJECTED")
        self.assertEqual(detail["executed_qty"], "0")

    def test_accepted_post_only_order_fills_when_market_crosses_later(self):
        row = order("ta2-buy-replay04", "buy", "BUY", "LIMIT", 10, price="100")
        self.client.submit(row)
        with self.assertRaisesRegex(CoinoneError, "transport pending"):
            self.client.detail("AAA", row["cid"])
        self.clock.set_ms(100)
        self.client.advance()
        self.assertEqual(self.client.detail("AAA", row["cid"])["status"], "LIVE")
        crossed = dict(
            bids=[{"price": "99.9", "qty": "20"}],
            asks=[{"price": "100", "qty": "20"}],
        )
        self.clock.set_ms(200)
        self.client.on_book("AAA", crossed)
        self.assertEqual(self.client.detail("AAA", row["cid"])["status"], "FILLED")


if __name__ == "__main__":
    unittest.main()
