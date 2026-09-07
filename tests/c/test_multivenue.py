import json
from copy import deepcopy
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
from track_c_multivenue.contract import (
    COINS, c_config, research_contract, source_identity,
)
from track_c_multivenue.diagnostics import (
    FillMarkoutProbe, local_minimum_action, reference_group,
)
from track_c_multivenue.input import Session, StudyTape
from track_c_multivenue.policies import membership, reasons
from track_c_multivenue.registration import create_registration


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


def external_message(venue, coin, channel, at, mid=99.5):
    if channel == "ORDERBOOK":
        return {
            "type": "orderbook",
            "code": "KRW-" + coin,
            "timestamp": at * 1000 if venue == "bithumb" else at,
            "orderbook_units": [{
                "bid_price": mid - 0.01,
                "bid_size": 1000,
                "ask_price": mid + 0.01,
                "ask_size": 1000,
            }],
        }
    return {
        "type": "trade",
        "code": "KRW-" + coin,
        "timestamp": at,
        "trade_timestamp": at,
        "trade_price": mid,
        "trade_volume": 1,
        "ask_bid": "BID",
        "sequential_id": str(at) + coin,
    }


def make_session(
    root, name="multi-session", base=None, *, wall_regression=False,
    submillisecond=False,
):
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
    arrival_offset_ns = 0

    def arrival_time():
        nonlocal arrival_offset_ns
        if not submillisecond:
            return {"received_ms": base + 12}
        arrival_offset_ns += 10_000
        return {"received_ns": (base + 12) * 1_000_000 + arrival_offset_ns}

    for coin in COINS:
        for venue in ("upbit", "bithumb"):
            for channel in ("ORDERBOOK", "TRADE"):
                raw = json.dumps(external_message(
                    venue, coin, channel, base + 12,
                ), separators=(",", ":"))
                external.write(
                    venue, **arrival_time(), raw=raw,
                    stream=stream_fields(venue, json.loads(raw)),
                )
        identity += 2
        common = {
            "quote_currency": "KRW",
            "target_currency": coin,
            "timestamp": base + 12,
        }
        coinone.write(
            **arrival_time(),
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
            **arrival_time(),
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


class WarmPublicClient(PublicClient):
    def __init__(self, base):
        self.base = base

    def universe(self):
        contracts, tickers = super().universe()
        for contract in contracts:
            contract["qty_unit"] = "0.001"
            contract["price_unit"] = "0.01"
        return contracts, tickers

    def price_units(self, _coin):
        return [{"range_min": 0, "price_unit": 0.01}]

    def candles(self, _coin, interval, _size):
        if interval != "1m":
            return []
        return [{
            "timestamp": self.base - (120 - index) * 60_000,
            "open": "100", "high": "100.05", "low": "99.95",
            "close": "100", "target_volume": "1000",
        } for index in range(120)]


def make_warm_integration_session(root, name="warm-integration"):
    """Actual adapters and feature engines, with a deterministic dip/recovery."""
    base = time.time_ns() // 1_000_000 + 2_000
    config = deepcopy(load_a2())
    config["signal"].update({
        "vol_hl": 1, "v_hl": 1, "a_lag": 1, "hold_s": 1,
        "v_fast": 0.5, "v_slow": 0.5,
        "c1_on": 0, "s8_dip": 0, "s8_pop": 0,
    })
    folder, _ = prepare_observation(
        config, list(COINS), WarmPublicClient(base), root,
        now_ms=base, session=name,
    )
    clock = ArrivalClock()
    coinone = Capture(folder, config, list(COINS), clock=clock)
    external = ExternalCapture(
        folder, config, list(COINS), ["upbit", "bithumb"], clock=clock,
    )
    coinone.write(received_ms=base + 10, event="SOCKET_OPEN")
    for venue in ("upbit", "bithumb"):
        external.write(venue, received_ms=base + 10, event="SOCKET_OPEN")
        external.write(venue, received_ms=base + 10, event="SUBSCRIPTION_SENT")
    coinone.write(
        received_ms=base + 10,
        raw=json.dumps({"response_type": "CONNECTED", "data": {}}),
    )
    for coin in COINS:
        for channel in ("ORDERBOOK", "TRADE"):
            coinone.write(
                received_ms=base + 11,
                raw=json.dumps({
                    "response_type": "SUBSCRIBED", "channel": channel,
                    "data": {"quote_currency": "KRW", "target_currency": coin},
                }),
            )
    book_ids = {coin: 0 for coin in COINS}
    trade_ids = {coin: 0 for coin in COINS}

    def external_row(venue, coin, channel, at, mid):
        raw = json.dumps(
            external_message(venue, coin, channel, at, mid),
            separators=(",", ":"),
        )
        external.write(
            venue, received_ms=at, raw=raw,
            stream=stream_fields(venue, json.loads(raw)),
        )

    def local_book(coin, at, mid):
        book_ids[coin] += 1
        bids = [{
            "price": f"{mid - 0.01 - level * 0.01:.2f}", "qty": "1000",
        } for level in range(5)]
        asks = [{
            "price": f"{mid + 0.01 + level * 0.01:.2f}", "qty": "1000",
        } for level in range(5)]
        coinone.write(
            received_ms=at,
            raw=json.dumps({
                "response_type": "DATA", "channel": "ORDERBOOK",
                "data": {
                    "quote_currency": "KRW", "target_currency": coin,
                    "timestamp": at, "id": str(book_ids[coin]),
                    "bids": bids, "asks": asks,
                },
            }, separators=(",", ":")),
        )

    def local_trade(coin, at, price, qty, buy):
        trade_ids[coin] += 1
        coinone.write(
            received_ms=at,
            raw=json.dumps({
                "response_type": "DATA", "channel": "TRADE",
                "data": {
                    "quote_currency": "KRW", "target_currency": coin,
                    "timestamp": at, "id": str(trade_ids[coin]),
                    "price": f"{price:.2f}", "qty": str(qty),
                    "is_seller_maker": buy,
                },
            }, separators=(",", ":")),
        )

    for second in range(331):
        at = base + 1_000 + second * 1_000
        if second and second % 60 == 0:
            coinone.write(received_ms=at, event="PING_SENT")
            coinone.write(
                received_ms=at,
                raw=json.dumps({"response_type": "PONG", "data": {}}),
            )
        if second and second % 20 == 0:
            for venue in ("upbit", "bithumb"):
                external.write(venue, received_ms=at, event="CONTROL_PING_SENT")
                external.write(venue, received_ms=at, event="CONTROL_PONG")
        for coin in COINS:
            local_mid = (
                99.5 if coin == "ETH" and 305 <= second < 315 else 100.0
            )
            for venue in ("upbit", "bithumb"):
                external_row(venue, coin, "ORDERBOOK", at, 100.0)
                if second == 0:
                    external_row(venue, coin, "TRADE", at, 100.0)
            local_book(coin, at, local_mid)
            if second == 0:
                local_trade(coin, at, local_mid - 0.01, 1, True)
            if coin == "ETH" and second >= 305:
                bid = local_mid - 0.01
                local_trade(coin, at, bid, 2_000, True)
                local_trade(coin, at, bid, 1_020, False)
    completed = base + 332_000
    coinone.write(received_ms=completed, event="COMPLETED")
    for venue in ("upbit", "bithumb"):
        external.write(venue, received_ms=completed, event="COMPLETED")
    coinone.close()
    external.close()
    return folder, config


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

    def test_external_unavailable_fill_markout_is_diagnostic_not_an_order(self):
        cfg = c_config()
        state = {
            "coin": "ETH", "t_ms": 1_001, "book_ms": 1_000,
            "bid": 100.0, "ask": 101.0, "tick": 1.0,
            "bids": [(100.0, 100.0), (99.0, 100.0)],
            "asks": [(101.0, 100.0)], "entry_fresh": True,
            "reference": {"ready": False, "reason": "reference_unavailable"},
            "contract": {
                "trade_status": 1, "maintenance_status": 0,
                "order_types": ["limit"], "qty_unit": "1",
                "min_qty": "1", "min_order_amount": "5000",
                "max_qty": "1000000", "max_order_amount": "1000000000",
            },
        }
        self.assertEqual(
            reference_group(state, cfg), "external_reference_unavailable",
        )
        action, reason = local_minimum_action(state, cfg, "candidate")
        self.assertIsNone(reason)
        self.assertEqual(action["qty"], 50.0)
        probe = FillMarkoutProbe(action, cfg, state, reference_group(state, cfg))
        probe.event({
            "kind": "trade", "t": 1_400, "price": 99.0,
            "qty": 100.0, "buy": False,
        })
        probe.observe(2_400, dict(state, t_ms=2_400, book_ms=2_400))
        result = probe.result(122_000)
        self.assertEqual(result["filled_qty"], 50.0)
        markout = result["fills"][0]["markouts"]["1000"]
        self.assertEqual(markout["status"], "valued")
        self.assertEqual(markout["markout_bp"], 0.0)
        self.assertFalse(result["orders_enabled"])

    def test_p0_is_selected_but_not_attempted_without_c_common_reference(self):
        from track_c_multivenue.compare import _shared_action

        cfg = c_config()
        selected = membership(
            a2_trigger=True, c_trigger=False,
            external_eligible=False, a2_known_recent=True,
        )
        self.assertTrue(selected["P0"])
        state = {
            "entry_fresh": True,
            "reference": {"ready": False, "reason": "reference_unavailable"},
            "risk": {"ready": True},
        }
        action, reason = _shared_action(state, cfg, "candidate", 300_000.0)
        self.assertIsNone(action)
        self.assertEqual(reason, "external_reference_unavailable")

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
        self.assertTrue(
            contract["policies"]["P0"]["external_reference_used_by_common_execution"]
        )
        self.assertEqual(
            contract["decision_clock"]["effective_time"],
            "end_of_received_millisecond_bucket",
        )

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
            current = time.time_ns() // 1_000_000
            aligned = current - ((current + 12) % 500)
            session = make_session(
                folder, base=aligned, submillisecond=True,
            )
            study = StudyTape([session])
            with patch("track_c_multivenue.compare.CMarket", FakeCMarket), patch(
                "track_c_multivenue.compare.A2Market", FakeA2Market,
            ), patch("track_c_multivenue.compare._shared_action", side_effect=action):
                report, candidates, outcomes, diagnostics = evaluate(
                    study, load_a2(),
                )
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["coin"], "ETH")
            self.assertTrue(all(candidates[0]["selected_by_policy"].values()))
            self.assertEqual(len(outcomes), 4)
            self.assertEqual(len(diagnostics), 1)
            self.assertEqual(
                candidates[0]["decision_ms"],
                candidates[0]["decision_bucket_ms"] + 1,
            )
            self.assertLess(
                candidates[0]["latest_included_received_ns"],
                candidates[0]["decision_ns"],
            )
            self.assertGreater(
                candidates[0]["decision_ns"],
                candidates[0]["latest_trigger_received_ns"],
            )
            self.assertLess(
                candidates[0]["decision_ns"]
                - candidates[0]["latest_trigger_received_ns"],
                1_000_000,
            )
            for name in POLICIES:
                self.assertEqual(report["policies"][name]["common_attempts"], 1)

    def test_full_raw_feature_execution_and_boundary_path_without_mocks(self):
        from track_c_multivenue.compare import evaluate

        with tempfile.TemporaryDirectory() as folder:
            session, config = make_warm_integration_session(folder)
            study = StudyTape([session])
            self.assertTrue(study.audit()["safe_for_research"])
            report, candidates, outcomes, diagnostics = evaluate(study, config)
            self.assertGreater(len(candidates), 0)
            self.assertTrue(any(
                row["state"]
                and row["state"]["reference"]["ready"]
                and row["state"]["risk"]["ready"]
                for row in candidates
            ))
            self.assertTrue(any(row["common_action"] for row in candidates))
            self.assertTrue(any(
                row["trigger"]["a2_deceleration"] for row in candidates
            ))
            self.assertTrue(any(
                row["trigger"]["c_new_sell_episode"] for row in candidates
            ))
            self.assertTrue(any(row["filled_qty"] > 0 for row in outcomes))
            self.assertTrue(any(row["fill_events"] >= 2 for row in outcomes))
            self.assertTrue(any(row["exit_phases"] for row in outcomes))
            self.assertTrue(any(
                any(phase["protection_present"] for phase in row["exit_phases"])
                for row in outcomes
            ))
            self.assertTrue(any(
                row["filled_qty"] > 0 and not row["censored"]
                for row in outcomes
            ))
            self.assertGreater(len(diagnostics), 0)
            self.assertEqual(report["policy_ranking"], "WITHHELD")

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
            self.assertTrue((output / "diagnostic-fill-markouts.jsonl.gz").is_file())
            self.assertTrue((output / "report.json").is_file())
            self.assertEqual(
                report["evaluation_design"]["status"], "EXPLORATORY_POSTHOC",
            )

    def test_negative_cash_is_a_research_outcome_not_a_numeric_error(self):
        from track_c_multivenue.compare import _policy_report

        candidates = [{
            "selected_by_policy": {name: name == "P0" for name in POLICIES},
            "attempted_by_policy": {name: name == "P0" for name in POLICIES},
        }]
        outcomes = [{
            "policy": "P0", "filled_qty": 1.0, "censored": False,
            "cash_net_krw": -12.5, "net_krw": -12.5, "net_bp": -4.0,
            "marked_net_bp": -4.0, "fully_cash_settled": True,
            "residual_valuation_status": "not_applicable",
            "residual_cost_krw": 0.0, "residual_value_krw": 0.0,
            "action": {"price": 100.0, "notional": 100.0},
        }]
        report = _policy_report(candidates, outcomes, "P0")
        self.assertEqual(
            report["fully_cash_settled"]["mean_net_krw_per_outcome"], -12.5,
        )
        self.assertEqual(
            report["cash_recovery_zero_residual_stress"]["mean_bp_per_attempt"],
            -4.0,
        )

    def test_censored_unrecovered_principal_is_not_mixed_into_settled_profit(self):
        from track_c_multivenue.compare import _policy_report

        candidates = [{
            "selected_by_policy": {name: name == "P0" for name in POLICIES},
            "attempted_by_policy": {name: name == "P0" for name in POLICIES},
        }] * 2
        outcomes = [
            {
                "policy": "P0", "filled_qty": 1.0, "censored": False,
                "cash_net_krw": 1.0, "net_krw": 1.0, "net_bp": 100.0,
                "marked_net_bp": 100.0, "fully_cash_settled": True,
                "residual_valuation_status": "not_applicable",
                "residual_cost_krw": 0.0, "residual_value_krw": 0.0,
                "action": {"price": 100.0, "notional": 100.0},
            },
            {
                "policy": "P0", "filled_qty": 100.0, "censored": True,
                "cash_net_krw": -10000.0, "net_krw": 0.0,
                "net_bp": -10000.0, "marked_net_bp": 0.0,
                "fully_cash_settled": False,
                "residual_valuation_status": "fresh_bid",
                "residual_cost_krw": 10000.0,
                "residual_value_krw": 10000.0,
                "action": {"price": 100.0, "notional": 10000.0},
            },
        ]
        report = _policy_report(candidates, outcomes, "P0")
        self.assertEqual(report["fully_cash_settled"]["outcomes"], 1)
        self.assertEqual(
            report["fully_cash_settled"]["mean_net_krw_per_outcome"], 1.0,
        )
        self.assertEqual(
            report["freshly_marked_outcomes"]["fresh_residual_value_krw"],
            10000.0,
        )
        self.assertEqual(
            report["cash_recovery_zero_residual_stress"]["mean_krw_per_attempt"],
            -4999.5,
        )
        self.assertEqual(report["censored_outcome_rate"], 0.5)
        self.assertFalse(report["selection_eligible"])

    def test_flat_price_boundary_inventory_stays_marked_not_realized_loss(self):
        from track_c.replay.execution import Attempt
        from track_c_multivenue.compare import _finish, _policy_report

        cfg = c_config()
        state = {
            "coin": "ETH", "t_ms": 0, "book_ms": 0,
            "bid": 100.0, "ask": 101.0,
            "bids": [(100.0, 1000.0)], "asks": [(101.0, 1000.0)],
            "reference": {
                "ready": True, "lower": 102.0, "upper": 103.0,
                "fair": 102.5, "m10": 0.0, "t_ms": 0,
            },
        }
        action = {
            "id": "0:minimum", "price": 100.0, "qty": 100.0,
            "qty_step": 1.0, "notional": 10000.0, "minimum": 5000.0,
            "stop": 90.0, "stop_limit": 89.0, "tick": 1.0,
            "ttl_s": 8, "hold_s": 180, "episode_id": "boundary",
            "coin": "ETH", "t_ms": 0, "reference": 102.5,
        }
        attempt = Attempt(action, cfg, state)
        attempt.event({
            "kind": "trade", "t": 400, "price": 99.0,
            "qty": 100.0, "buy": False,
        })
        outcomes = []
        remaining = _finish([{
            "policy": "P0", "candidate_id": "boundary",
            "session_id": "session", "attempt": attempt,
        }], outcomes, 1_000, boundary=True)
        self.assertEqual(remaining, [])
        self.assertEqual(outcomes[0]["net_krw"], 0.0)
        self.assertEqual(outcomes[0]["cash_net_krw"], -10000.0)
        self.assertEqual(outcomes[0]["residual_valuation_status"], "fresh_bid")
        candidates = [{
            "selected_by_policy": {name: name == "P0" for name in POLICIES},
            "attempted_by_policy": {name: name == "P0" for name in POLICIES},
        }]
        report = _policy_report(candidates, outcomes, "P0")
        self.assertEqual(report["fully_cash_settled"]["outcomes"], 0)
        self.assertEqual(
            report["freshly_marked_outcomes"]["mean_net_krw_per_valued_outcome"],
            0.0,
        )
        self.assertEqual(
            report["cash_recovery_zero_residual_stress"]["mean_bp_per_attempt"],
            -10000.0,
        )

    def test_future_window_registration_is_distinct_from_run_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            base = time.time_ns() // 1_000_000 + 10_000
            session = make_session(folder, base=base)
            registration = Path(folder) / "future-registration.json"
            document = create_registration(
                registration, load_a2(), base - 100, base + 2_000,
                created_ms=base - 200,
            )
            output = Path(folder) / "registered-result"
            report = run([session], output, registration_path=registration)
            self.assertEqual(
                report["evaluation_design"]["status"],
                "PREREGISTERED_FUTURE_WINDOW",
            )
            self.assertEqual(
                report["identity"]["registration_digest"],
                document["registration_digest"],
            )

    def test_registration_cannot_be_backdated_to_an_observed_window(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "created before"):
                create_registration(
                    Path(folder) / "invalid.json", load_a2(),
                    1_000, 2_000, created_ms=1_000,
                )

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
