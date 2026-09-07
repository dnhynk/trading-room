import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from track_a_2.external import ExternalCapture, stream_fields
from track_a_2.observe import ArrivalClock, Capture, prepare_observation
from track_a_2.settings import load as load_a2
from track_c.learning.config import sources as c4_sources
from track_c_multivenue import POLICIES
from track_c_multivenue.compare import run
from track_c_multivenue.contract import COINS, research_contract, source_identity
from track_c_multivenue.input import Session, StudyTape
from track_c_multivenue.policies import membership, reasons


class PublicClient:
    def universe(self):
        contracts = []
        tickers = []
        for coin in COINS:
            contracts.append({
                "quote_currency": "KRW",
                "target_currency": coin,
                "trade_status": 1,
                "maintenance_status": 0,
                "order_types": ["limit", "market", "stop_limit"],
                "qty_unit": "0.001",
                "min_qty": "0.001",
                "min_order_amount": "5000",
                "max_qty": "1000000",
                "max_order_amount": "1000000000",
                "price_unit": "1",
            })
            tickers.append({
                "quote_currency": "KRW", "target_currency": coin,
                "last": "100",
            })
        return contracts, tickers

    def price_units(self, _coin):
        return [{"range_min": 0, "price_unit": 1}]

    def orderbook(self, coin):
        return {
            "quote_currency": "KRW", "target_currency": coin,
            "bids": [], "asks": [],
        }

    def candles(self, _coin, _interval, _size):
        return []


def external_message(venue, coin, channel, at):
    if channel == "ORDERBOOK":
        return {
            "type": "orderbook",
            "code": "KRW-" + coin,
            "timestamp": at * 1000 if venue == "bithumb" else at,
            "orderbook_units": [{
                "bid_price": 99,
                "bid_size": 100,
                "ask_price": 100,
                "ask_size": 100,
            }],
        }
    return {
        "type": "trade",
        "code": "KRW-" + coin,
        "timestamp": at,
        "trade_timestamp": at,
        "trade_price": 100,
        "trade_volume": 1,
        "ask_bid": "BID",
        "sequential_id": str(at) + coin,
    }


def make_session(root, name="multi-session", base=None, *, wall_regression=False):
    base = time.time_ns() // 1_000_000 if base is None else base
    config = load_a2()
    folder, _ = prepare_observation(
        config, list(COINS), PublicClient(), root,
        now_ms=base, session=name,
    )
    clock = ArrivalClock()
    coinone = Capture(folder, config, list(COINS), clock=clock)
    external = ExternalCapture(
        folder, config, list(COINS), ["upbit", "bithumb"], clock=clock,
    )
    coinone.write(received_ms=base + 10, event="SOCKET_OPEN")
    external_start = base + 9 if wall_regression else base + 10
    for venue in ("upbit", "bithumb"):
        external.write(venue, received_ms=external_start, event="SOCKET_OPEN")
        external.write(
            venue, received_ms=external_start,
            event="SUBSCRIPTION_SENT",
        )
    coinone.write(
        received_ms=base + 10,
        raw=json.dumps({"response_type": "CONNECTED", "data": {}}),
    )
    for coin in COINS:
        for channel in ("ORDERBOOK", "TRADE"):
            coinone.write(
                received_ms=base + 11,
                raw=json.dumps({
                    "response_type": "SUBSCRIBED",
                    "channel": channel,
                    "data": {
                        "quote_currency": "KRW",
                        "target_currency": coin,
                    },
                }),
            )
    identity = 0
    for coin in COINS:
        for venue in ("upbit", "bithumb"):
            for channel in ("ORDERBOOK", "TRADE"):
                raw = json.dumps(external_message(
                    venue, coin, channel, base + 12,
                ), separators=(",", ":"))
                external.write(
                    venue, received_ms=base + 12, raw=raw,
                    stream=stream_fields(venue, json.loads(raw)),
                )
        identity += 2
        common = {
            "quote_currency": "KRW",
            "target_currency": coin,
            "timestamp": base + 12,
        }
        coinone.write(
            received_ms=base + 12,
            raw=json.dumps({
                "response_type": "DATA", "channel": "ORDERBOOK",
                "data": {
                    **common, "id": str(identity - 1),
                    "bids": [{"price": "99", "qty": "100"}],
                    "asks": [{"price": "100", "qty": "100"}],
                },
            }, separators=(",", ":")),
        )
        coinone.write(
            received_ms=base + 12,
            raw=json.dumps({
                "response_type": "DATA", "channel": "TRADE",
                "data": {
                    **common, "id": str(identity), "price": "100",
                    "qty": "1", "is_seller_maker": True,
                },
            }, separators=(",", ":")),
        )
    coinone.write(received_ms=base + 1013, event="COMPLETED")
    for venue in ("upbit", "bithumb"):
        external.write(venue, received_ms=base + 1013, event="COMPLETED")
    coinone.close()
    external.close()
    return folder


class MultiVenueInputTests(unittest.TestCase):
    def test_adapter_preserves_raw_and_global_same_millisecond_sequence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = make_session(folder)
            session = Session(path)
            events = list(session.events())
            self.assertEqual(
                [row["sequence"] for row in events],
                list(range(1, len(events) + 1)),
            )
            same_ms = [row for row in events if row["received_ms"] == session.captured_ms + 12]
            self.assertGreater(len({row["venue"] for row in same_ms}), 1)
            self.assertEqual(
                same_ms,
                sorted(same_ms, key=lambda row: row["sequence"]),
            )
            raw = next(row for row in events if row["kind"] == "data" and row["venue"] == "upbit")
            self.assertEqual(json.loads(raw["raw"]), raw["message"])
            self.assertTrue(session.audit()["safe_for_research"])

    def test_wall_clock_regression_is_visible_and_blocks_timed_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            path = make_session(folder, wall_regression=True)
            audit = Session(path).audit()
            self.assertGreater(audit["wall_clock_regressions"], 0)
            self.assertFalse(audit["safe_for_research"])

    def test_session_boundaries_are_sorted_censored_and_never_bridged(self):
        with tempfile.TemporaryDirectory() as folder:
            first = make_session(folder, "session-first")
            completed = Session(first).completed_ms
            second = make_session(
                folder, "session-second", base=completed + 100,
            )
            audit = StudyTape([second, first]).audit()
            self.assertTrue(audit["safe_for_research"])
            self.assertEqual(
                [row["session_id"] for row in audit["sessions"]],
                ["session-first", "session-second"],
            )
            self.assertEqual(audit["boundaries"][0]["inventory"], "censor")
            self.assertFalse(audit["boundaries"][0]["bridged"])
            self.assertEqual(audit["boundaries"][0]["gap_ms"], 100)


class MultiVenuePolicyTests(unittest.TestCase):
    def test_policy_matrix_is_predeclared_and_p3_uses_only_known_information(self):
        values = membership(
            a2_trigger=True, c_trigger=False,
            external_eligible=True, a2_known_recent=True,
        )
        self.assertEqual(tuple(values), POLICIES)
        self.assertEqual(values, {
            "P0": True, "P1": True, "P2": False, "P3": False,
        })
        values = membership(
            a2_trigger=False, c_trigger=True,
            external_eligible=True, a2_known_recent=False,
        )
        self.assertEqual(values, {
            "P0": False, "P1": False, "P2": True, "P3": False,
        })
        self.assertEqual(
            reasons(
                a2_trigger=False, c_trigger=True,
                external_eligible=True, a2_known_recent=False,
            )["P3"],
            "no_causally_known_a2_deceleration",
        )

    def test_annex_does_not_enter_frozen_c4_source_identity(self):
        identity = source_identity()
        self.assertIn("track_a_2/market/units.py", identity)
        self.assertIn("track_c/learning/features.py", identity)
        self.assertFalse(any(
            name.startswith("track_c_multivenue/") for name in c4_sources()
        ))
        contract = research_contract(load_a2())
        self.assertFalse(contract["orders_enabled"])
        self.assertEqual(contract["live_promotion"], "forbidden")
        self.assertIn("dip_min_atr", contract["a2_signal"]["parameters"])

    def test_policy_comparison_uses_one_decision_frame_and_one_common_action(self):
        from track_c_multivenue.compare import evaluate

        class Reference:
            def quote(self, _row):
                return None

        class FakeCMarket:
            def __init__(self, coin, _cfg):
                self.coin = coin
                self.reference = Reference()
                self.have_book = False
                self.have_sell = False
                self.emitted = False

            def feed(self, channel, _data, now):
                if channel == "ORDERBOOK":
                    self.have_book = True
                    return {
                        "kind": "book", "t": now,
                        "bids": [(99.0, 100.0)], "asks": [(100.0, 100.0)],
                    }
                self.have_sell = True
                return {
                    "kind": "trade", "t": now, "exchange_t": now,
                    "price": 99.0, "qty": 1.0, "buy": False,
                }

            def snapshot(self, now, contract, units):
                if not self.have_book:
                    return None
                new = self.coin == "ETH" and self.have_sell and not self.emitted
                if new:
                    self.emitted = True
                return {
                    "coin": self.coin, "t_ms": now, "book_ms": now,
                    "bid": 99.0, "ask": 100.0, "tick": 1.0,
                    "bids": [(99.0, 100.0)], "asks": [(100.0, 100.0)],
                    "reference": {
                        "ready": True, "reason": None, "fair": 102.0,
                        "lower": 101.0, "upper": 103.0, "dev_ticks": 2.5,
                        "disagreement_ticks": 0.0, "m10": 0.0, "m30": 0.0,
                        "regime_id": 0, "t_ms": now,
                    },
                    "risk": {
                        "ready": True, "reason": None, "n_returns": 30,
                        "distance_price": 2.0,
                    },
                    "pressure": 2.0, "flow": -1.0,
                    "buy_volume": 10.0, "sell_volume": 10.0,
                    "entry_fresh": True, "entry_eligible": True,
                    "episode_id": "episode" if self.have_sell else None,
                    "new_episode": new, "contract": contract, "units": units,
                }

        class FakeA2Market:
            def __init__(self, coin, *_args, **_kwargs):
                self.coin = coin
                self.sent = False

            def feed(self, channel, _data, _now):
                if self.coin == "ETH" and channel == "ORDERBOOK" and not self.sent:
                    self.sent = True
                    return [{"sig": "DIP_SLOWING", "src": "v", "t": 1}]
                return []

        def action(state, _cfg, candidate_id, _cash):
            return ({
                "id": "0:minimum", "offset": 0, "size": "minimum",
                "price": 99.0, "qty": 1.0, "stop": 97.0,
                "stop_limit": 96.0, "minimum": 5.0, "qty_step": 0.001,
                "tick": 1.0, "ttl_s": 8, "hold_s": 180,
                "notional": 99.0, "nominal_loss": 3.0,
                "episode_id": candidate_id, "coin": state["coin"],
                "t_ms": state["t_ms"], "reference": 102.0,
                "dev_ticks": 2.5, "spread_ticks": 1.0,
                "pressure": 2.0, "liquidation_vwap": 99.0,
                "key": "ETH:deep:sell:local",
            }, None)

        with tempfile.TemporaryDirectory() as folder:
            session = make_session(folder)
            study = StudyTape([session])
            with patch("track_c_multivenue.compare.CMarket", FakeCMarket), patch(
                "track_c_multivenue.compare.A2Market", FakeA2Market,
            ), patch("track_c_multivenue.compare._shared_action", side_effect=action):
                report, candidates, outcomes = evaluate(study, load_a2())
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["coin"], "ETH")
            self.assertTrue(all(candidates[0]["selected_by_policy"].values()))
            self.assertEqual(len(outcomes), 4)
            for name in POLICIES:
                self.assertEqual(report["policies"][name]["common_attempts"], 1)

    def test_short_complete_session_produces_research_receipts_not_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            session = make_session(folder)
            output = Path(folder) / "result"
            report = run([session], output)
            self.assertEqual(report["verdict"], "RESEARCH_ONLY")
            self.assertFalse(report["orders_enabled"])
            self.assertEqual(report["exchange_orders"], 0)
            self.assertTrue((output / "frozen-before-evaluation.json").is_file())
            self.assertTrue((output / "candidate-events.jsonl.gz").is_file())
            self.assertTrue((output / "attempt-outcomes.jsonl.gz").is_file())
            self.assertTrue((output / "report.json").is_file())

    def test_negative_cash_is_a_research_outcome_not_a_numeric_error(self):
        from track_c_multivenue.compare import _policy_report

        candidates = [{
            "selected_by_policy": {name: name == "P0" for name in POLICIES},
            "attempted_by_policy": {name: name == "P0" for name in POLICIES},
        }]
        outcomes = [{
            "policy": "P0", "filled_qty": 1.0, "censored": False,
            "cash_net_krw": -12.5, "net_bp": -4.0,
        }]
        report = _policy_report(candidates, outcomes, "P0")
        self.assertEqual(report["mean_cash_net_krw_per_attempt"], -12.5)
        self.assertEqual(report["mean_cash_net_bp_per_attempt"], -4.0)

    def test_bad_quality_still_preserves_a_failure_result(self):
        with tempfile.TemporaryDirectory() as folder:
            session = make_session(folder, wall_regression=True)
            output = Path(folder) / "failed-result"
            with self.assertRaisesRegex(ValueError, "research quality"):
                run([session], output)
            failure = json.loads((output / "failure.json").read_text())
            self.assertEqual(failure["status"], "RESEARCH_ERROR")
            self.assertFalse(failure["orders_enabled"])
            self.assertEqual(
                failure["opening_receipt"], "frozen-before-evaluation.json",
            )

    def test_repository_output_is_rejected_before_creation(self):
        target = Path(__file__).resolve().parents[2] / "forbidden-research-output"
        self.assertFalse(target.exists())
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            run(["unused"], target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
