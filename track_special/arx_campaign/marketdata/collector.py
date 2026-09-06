"""One-shot, unsigned public-market snapshot collection into external state."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from ..contracts import ApiFamily, InstrumentSpec
from .bitget_uta_v3 import BENCHMARK_SYMBOLS, BitgetUtaV3PublicClient, FUTURES_CATEGORY, FUTURES_SYMBOL, SPOT_CATEGORY, SPOT_SYMBOL
from .metrics import collection_metrics
from .normalize import NormalizedRecord, normalize_response, raw_hash
from .persistence import AppendOnlyJsonlStore


RESEARCH_INTERVALS = ("4H", "1H", "5m")
FUTURES_CANDLE_TYPES = ("market", "mark", "index")


@dataclass(frozen=True, slots=True)
class SnapshotReceipt:
    first_received_at: datetime | None
    last_received_at: datetime | None
    paths: Mapping[str, Path]
    counts: Mapping[str, int]
    coverage: Mapping[str, int]
    readback_counts: Mapping[str, int]
    longest_gap_seconds: Mapping[str, float | None]
    median_latency_ms: Mapping[str, float | None]
    negative_latency_count: Mapping[str, int]
    gaps: Mapping[str, str]
    errors: Mapping[str, str]
    futures_identity_verified: bool
    spot_identity_verified: bool


def instrument_spec_from_observation(observation: Mapping[str, Any], observed_at: datetime) -> InstrumentSpec:
    """Convert a complete observation; multiplier is one because v3 reports base-coin quantity."""
    required = ("symbol", "category", "baseCoin", "quoteCoin", "type", "status", "quantityMultiplier", "priceMultiplier", "minOrderQty", "minOrderAmount", "makerFeeRate", "takerFeeRate", "minLeverage", "maxLeverage", "fundInterval")
    missing = tuple(name for name in required if observation.get(name) in (None, ""))
    if missing:
        raise ValueError(f"incomplete futures instrument observation: {', '.join(missing)}")
    spec = InstrumentSpec(
        venue="bitget", api_family=ApiFamily.UTA_V3, symbol=str(observation["symbol"]), category=str(observation["category"]), base_coin=str(observation["baseCoin"]), quote_coin=str(observation["quoteCoin"]), settlement_coin="USDT", contract_type=str(observation["type"]), is_linear=str(observation["category"]) == FUTURES_CATEGORY and str(observation["quoteCoin"]) == "USDT" and observation.get("settleCoin", "USDT") == "USDT", contract_multiplier=Decimal("1"), status=str(observation["status"]), price_tick=Decimal(str(observation["priceMultiplier"])), quantity_step=Decimal(str(observation["quantityMultiplier"])), min_order_quantity=Decimal(str(observation["minOrderQty"])), min_order_notional=Decimal(str(observation["minOrderAmount"])), max_limit_quantity=_optional_decimal(observation.get("maxOrderQty")), max_market_quantity=_optional_decimal(observation.get("maxMarketOrderQty")), min_leverage=_optional_decimal(observation.get("minLeverage")), max_leverage=_optional_decimal(observation.get("maxLeverage")), funding_interval_hours=int(str(observation["fundInterval"])), maker_fee_rate=Decimal(str(observation["makerFeeRate"])), taker_fee_rate=Decimal(str(observation["takerFeeRate"])), observed_at=observed_at, raw_hash=raw_hash(observation),
    )
    if not spec.live_identity_verified:
        raise ValueError("observation is not the exact online ARX USDT-linear perpetual")
    return spec


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def collect_public_snapshot(state_directory: Path, client: Any | None = None) -> SnapshotReceipt:
    """Collect a bounded public slice; a failed endpoint does not discard other streams."""
    client = client or BitgetUtaV3PublicClient()
    store = AppendOnlyJsonlStore(state_directory)
    calls: list[tuple[str, str, str, str, Callable[[], tuple[Mapping[str, Any], datetime]], str | None, str | None]] = [
        ("spot_instruments", "instruments", SPOT_CATEGORY, SPOT_SYMBOL, client.spot_instruments, None, None),
        ("futures_instruments", "instruments", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_instruments, None, None),
        ("spot_ticker", "ticker", SPOT_CATEGORY, SPOT_SYMBOL, client.spot_ticker, None, None),
        ("futures_ticker", "ticker", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_ticker, None, None),
        ("spot_book", "orderbook", SPOT_CATEGORY, SPOT_SYMBOL, client.spot_orderbook, None, None),
        ("futures_book", "orderbook", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_orderbook, None, None),
        ("funding_current", "funding_current", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_funding_current, None, None),
        ("funding_history", "funding_history", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_funding_history, None, None),
        ("position_tiers", "position_tier", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_position_tiers, None, None),
        ("open_interest", "open_interest", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_open_interest, None, None),
        ("spot_fills", "fills", SPOT_CATEGORY, SPOT_SYMBOL, client.spot_fills, None, None),
        ("futures_fills", "fills", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_fills, None, None),
        ("liquidations", "liquidations", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_liquidations, None, None),
    ]
    for research_interval in RESEARCH_INTERVALS:
        def fetch_spot_candles(value: str = research_interval) -> tuple[Mapping[str, Any], datetime]:
            return client.spot_candles(interval=value, candle_type="market")

        calls.append(
            (f"spot_candles_market_{research_interval}", "candles", SPOT_CATEGORY, SPOT_SYMBOL,
             fetch_spot_candles, research_interval, "market")
        )
        for futures_candle_type in FUTURES_CANDLE_TYPES:
            def fetch_futures_candles(
                value: str = research_interval, kind: str = futures_candle_type
            ) -> tuple[Mapping[str, Any], datetime]:
                return client.futures_candles(interval=value, candle_type=kind)

            calls.append(
                (f"futures_candles_{futures_candle_type}_{research_interval}", "candles",
                 FUTURES_CATEGORY, FUTURES_SYMBOL, fetch_futures_candles,
                 research_interval, futures_candle_type)
            )
    for benchmark_symbol in BENCHMARK_SYMBOLS:
        def fetch_benchmark(symbol: str = benchmark_symbol) -> tuple[Mapping[str, Any], datetime]:
            result: tuple[Mapping[str, Any], datetime] = client.futures_ticker(symbol)
            return result
        calls.append((f"futures_benchmark_{benchmark_symbol}", "benchmark_ticker", FUTURES_CATEGORY, benchmark_symbol, fetch_benchmark, None, None))
    paths: dict[str, Path] = {}
    counts: dict[str, int] = {}
    coverage: dict[str, int] = {}
    readback_counts: dict[str, int] = {}
    longest_gaps: dict[str, float | None] = {}
    median_latencies: dict[str, float | None] = {}
    negative_latencies: dict[str, int] = {}
    gaps: dict[str, str] = {}
    errors: dict[str, str] = {}
    records: list[NormalizedRecord] = []
    futures_identity_verified = False
    spot_identity_verified = False
    for stream, record_type, category, symbol, call, record_interval, record_candle_type in calls:
        paths[stream] = state_directory / f"{stream}.jsonl"
        try:
            payload, received_at = call()
            data = payload.get("data")
            if isinstance(data, list):
                identity_items = data
            elif isinstance(data, Mapping) and isinstance(data.get("list"), list):
                identity_items = data["list"]
            else:
                identity_items = [data]
            identity_item = next(
                (item for item in identity_items if isinstance(item, Mapping)), None
            )
            if stream == "futures_instruments" and identity_item is not None:
                instrument_spec_from_observation(identity_item, received_at)
                futures_identity_verified = True
            if stream == "spot_instruments" and identity_item is not None:
                spot_identity_verified = all(
                    (
                        identity_item.get("symbol") == "ARXUSDT",
                        identity_item.get("category") == "SPOT",
                        identity_item.get("baseCoin") == "ARX",
                        identity_item.get("quoteCoin") == "USDT",
                        identity_item.get("status") == "online",
                    )
                )
                if not spot_identity_verified:
                    raise ValueError("spot response is not the exact online ARX/USDT market")
            rows = normalize_response(
                record_type,
                payload,
                received_at,
                category=category,
                symbol=symbol,
                interval=record_interval,
                candle_type=record_candle_type,
            )
            counts[stream] = store.append(stream, rows)
            coverage[stream] = (
                sum(bool(row.fields.get("completed")) for row in rows)
                if record_type == "candles"
                else len(rows)
            )
            readback_counts[stream] = len(store.readback(stream))
            metric = collection_metrics(rows)
            longest_gaps[stream] = metric.longest_gap_seconds
            median_latencies[stream] = metric.median_latency_ms
            negative_latencies[stream] = metric.negative_latency_count
            records.extend(rows)
            if not rows: gaps[stream] = "empty response"
        except Exception as exc:
            counts[stream] = coverage[stream] = readback_counts[stream] = 0
            longest_gaps[stream] = median_latencies[stream] = None
            negative_latencies[stream] = 0
            errors[stream] = f"{type(exc).__name__}: {exc}"
    receipt_path = state_directory / "snapshot_receipts.jsonl"
    paths["receipt"] = receipt_path
    receipt = SnapshotReceipt(
        min((row.received_at for row in records), default=None),
        max((row.received_at for row in records), default=None),
        paths,
        counts,
        coverage,
        readback_counts,
        longest_gaps,
        median_latencies,
        negative_latencies,
        gaps,
        errors,
        futures_identity_verified,
        spot_identity_verified,
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("a", encoding="utf-8", newline="\n") as handle:
        value = asdict(receipt); value["first_received_at"] = receipt.first_received_at.isoformat() if receipt.first_received_at else None; value["last_received_at"] = receipt.last_received_at.isoformat() if receipt.last_received_at else None; value["paths"] = {key: str(path) for key, path in paths.items()}
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return receipt
