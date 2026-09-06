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
from .normalize import NormalizedRecord, normalize_response, raw_hash
from .persistence import AppendOnlyJsonlStore


@dataclass(frozen=True, slots=True)
class SnapshotReceipt:
    first_received_at: datetime | None
    last_received_at: datetime | None
    paths: Mapping[str, Path]
    counts: Mapping[str, int]
    coverage: Mapping[str, int]
    gaps: Mapping[str, str]
    errors: Mapping[str, str]


def instrument_spec_from_observation(observation: Mapping[str, Any], observed_at: datetime) -> InstrumentSpec:
    """Convert a complete observation; multiplier is one because v3 reports base-coin quantity."""
    required = ("symbol", "category", "baseCoin", "quoteCoin", "settleCoin", "type", "status", "quantityMultiplier", "priceMultiplier", "minOrderQty", "minOrderAmount", "makerFeeRate", "takerFeeRate", "minLeverage", "maxLeverage", "fundInterval")
    missing = tuple(name for name in required if observation.get(name) in (None, ""))
    if missing:
        raise ValueError(f"incomplete futures instrument observation: {', '.join(missing)}")
    return InstrumentSpec(
        venue="bitget", api_family=ApiFamily.UTA_V3, symbol=str(observation["symbol"]), category=str(observation["category"]), base_coin=str(observation["baseCoin"]), quote_coin=str(observation["quoteCoin"]), settlement_coin=str(observation["settleCoin"]), contract_type=str(observation["type"]), is_linear=str(observation["category"]) == FUTURES_CATEGORY and str(observation["settleCoin"]) == "USDT", contract_multiplier=Decimal("1"), status=str(observation["status"]), price_tick=Decimal(str(observation["priceMultiplier"])), quantity_step=Decimal(str(observation["quantityMultiplier"])), min_order_quantity=Decimal(str(observation["minOrderQty"])), min_order_notional=Decimal(str(observation["minOrderAmount"])), max_limit_quantity=_optional_decimal(observation.get("maxOrderQty")), max_market_quantity=_optional_decimal(observation.get("maxMarketOrderQty")), min_leverage=_optional_decimal(observation.get("minLeverage")), max_leverage=_optional_decimal(observation.get("maxLeverage")), funding_interval_hours=int(str(observation["fundInterval"])), maker_fee_rate=Decimal(str(observation["makerFeeRate"])), taker_fee_rate=Decimal(str(observation["takerFeeRate"])), observed_at=observed_at, raw_hash=raw_hash(observation),
    )


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def collect_public_snapshot(state_directory: Path, client: Any | None = None) -> SnapshotReceipt:
    """Collect a bounded public slice; a failed endpoint does not discard other streams."""
    client = client or BitgetUtaV3PublicClient()
    store = AppendOnlyJsonlStore(state_directory)
    calls: list[tuple[str, str, str, str, Callable[[], tuple[Mapping[str, Any], datetime]], str | None]] = [
        ("spot_instruments", "instruments", SPOT_CATEGORY, SPOT_SYMBOL, client.spot_instruments, None),
        ("futures_instruments", "instruments", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_instruments, None),
        ("spot_ticker", "ticker", SPOT_CATEGORY, SPOT_SYMBOL, client.spot_ticker, None),
        ("futures_ticker", "ticker", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_ticker, None),
        ("spot_book", "orderbook", SPOT_CATEGORY, SPOT_SYMBOL, client.spot_orderbook, None),
        ("futures_book", "orderbook", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_orderbook, None),
        ("funding_current", "funding_current", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_funding_current, None),
        ("funding_history", "funding_history", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_funding_history, None),
        ("position_tiers", "position_tier", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_position_tiers, None),
        ("open_interest", "open_interest", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_open_interest, None),
        ("spot_fills", "fills", SPOT_CATEGORY, SPOT_SYMBOL, client.spot_fills, None),
        ("futures_fills", "fills", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_fills, None),
        ("spot_candles", "candles", SPOT_CATEGORY, SPOT_SYMBOL, client.spot_candles, "1m"),
        ("futures_candles", "candles", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_candles, "1m"),
        ("liquidations", "liquidations", FUTURES_CATEGORY, FUTURES_SYMBOL, client.futures_liquidations, None),
    ]
    calls.extend((f"futures_benchmark_{symbol}", "benchmark_ticker", FUTURES_CATEGORY, symbol, lambda symbol=symbol: client.futures_ticker(symbol), None) for symbol in BENCHMARK_SYMBOLS)
    paths: dict[str, Path] = {}; counts: dict[str, int] = {}; coverage: dict[str, int] = {}; gaps: dict[str, str] = {}; errors: dict[str, str] = {}; records: list[NormalizedRecord] = []
    for stream, record_type, category, symbol, call, interval in calls:
        paths[stream] = state_directory / f"{stream}.jsonl"
        try:
            payload, received_at = call()
            rows = normalize_response(record_type, payload, received_at, category=category, symbol=symbol, interval=interval)
            counts[stream] = store.append(stream, rows); coverage[stream] = len(rows); records.extend(rows)
            if not rows: gaps[stream] = "empty response"
        except Exception as exc:
            counts[stream] = coverage[stream] = 0; errors[stream] = f"{type(exc).__name__}: {exc}"
    receipt = SnapshotReceipt(min((row.received_at for row in records), default=None), max((row.received_at for row in records), default=None), paths, counts, coverage, gaps, errors)
    receipt_path = state_directory / "snapshot_receipts.jsonl"; receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("a", encoding="utf-8", newline="\n") as handle:
        value = asdict(receipt); value["first_received_at"] = receipt.first_received_at.isoformat() if receipt.first_received_at else None; value["last_received_at"] = receipt.last_received_at.isoformat() if receipt.last_received_at else None; value["paths"] = {key: str(path) for key, path in paths.items()}
        handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    paths["receipt"] = receipt_path
    return SnapshotReceipt(receipt.first_received_at, receipt.last_received_at, paths, counts, coverage, gaps, errors)
