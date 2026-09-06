from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from decimal import Decimal

from track_special.arx_campaign.marketdata import AppendOnlyJsonlStore, collect_public_snapshot, collection_metrics, instrument_spec_from_observation, normalize_response
from track_special.arx_campaign.marketdata.bitget_uta_v3 import BASE_URL, CAPABILITIES, BitgetUtaV3PublicClient


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


class FixtureClient:
    def __getattr__(self, name):
        def call(*args, **kwargs):
            if name == "futures_open_interest":
                raise RuntimeError("unavailable fixture endpoint")
            symbol = args[0] if args else "ARXUSDT"
            category = "SPOT" if name.startswith("spot_") else "USDT-FUTURES"
            if name == "futures_instruments":
                data = [{"symbol":"ARXUSDT","category":"USDT-FUTURES","baseCoin":"ARX","quoteCoin":"USDT","type":"perpetual","status":"online","quantityMultiplier":"1","priceMultiplier":"0.00001","minOrderQty":"1","minOrderAmount":"5","makerFeeRate":"0.0002","takerFeeRate":"0.0006","minLeverage":"1","maxLeverage":"20","fundInterval":"4"}]
            elif name == "spot_instruments":
                data = [{"symbol":"ARXUSDT","category":"SPOT","baseCoin":"ARX","quoteCoin":"USDT","status":"online"}]
            else:
                data = [["1788739140000", "1", "2", "1", "2", "3", "4"]] if "candles" in name else {"list": [{"symbol": symbol, "category": category, "ts": "1788739200000"}]}
            return {"code": "00000", "data": data}, NOW
        return call


class MarketDataTests(unittest.TestCase):
    def test_normalizes_duplicates_and_preserves_missing_values(self) -> None:
        payload = {"code": "00000", "requestTime": "1788739200000", "data": [
            {"symbol": "ARXUSDT", "tradeId": "same", "price": "1.2", "size": "3", "ts": "1788739200000"},
            {"symbol": "ARXUSDT", "tradeId": "same", "price": "1.2", "size": "3", "ts": "1788739200000"},
        ]}
        rows = normalize_response("trades", payload, NOW, category="USDT-FUTURES", symbol="ARXUSDT")
        self.assertEqual(2, len(rows))
        self.assertIsNone(rows[0].duplicate_of)
        self.assertIsNotNone(rows[1].duplicate_of)
        self.assertNotIn("markPrice", rows[0].fields)
        self.assertEqual("uta_v3", rows[0].api_family)

    def test_append_only_readback_and_gap_latency_metrics(self) -> None:
        first = normalize_response("ticker", {"data": {"symbol": "ARXUSDT", "ts": "1788739200000", "lastPr": "1"}}, NOW, category="SPOT", symbol="ARXUSDT")[0]
        second = normalize_response("ticker", {"data": {"symbol": "ARXUSDT", "ts": "1788739207000", "lastPr": "2"}}, NOW + timedelta(seconds=7), category="SPOT", symbol="ARXUSDT")[0]
        with TemporaryDirectory() as directory:
            store = AppendOnlyJsonlStore(Path(directory))
            self.assertEqual(2, store.append("spot_arxusdt_ticker", (first, second)))
            self.assertEqual(2, len(store.readback("spot_arxusdt_ticker")))
        metric = collection_metrics((first, second))
        self.assertEqual(2, metric.count)
        self.assertEqual(7.0, metric.longest_gap_seconds)

    def test_current_uta_routes_and_https_base(self) -> None:
        self.assertEqual(BASE_URL, "https://api.bitget.com")
        self.assertEqual(CAPABILITIES["tickers"].path, "/api/v3/market/tickers")
        self.assertEqual(CAPABILITIES["position_tiers"].path, "/api/v3/market/position-tier")
        self.assertEqual(CAPABILITIES["liquidations"].path, "/api/v3/market/liquidations")
        with self.assertRaises(ValueError):
            BitgetUtaV3PublicClient("http://api.bitget.com")

    def test_current_query_names_and_mismatched_response_fail(self) -> None:
        class Capture(BitgetUtaV3PublicClient):
            def __init__(self): self.calls = []
            def get(self, capability, params=None): self.calls.append((capability, params)); return {"code": "00000", "data": []}, NOW
        client = Capture(); client.futures_funding_history(cursor=2, limit=9); client.futures_candles(interval="5m", candle_type="mark", limit=8); client.futures_liquidations(cursor="next", limit=7)
        self.assertEqual(("funding_history", {"category": "USDT-FUTURES", "symbol": "ARXUSDT", "cursor": 2, "limit": 9}), client.calls[0])
        self.assertEqual({"category": "USDT-FUTURES", "symbol": "ARXUSDT", "interval": "5m", "type": "mark", "limit": 8}, client.calls[1][1])
        self.assertEqual({"category": "USDT-FUTURES", "symbol": "ARXUSDT", "limit": 7, "cursor": "next"}, client.calls[2][1])
        with self.assertRaises(RuntimeError):
            BitgetUtaV3PublicClient._validate_identity({"data": [{"symbol": "ETHUSDT", "category": "USDT-FUTURES"}]}, {"symbol": "ARXUSDT", "category": "USDT-FUTURES"}, "tickers")

    def test_nested_list_shape_and_completed_candles_preserve_raw(self) -> None:
        rows = normalize_response("funding_history", {"data": {"resultList": [{"symbol": "ARXUSDT", "tradeId": "x", "value": None}]}}, NOW, category="USDT-FUTURES", symbol="ARXUSDT")
        self.assertEqual(1, len(rows)); self.assertIsNone(rows[0].base_coin); self.assertIn("value", rows[0].fields)
        candles = normalize_response("candles", {"data": [["1788739140000", "1", "2", "1", "2", "3", "4"], ["1788739200000", "2", "3", "2", "3", "4", "5"]]}, NOW, category="SPOT", symbol="ARXUSDT", interval="1m")
        self.assertTrue(candles[0].fields["completed"]); self.assertFalse(candles[1].fields["completed"]); self.assertEqual(candles[0].raw["candle"][0], "1788739140000")

    def test_complete_instrument_conversion_keeps_base_quantity_inference_explicit(self) -> None:
        data = {"symbol": "ARXUSDT", "category": "USDT-FUTURES", "baseCoin": "ARX", "quoteCoin": "USDT", "type": "perpetual", "status": "online", "quantityMultiplier": "1", "priceMultiplier": "0.00001", "minOrderQty": "1", "minOrderAmount": "5", "makerFeeRate": "0.0002", "takerFeeRate": "0.0006", "minLeverage": "1", "maxLeverage": "20", "fundInterval": "4"}
        spec = instrument_spec_from_observation(data, NOW)
        self.assertEqual(Decimal("1"), spec.contract_multiplier); self.assertTrue(spec.live_identity_verified)
        del data["baseCoin"]
        with self.assertRaises(ValueError): instrument_spec_from_observation(data, NOW)

    def test_snapshot_receipt_isolates_failed_endpoint_and_keeps_benchmarks(self) -> None:
        with TemporaryDirectory() as directory:
            receipt = collect_public_snapshot(Path(directory), FixtureClient())
            self.assertGreater(receipt.counts["futures_ticker"], 0)
            self.assertIn("open_interest", receipt.errors)
            self.assertTrue(receipt.paths["futures_benchmark_BTCUSDT"].exists())
            self.assertTrue(receipt.paths["receipt"].exists())
            self.assertEqual(receipt.counts["futures_ticker"], receipt.readback_counts["futures_ticker"])
            self.assertTrue(receipt.futures_identity_verified)
            self.assertTrue(receipt.spot_identity_verified)


if __name__ == "__main__":
    unittest.main()
