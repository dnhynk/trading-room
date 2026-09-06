from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from track_special.arx_campaign.marketdata import AppendOnlyJsonlStore, collection_metrics, normalize_response
from track_special.arx_campaign.marketdata.bitget_uta_v3 import CAPABILITIES, FUTURES_SYMBOL, SPOT_SYMBOL, BitgetUtaV3PublicClient


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


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
        second = normalize_response("ticker", {"data": {"symbol": "ARXUSDT", "ts": "1788739200000", "lastPr": "2"}}, NOW + timedelta(seconds=7), category="SPOT", symbol="ARXUSDT")[0]
        with TemporaryDirectory() as directory:
            store = AppendOnlyJsonlStore(Path(directory))
            self.assertEqual(2, store.append("spot_arxusdt_ticker", (first, second)))
            self.assertEqual(2, len(store.readback("spot_arxusdt_ticker")))
        metric = collection_metrics((first, second))
        self.assertEqual(2, metric.count)
        self.assertEqual(7.0, metric.longest_gap_seconds)

    def test_client_has_no_unverified_position_tier_request(self) -> None:
        self.assertFalse(CAPABILITIES["position_tiers"].documented)
        with self.assertRaises(ValueError):
            BitgetUtaV3PublicClient().get("position_tiers")

    def test_exact_arx_series_and_documented_read_only_query_shapes(self) -> None:
        self.assertEqual("ARXUSDT", FUTURES_SYMBOL)
        self.assertEqual("ARXUSDT", SPOT_SYMBOL)
        self.assertTrue(all(cap.path.startswith("/api/v3/") for cap in CAPABILITIES.values() if cap.documented))


if __name__ == "__main__":
    unittest.main()
