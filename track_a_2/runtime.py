"""Coinone spot owner for Track A-2 long-only rotation.

The event loop owns market/strategy state. Blocking REST and durable OMS work is
serialized through worker threads. Every entry is admitted again immediately
before transport writes any order bytes.
"""
import asyncio
from collections import Counter
from concurrent.futures import TimeoutError as FutureTimeout
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import time

from common.signal import Strategy
from track_a_2 import EXECUTION_VERSION
from track_a_2.execution.client import CoinoneA2, read_credentials
from track_a_2.execution.oms import OMS
from track_a_2.execution.preflight import block_reasons
from track_a_2.execution.private_stream import follow as private_follow
from track_a_2.execution.store import Store, encoded
from track_a_2.market.feed import Market
from track_a_2.market.select import ranked, retain
from track_a_2.market.units import (
    floor_step,
    price_ceil,
    price_floor,
    stop_prices,
    stop_prices_for_limit_floor,
)
from track_a_2.settings import CONFIG, ROOT, load, resolved_env_path, resolved_state_directory
from track_a_2.strategy.sizing import size
from track_c.execution.coinone import CoinoneError, EntryExpired, decimal
from track_c.execution.http_pool import HTTPSPool
from track_c.execution.rate_limit import Transport


FEATURE_FIELDS = frozenset(("t", "mid", "bid", "ask", "v", "brk", "bko"))


class Runtime:
    def __init__(
        self, config, *, config_path=CONFIG, root=ROOT, client=None, store=None,
        clock=time.time, recovery_only=False, operational_checks=True,
    ):
        self.config = config
        self.config_path = Path(config_path)
        self.root = Path(root).resolve()
        self.clock = clock
        self.directory = resolved_state_directory(config, self.root)
        self.http_pool = None
        if client is None:
            credentials = read_credentials(
                resolved_env_path(config, self.root),
                profile=config["credential_profile"],
            )
            self.http_pool = HTTPSPool()
            transport = Transport(self.http_pool)
            client = CoinoneA2(
                credentials,
                transport=transport,
                timeout=float(config["http_timeout_s"]),
            )
        self.client = client
        self.store = store or Store(self.directory)
        self.oms = OMS(config, self.client, self.store, clock=clock)
        self.markets = {}
        self.strategies = {}
        self.strategy_params = {}
        self.sizing_reasons = {}
        self.coverage_reasons = {}
        self.counts = Counter()
        self.connected = False
        self.private_connected = False
        self.storage_ok = True
        self.stopping = False
        self.generation = 0
        self.last_scan = self.last_account = self.last_report = 0.0
        self.last_private_events = 0
        self.raw = None
        self.raw_hour = None
        self.last_contract_digest = None
        self.wakeup = asyncio.Event()
        self.loop = None
        self.egress = config.get("expected_egress_ip")
        self.recovery_only = bool(recovery_only)
        self.operational_checks = bool(operational_checks)

    def controls(self):
        repository_stop = (self.root / "STOP").exists()
        repository_pause = (self.root / "PAUSE").exists()
        runtime_stop = (self.directory / "STOP").exists()
        runtime_pause = (self.directory / "PAUSE").exists()
        return dict(
            # An explicitly launched recovery owner may manage A-2 exposure
            # while the repository-wide A/B suspension remains in place. Its
            # own state-directory STOP still terminates it.
            stop=runtime_stop or (
                repository_stop
                and not self.recovery_only
                and not self.config["repository_ab_controls_acknowledged"]
            ),
            pause=runtime_pause or self.recovery_only or (
                repository_pause
                and not self.config["repository_ab_controls_acknowledged"]
            ),
        )

    def activation_reasons(self):
        try:
            current = load(self.config_path, root=self.root)
        except ValueError:
            return ["configuration_unavailable"]
        if current != self.config:
            return ["configuration_changed"]
        return block_reasons(current, root=self.root, egress=self.egress)

    def marks(self):
        return {
            coin: market.book["bids"][0]["price"]
            for coin, market in self.markets.items()
            if market.book and market.book.get("bids")
        }

    def record(self, received_ms, message):
        if not self.storage_ok:
            return
        try:
            hour = time.strftime("%Y%m%d-%H", time.gmtime(received_ms / 1000))
            if hour != self.raw_hour:
                if self.raw:
                    self.raw.close()
                folder = self.directory / "public"
                folder.mkdir(parents=True, exist_ok=True)
                self.raw = gzip.open(folder / (hour + ".jsonl.gz"), "at", encoding="utf-8")
                self.raw_hour = hour
            self.raw.write(encoded(dict(received_ms=received_ms, message=message)) + "\n")
        except OSError:
            self.storage_ok = False
            self.counts["public_record_errors"] += 1
            self.oms.halt("PUBLIC_RECORDING")

    async def public_feed(self):
        from websockets.asyncio.client import connect

        backoff = 1
        while not self.stopping:
            try:
                async with connect(
                    "wss://stream.coinone.co.kr",
                    open_timeout=10,
                    ping_interval=15,
                    ping_timeout=15,
                    close_timeout=3,
                    max_queue=2048,
                ) as websocket:
                    subscribed, pending = set(), set()
                    last_ping = time.monotonic()
                    while not self.stopping:
                        desired = {
                            (coin, channel)
                            for coin in self.markets
                            for channel in ("ORDERBOOK", "TRADE")
                        }
                        for action, pairs in (
                            ("UNSUBSCRIBE", subscribed - desired),
                            ("SUBSCRIBE", desired - subscribed),
                        ):
                            for coin, channel in sorted(pairs):
                                await websocket.send(encoded(dict(
                                    request_type=action,
                                    channel=channel,
                                    topic=dict(quote_currency="KRW", target_currency=coin),
                                )))
                                if action == "SUBSCRIBE":
                                    pending.add((coin, channel))
                                else:
                                    pending.discard((coin, channel))
                        subscribed = desired
                        if time.monotonic() - last_ping >= 15:
                            await websocket.send('{"request_type":"PING"}')
                            last_ping = time.monotonic()
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), 1)
                        except asyncio.TimeoutError:
                            continue
                        received = time.time_ns() // 1_000_000
                        message = json.loads(raw)
                        self.record(received, message)
                        data = message.get("data") or {}
                        coin = data.get("target_currency")
                        channel = message.get("channel")
                        kind = message.get("response_type")
                        if kind == "ERROR":
                            raise CoinoneError("public subscription rejected")
                        if kind == "SUBSCRIBED":
                            pending.discard((coin, channel))
                        self.connected = bool(subscribed) and not pending
                        if kind != "DATA" or coin not in self.markets:
                            continue
                        market = self.markets[coin]
                        revision = market.revision
                        market.feed(channel, data, received)
                        if market.revision != revision:
                            self.counts["public_messages"] += 1
                            self.wakeup.set()
                    backoff = 1
            except Exception:
                self.counts["public_ws_errors"] += 1
            finally:
                self.connected = False
                for market in self.markets.values():
                    market.book = None
                    market.book_received = market.book_exchange = 0
                    market.signals.clear()
            if not self.stopping:
                await asyncio.sleep(backoff)
                backoff = min(15, backoff * 2)

    async def _metadata_snapshot(self, coin, contract, seeded):
        fees, units = await asyncio.gather(
            asyncio.to_thread(self.client.fees, coin),
            asyncio.to_thread(self.client.price_units, coin),
        )
        if any(decimal(value) > decimal(self.config["max_fee_rate"]) for value in fees.values()):
            raise CoinoneError("fee exceeds Track A-2 ceiling")
        candles = None
        if not seeded:
            one, fifteen, daily = await asyncio.gather(
                asyncio.to_thread(self.client.candles, coin, "1m", 500),
                asyncio.to_thread(self.client.candles, coin, "15m", 500),
                asyncio.to_thread(self.client.candles, coin, "1d", 400),
            )
            candles = dict(one=one, fifteen=fifteen, daily=daily)
        return dict(contract=contract, units=units, fees=fees, candles=candles)

    async def _discover(self):
        """Fetch a scan snapshot without mutating event-loop-owned market state."""
        contracts, tickers = await asyncio.to_thread(self.client.universe)
        candidates, reasons = ranked(contracts, tickers, self.config)
        by_coin = {row["target_currency"]: row for row in contracts}
        held = {
            coin for coin in self.oms.state["books"]
            if self.oms.quantity(coin) or self.oms.active(coin)
        }
        wanted = set(held) | {row["coin"] for row in candidates}
        tasks = {}
        for coin in sorted(wanted):
            contract = by_coin.get(coin)
            if contract is None:
                reasons[coin] = "market_missing"
                continue
            tasks[coin] = asyncio.create_task(
                self._metadata_snapshot(coin, contract, coin in self.markets)
            )
        available = {}
        if tasks:
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for coin, result in zip(tasks, results):
                if isinstance(result, Exception):
                    reasons[coin] = "metadata_or_fee"
                    self.counts["scan_market_errors"] += 1
                else:
                    available[coin] = result
        return dict(
            candidates=candidates,
            reasons=reasons,
            available=available,
            captured_ms=int(self.clock() * 1000),
        )

    def _apply_scan(self, snapshot):
        """Atomically apply a completed network snapshot on the owner loop."""
        candidates = snapshot["candidates"]
        reasons = snapshot["reasons"]
        metadata = snapshot["available"]
        old_markets = self.markets
        available = {}
        for coin, row in metadata.items():
            old = old_markets.get(coin)
            try:
                if old is None:
                    candles = row.get("candles") or {}
                    available[coin] = Market(
                        coin, self.config, row["contract"], row["units"], row["fees"],
                        candles.get("one", ()), candles.get("fifteen", ()),
                        candles.get("daily", ()), now_ms=snapshot["captured_ms"],
                    )
                else:
                    old.contract, old.units, old.fees = (
                        row["contract"], row["units"], row["fees"]
                    )
                    available[coin] = old
            except (CoinoneError, KeyError, TypeError, ValueError):
                reasons[coin] = "metadata_or_fee"
                self.counts["scan_market_errors"] += 1
        eligible = [row for row in candidates if row["coin"] in available]
        held = {
            coin for coin in self.oms.state["books"]
            if self.oms.quantity(coin) or self.oms.active(coin)
        }
        plan = retain(
            self.oms.state.get("selected", []), held, eligible, self.config["basket_size"]
        )
        self.oms.set_selection(plan, reasons)
        self.markets = {
            coin: available.get(coin) or old_markets[coin]
            for coin in plan["watch"]
            if coin in available or coin in old_markets
        }
        for coin in list(self.strategies):
            if coin not in self.markets:
                self.strategies.pop(coin, None)
                self.strategy_params.pop(coin, None)
        self.coverage_reasons = reasons
        self.generation += 1
        self.last_scan = self.clock()
        contract_state = dict(
            selection_contract=self.config["selection_contract"],
            markets={
                coin: dict(
                    contract=row["contract"], units=row["units"], fees=row["fees"],
                )
                for coin, row in sorted(metadata.items())
            },
        )
        digest = hashlib.sha256(encoded(contract_state).encode()).hexdigest()
        if digest != self.last_contract_digest:
            recorded = dict(captured_ms=snapshot["captured_ms"], **contract_state)
            body = encoded(recorded)
            folder = self.directory / "contracts"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (str(recorded["captured_ms"]) + ".json")
            path.write_text(body + "\n", encoding="utf-8")
            self.last_contract_digest = digest
        self.store.event(
            "SCAN",
            selected=plan["selected"],
            wind_down=plan["wind_down"],
            watched=sorted(self.markets),
            reasons=reasons,
        )

    async def scan(self):
        self._apply_scan(await self._discover())

    def notify_fills(self, fills):
        for fill in fills:
            strategy = self.strategies.get(fill["coin"])
            if (
                strategy is not None
                and fill["role"] == "buy"
                and decimal(fill["qty"]) > 0
            ):
                strategy.on_fill("buy", float(fill["qty"]))

    def reconcile(self, *, force=False):
        before = {cid: order["status"] for cid, order in self.oms.state["orders"].items()}
        fills = self.oms.reconcile(force=force)
        changed = any(
            before.get(cid) != order["status"]
            for cid, order in self.oms.state["orders"].items()
        )
        if fills or changed:
            self.oms.state["account_at"] = 0
            self.last_account = 0
        return fills, changed

    async def refresh_account(self, *, reconcile=True):
        fills = []
        if reconcile:
            fills, _ = await asyncio.to_thread(self.reconcile, force=True)
            self.notify_fills(fills)
        balances = await asyncio.to_thread(self.client.balances)
        orders = await asyncio.to_thread(self.client.active_orders)
        steps = {}
        for coin, book in self.oms.state["books"].items():
            if coin in self.markets:
                steps[coin] = self.markets[coin].contract.get("qty_unit", "0")
            elif book.get("strategy_params"):
                steps[coin] = book["strategy_params"].get("qstep", "0")
        self.oms.sync_account(balances, orders, steps)
        self.last_account = self.clock()
        return fills

    def _strategy(self, coin):
        market = self.markets[coin]
        book = self.oms.book(coin)
        params = book.get("strategy_params")
        if params is None:
            if not market.book:
                return None
            sized = size(
                self.config,
                market.contract,
                market.fees,
                market.book,
                equity=self.oms.equity(self.marks()),
                cash=self.oms.free_cash(),
                portfolio_notional=self.oms.portfolio_notional(self.marks()),
            )
            self.sizing_reasons[coin] = sized["reason"]
            if sized["reason"]:
                return None
            params = {
                **self.config["strategy"],
                "side": "long",
                "unit_qty": float(sized["qty"]),
                "max_notional": float(sized["max_notional"]),
                "cap_usdt": float(sized["cap_krw"]),
                "campaign_loss_budget_krw": float(sized["campaign_loss_budget_krw"]),
                "tick": float(market.tick()),
                "qstep": float(market.contract["qty_unit"]),
                "fee_rt_pct": float(sized["fee_rt_pct"]),
                "lever": 0,
                "margin_mode": None,
                "wallet_frac": 1.0,
                "wind_down": False,
                "unit_frac": 0.0,
                "cap_frac": 0.0,
                "daily_loss_frac": 0.0,
                "notional_frac": 0.0,
            }
            self.oms.set_strategy_params(coin, params)
            self.store.event(
                "SIZE",
                coin=coin,
                unit_qty=sized["qty"],
                cap_krw=sized["cap_krw"],
                max_notional=sized["max_notional"],
                binding=sized["binding"],
            )
        if coin not in self.strategies or self.strategy_params.get(coin) != params:
            strategy = Strategy(params, self.config["signal"])
            if book.get("desired_stop"):
                strategy.adopt_stop(float(book["desired_stop"]))
            self.strategies[coin] = strategy
            self.strategy_params[coin] = dict(params)
        strategy = self.strategies[coin]
        # Coinone's price unit is banded and can change while a campaign is open.
        # Exchange metadata is dynamic execution state, not a campaign resize.
        strategy.p["tick"] = float(market.tick())
        strategy.p["qstep"] = float(market.contract["qty_unit"])
        current_round_trip = float(
            (decimal(market.fees["maker"]) + decimal(market.fees["taker"])) * D(100)
        )
        strategy.p["fee_rt_pct"] = max(float(params.get("fee_rt_pct", 0)), current_round_trip)
        # A fee floor is a floor, even when a configured normal gate is lower.
        for name in ("pop_min_pct", "unit_min_pct", "gate_floor_unit_pct"):
            strategy.p[name] = max(float(params.get(name, 0)), current_round_trip)
        return strategy

    def working(self, coin):
        def row(role):
            orders = self.oms.active(coin, role)
            if not orders:
                return None
            order = orders[0]
            price = order.get("price") or order.get("limit_price")
            return (float(price), float(self.oms.remaining(order))) if price else None
        return dict(buy=row("buy"), trim=row("trim"))

    def desired(self, coin, now_ms):
        market = self.markets[coin]
        signals = market.drain()
        feature = market.features.f
        if (
            not isinstance(feature, dict)
            or not FEATURE_FIELDS <= set(feature)
            or any(feature.get(name) is None for name in FEATURE_FIELDS)
        ):
            self.counts["incomplete_features"] += 1
            return None
        try:
            feature_age = now_ms - (int(feature["t"]) + 1) * 1000
            for name in ("mid", "bid", "ask"):
                decimal(feature[name], positive=True)
        except (CoinoneError, TypeError, ValueError):
            self.counts["incomplete_features"] += 1
            return None
        if not 0 <= feature_age <= self.config["quote_max_age_ms"]:
            self.counts["stale_features"] += 1
            return None
        fresh_signals = []
        for signal in signals:
            try:
                # Features label the second being closed; the event exists at
                # the following second boundary, not at that second's start.
                age = now_ms - (int(signal["t"]) + 1) * 1000
            except (KeyError, TypeError, ValueError):
                age = self.config["quote_max_age_ms"] + 1
            if 0 <= age <= self.config["quote_max_age_ms"]:
                fresh_signals.append(signal)
            else:
                self.counts["expired_signals"] += 1
        signals = fresh_signals
        causes = [
            {key: signal.get(key) for key in ("sig", "src", "t")}
            for signal in signals
        ]
        for signal in signals:
            try:
                self.store.event("SIGNAL", coin=coin, signal=signal)
            except (TypeError, ValueError):
                self.counts["signal_record_errors"] += 1
        strategy = self._strategy(coin)
        if strategy is None:
            return None
        controls = self.controls()
        paused = controls["pause"] or controls["stop"] or self.stopping or self.recovery_only
        output = strategy.step(
            feature,
            signals,
            self.oms.position(coin, paused=paused),
            self.working(coin),
        )
        for kind, fields in output["events"]:
            self.store.event("STRATEGY_" + kind, coin=coin, causes=causes, **fields)
        normalized = dict(buy=None, trim=None, trigger=None, limit=None, no_stop=output["no_stop"])
        if output["buy"]:
            price, qty = output["buy"]
            normalized["buy"] = (
                price_floor(market.units, price),
                floor_step(qty, market.contract["qty_unit"]),
            )
        if output["trim"]:
            price, qty, scope, lot, *tags = output["trim"]
            pull = strategy.pull or {}
            gate = pull.get("gate")
            purpose = "risk" if pull.get("exit") or (gate is not None and gate < 0) else "profit"
            normalized["trim"] = dict(
                price=price_floor(market.units, price),
                qty=floor_step(qty, market.contract["qty_unit"]),
                requested_scope=scope,
                lot=lot,
                tag=tags[0] if tags else None,
                purpose=purpose,
                ref=pull.get("ref"),
                gate_pct=gate,
            )
        raw_stop = decimal(output["stop"]) if output["stop"] is not None else None
        campaign_floor = self.oms.campaign_stop_floor(coin, market.fees["taker"])
        if campaign_floor and (raw_stop is None or campaign_floor > raw_stop):
            raw_stop, _ = stop_prices_for_limit_floor(
                market.units,
                campaign_floor,
                self.config["stop_limit_buffer_ticks"],
                self.config["stop_limit_buffer_bp"],
            )
            self.counts["campaign_stop_floor"] += 1
        if raw_stop is not None:
            trigger, limit = stop_prices(
                market.units,
                raw_stop,
                self.config["stop_limit_buffer_ticks"],
                self.config["stop_limit_buffer_bp"],
            )
            if campaign_floor and limit < campaign_floor:
                trigger, limit = stop_prices_for_limit_floor(
                    market.units,
                    campaign_floor,
                    self.config["stop_limit_buffer_ticks"],
                    self.config["stop_limit_buffer_bp"],
                )
            normalized.update(trigger=trigger, limit=limit)
            normalized["no_stop"] = False
        return normalized

    def _sell_quote(self, market, qty, minimum_price=None):
        """Conservative executable sell quote from the first five bid levels."""
        remaining = decimal(qty, positive=True)
        minimum_price = decimal(minimum_price) if minimum_price is not None else None
        gross = D(0)
        filled = D(0)
        worst = None
        for row in (market.book or {}).get("bids", [])[:5]:
            price = decimal(row["price"], positive=True)
            if minimum_price is not None and price < minimum_price:
                continue
            available = decimal(row["qty"]) * decimal(self.config["depth_fraction"])
            take = min(remaining, available)
            if take > 0:
                gross += take * price
                filled += take
                remaining -= take
                worst = price
            if remaining <= 0:
                break
        step = decimal(market.contract["qty_unit"], positive=True)
        if remaining > step / 2 or not filled:
            return None
        return dict(qty=filled, gross=gross, vwap=gross / filled, worst=worst)

    def _profit_price(self, market, trim):
        ref = trim.get("ref")
        if ref is None:
            ref = self.oms.book(market.coin).get("avg") or trim["price"]
        ref = decimal(ref, positive=True)
        gate = max(D(0), decimal(trim.get("gate_pct") or 0)) / D(100)
        maker = decimal(market.fees["maker"])
        taker = decimal(market.fees["taker"])
        if taker >= 1:
            raise CoinoneError("invalid taker fee")
        gate_price = ref * (D(1) + gate)
        fee_break_even = ref * (D(1) + maker) / (D(1) - taker)
        return price_ceil(
            market.units,
            max(decimal(trim["price"], positive=True), gate_price, fee_break_even),
        )

    def validate_intent(
        self, coin, role, side, qty, *, fee_rate=0, price=None,
        minimum_price=None, minimum_gross=None, own_order=None,
        require_intent=False, operational=True, now_ms=None,
    ):
        """Pure admission decision reused for planning, final send, and replay."""
        market = self.markets[coin]
        qty = decimal(qty, positive=True)
        now_ms = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
        if not 0 <= self.clock() - self.oms.state["account_at"] <= self.config["account_fresh_s"]:
            return "account_age"
        if require_intent:
            active = [order for order in self.oms.active(coin) if order["side"] == side]
            if (
                len(active) != 1
                or active[0]["role"] != role
                or active[0]["status"] != "INTENT"
                or own_order is not active[0]
            ):
                return "order_ownership_changed"
        if side == "BUY":
            if operational and self.operational_checks:
                reasons = self.activation_reasons()
                if reasons:
                    return reasons[0]
            if self.stopping or not self.connected or not self.private_connected or not self.storage_ok:
                return "runtime_unavailable"
            if not market.fresh(now_ms) or not market.book:
                return "market_age"
            price = decimal(price, positive=True) if price is not None else None
            current_bid = decimal(market.book["bids"][0]["price"], positive=True)
            if price is None or price > current_bid:
                return "entry_price_changed"
            book = self.oms.book(coin)
            if (
                self.oms.state["halt"]
                or not book["selected"]
                or book["wind_down"]
                or coin in self.oms.state.get("foreign_assets", ())
                or coin in self.oms.state.get("foreign_order_coins", ())
            ):
                return "entry_admission_changed"
            if self.oms.daily_blocked(self.marks()):
                return "daily_loss"
            if self.oms.state["day_stops"] >= self.config["strategy"]["max_stops_day"]:
                return "daily_stops"
            minimum = decimal(market.contract["min_order_amount"], positive=True)
            minimum_qty = decimal(market.contract["min_qty"], positive=True)
            if qty < minimum_qty or qty * price < minimum:
                return "entry_minimum_changed"
            if qty > self._buy_cap(
                coin, price, fee_rate, exclude_order=own_order,
            ):
                return "entry_size_changed"
            projected_floor = self.oms.projected_campaign_stop(
                coin, qty, price, fee_rate, market.fees["taker"],
            )
            if projected_floor > 0:
                projected_trigger, _ = stop_prices_for_limit_floor(
                    market.units,
                    projected_floor,
                    self.config["stop_limit_buffer_ticks"],
                    self.config["stop_limit_buffer_bp"],
                )
                if projected_trigger >= current_bid:
                    return "campaign_risk_changed"
            open_books = sum(
                bool(self.oms.quantity(name) or self.oms.active(name, "buy"))
                for name in self.oms.state["books"]
            )
            if open_books > self.config["max_open_books"]:
                return "open_book_limit_changed"
        else:
            if self.oms.quantity(coin) < qty:
                return "inventory_changed"
            if self.oms.available_asset(coin, exclude_order=own_order) < qty:
                return "available_inventory_changed"
            if role in ("trim", "exit") and (not market.book or not market.fresh(now_ms)):
                return "market_age"
            if role == "trim" and self.oms.book(coin).get("desired_stop"):
                current_bid = decimal(market.book["bids"][0]["price"], positive=True)
                if current_bid <= decimal(self.oms.book(coin)["desired_stop"]):
                    return "stop_priority"
            if role == "trim" and minimum_price is not None:
                quote = self._sell_quote(market, qty, minimum_price)
                if (
                    quote is None
                    or minimum_gross is not None
                    and quote["gross"] < decimal(minimum_gross)
                ):
                    return "profit_execution_changed"
        return None

    def submission_guard(
        self, coin, role, side, qty, *, fee_rate=0, price=None,
        minimum_price=None, minimum_gross=None, deadline=None,
    ):
        loop = self.loop
        deadline = deadline or (time.monotonic() + self.config["quote_max_age_ms"] / 1000)

        def decision():
            active = [order for order in self.oms.active(coin) if order["side"] == side]
            own_order = active[0] if len(active) == 1 else None
            return self.validate_intent(
                coin, role, side, qty, fee_rate=fee_rate, price=price,
                minimum_price=minimum_price, minimum_gross=minimum_gross,
                own_order=own_order, require_intent=True,
                now_ms=(
                    time.time_ns() // 1_000_000
                    if self.operational_checks else int(self.clock() * 1000)
                ),
            )

        async def validate():
            return decision()

        def check():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EntryExpired("decision_age")
            if loop is None:
                reason = decision()
            else:
                future = asyncio.run_coroutine_threadsafe(validate(), loop)
                try:
                    reason = future.result(timeout=remaining)
                except FutureTimeout:
                    future.cancel()
                    raise EntryExpired("validation_wait_expired") from None
            if reason:
                raise EntryExpired(reason)
            if time.monotonic() > deadline:
                raise EntryExpired("decision_age")
        return check

    def _cancel(self, orders):
        fills = []
        for order in list(orders):
            fills.extend(self.oms.cancel(order))
        if orders:
            self.oms.state["account_at"] = 0
            self.last_account = 0
        return fills

    @staticmethod
    def _same(order, *, qty, price=None, trigger=None):
        if decimal(order["qty"]) != decimal(qty):
            return False
        if price is not None and decimal(order.get("price") or 0) != decimal(price):
            return False
        if trigger is not None and decimal(order.get("trigger_price") or 0) != decimal(trigger):
            return False
        return True

    def _confirmed_protection(self, order, *, qty, price, trigger):
        return bool(
            order
            and order.get("status") == "NOT_TRIGGERED"
            and not order.get("cancel_requested")
            and self.oms.remaining(order) == decimal(qty)
            and self._same(order, qty=qty, price=price, trigger=trigger)
        )

    def _buy_cap(self, coin, price, fee_rate, *, exclude_order=None):
        """Return capacity after excluding only the intent being revalidated."""
        market = self.markets[coin]
        price, fee_rate = decimal(price, positive=True), decimal(fee_rate)
        held = self.oms.quantity(coin)
        params = self.oms.book(coin).get("strategy_params") or {}
        excluded_notional = D(0)
        excluded_reservation = D(0)
        if exclude_order is not None:
            excluded_qty = self.oms.remaining(exclude_order)
            excluded_price = decimal(exclude_order["price"], positive=True)
            excluded_notional = excluded_qty * excluded_price
            excluded_reservation = excluded_notional * (
                D(1) + decimal(exclude_order["fee_rate"])
            )
        pending_same = sum(
            (
                self.oms.remaining(order)
                for order in self.oms.active(coin, "buy")
                if order is not exclude_order
            ),
            D(0),
        )
        bid_depth = sum(
            (decimal(row["qty"]) for row in (market.book or {}).get("bids", [])[:5]),
            D(0),
        ) * decimal(self.config["depth_fraction"])
        ask_depth = sum(
            (decimal(row["qty"]) for row in (market.book or {}).get("asks", [])[:5]),
            D(0),
        ) * decimal(self.config["depth_fraction"])
        equity = self.oms.equity(self.marks())
        portfolio_used = max(
            D(0),
            self.oms.portfolio_notional(self.marks()) - excluded_notional,
        )
        portfolio_room = max(
            D(0),
            equity * decimal(self.config["portfolio_notional_fraction"])
            - portfolio_used,
        ) / price
        book_room = max(
            D(0),
            D(str(params.get("max_notional") or 0)) / price - held - pending_same,
        )
        ledger_cash = max(
            D(0),
            D(self.oms.state["cash_krw"])
            - (self.oms.reserved_cash() - excluded_reservation),
        )
        account_cash = self.oms._account_cash_available()
        if exclude_order is not None and exclude_order.get("status") != "INTENT":
            account_cash += excluded_reservation
        cash_room = min(account_cash, ledger_cash) / (price * (D(1) + fee_rate))
        exit_room = max(D(0), bid_depth - held - pending_same)
        cap = min(
            ask_depth, exit_room, portfolio_room, book_room, cash_room,
            decimal(market.contract["max_qty"]),
            decimal(market.contract["max_order_amount"]) / (price * (D(1) + fee_rate)),
        )
        return floor_step(cap, market.contract["qty_unit"])

    def _submit(self, coin, role, side, kind, qty, fee_rate, **fields):
        guard = self.submission_guard(
            coin, role, side, qty,
            fee_rate=fee_rate,
            price=fields.get("price"),
            minimum_price=fields.get("required_price"),
            minimum_gross=fields.get("minimum_gross"),
        )
        order = self.oms.submit(
            coin, role, side, kind, qty,
            fee_rate=fee_rate,
            before_send=guard,
            **fields,
        )
        return order

    def drive(self, coin, desired, *, fresh, stopping=False):
        market = self.markets[coin]
        book = self.oms.book(coin)
        fills = []
        qty = self.oms.quantity(coin)
        minimum = decimal(market.contract["min_order_amount"], positive=True)
        minimum_qty = decimal(market.contract["min_qty"], positive=True)
        maker, taker = decimal(market.fees["maker"]), decimal(market.fees["taker"])
        buy_orders = self.oms.active(coin, "buy")
        sell_orders = [order for order in self.oms.active(coin) if order["side"] == "SELL"]

        if buy_orders and any(decimal(order["filled"]) for order in buy_orders):
            return self._cancel(buy_orders)

        if qty and desired and desired["trigger"] is not None:
            trigger, limit = desired["trigger"], desired["limit"]
            current = decimal(book["desired_stop"]) if book.get("desired_stop") else None
            if current is None or trigger > current:
                book["desired_stop"], book["stop_limit"] = str(trigger), str(limit)
                self.oms.save("STOP_PLAN", coin=coin, trigger=str(trigger), limit=str(limit))
        trigger = decimal(book["desired_stop"]) if book.get("desired_stop") else None
        limit = decimal(book["stop_limit"]) if book.get("stop_limit") else None

        if stopping:
            cancellable = buy_orders + [
                order for order in sell_orders if order["role"] == "trim"
            ]
            if cancellable:
                return self._cancel(cancellable)
            if any(order["role"] == "exit" for order in sell_orders):
                return fills
            desired = dict(buy=None, trim=None, trigger=trigger, limit=limit, no_stop=False)

        bid = decimal(market.book["bids"][0]["price"], positive=True) if fresh and market.book else None
        protect = next((order for order in sell_orders if order["role"] == "protect"), None)
        if protect and protect.get("status") == "TRIGGERED":
            protect.setdefault("triggered_at", self.clock())
            if bid is not None and (
                bid <= decimal(protect["price"])
                or self.clock() - protect["triggered_at"] >= 2
            ):
                book["exit_reason"] = "stop_limit_gap"
                self.oms.save("EXIT_REQUEST", coin=coin, reason=book["exit_reason"])
        if qty and desired and desired.get("no_stop"):
            book["exit_reason"] = "strategy_stop_unavailable"
            self.oms.save("EXIT_REQUEST", coin=coin, reason=book["exit_reason"])
        if qty and trigger is not None and bid is not None and bid <= trigger and protect is None:
            book["exit_reason"] = "stop_without_protection"
            self.oms.save("EXIT_REQUEST", coin=coin, reason=book["exit_reason"])

        if qty and book.get("exit_reason"):
            if buy_orders:
                return self._cancel(buy_orders)
            # Keep any exchange-side sell protection in place until a fresh
            # executable market exit can replace it.
            if bid is None:
                return fills
            if sell_orders:
                if any(order["role"] != "exit" for order in sell_orders):
                    return self._cancel(sell_orders)
                return fills
            if qty < minimum_qty or qty * bid < minimum:
                self.oms.halt("UNSELLABLE_RESIDUAL", coin=coin, quantity=str(qty))
                return fills
            if self.oms.available_asset(coin) < qty:
                self.oms.state["account_at"] = 0
                self.last_account = 0
                return fills
            self._submit(
                coin, "exit", "SELL", "MARKET", qty, taker,
                reason=book["exit_reason"],
            )
            return fills

        trim = desired.get("trim") if desired else None
        trim_deferred = False
        if qty and trim and decimal(trim["qty"]) > 0:
            trim_qty = min(qty, decimal(trim["qty"]))
            if bid is None or trim_qty < minimum_qty or trim_qty * bid < minimum:
                self.counts["trim_below_minimum"] += 1
                trim_deferred = True
            elif trigger is not None and bid <= trigger:
                book["exit_reason"] = "stop_during_trim"
                self.oms.save("EXIT_REQUEST", coin=coin, reason=book["exit_reason"])
                return fills
            if not trim_deferred:
                purpose = trim.get("purpose") or "profit"
                required_price = None
                if purpose == "profit":
                    required_price = self._profit_price(market, trim)
                    quote = self._sell_quote(market, trim_qty, required_price)
                    if quote is None or quote["gross"] < minimum:
                        self.counts["profit_trim_not_executable"] += 1
                        trim_deferred = True
            if not trim_deferred:
                if buy_orders:
                    return self._cancel(buy_orders)
                if protect:
                    book["inventory_phase"] = "protection_canceling"
                    self.oms.save("INVENTORY_PHASE", coin=coin, phase=book["inventory_phase"])
                    return self._cancel([protect])
                active_trim = next((order for order in sell_orders if order["role"] == "trim"), None)
                if active_trim:
                    book["inventory_phase"] = "selling"
                    return fills
                if sell_orders:
                    return fills
                if self.oms.available_asset(coin) < trim_qty:
                    self.oms.state["account_at"] = 0
                    self.last_account = 0
                    trim_deferred = True
                else:
                    fields = dict(
                        lot=trim.get("lot"),
                        requested_scope=trim.get("requested_scope"),
                        reason="take_profit" if purpose == "profit" else "strategy_risk_trim",
                        purpose=purpose,
                    )
                    if required_price is not None:
                        fields["required_price"] = required_price
                        fields["limit_price"] = required_price
                        fields["minimum_gross"] = minimum
                    book["inventory_phase"] = "selling"
                    self.oms.save("INVENTORY_PHASE", coin=coin, phase=book["inventory_phase"])
                    self._submit(coin, "trim", "SELL", "MARKET", trim_qty, taker, **fields)
                    return fills
            if trim_deferred:
                book["inventory_phase"] = "reprotecting"
                self.oms.save("TRIM_DEFERRED_REPROTECT", coin=coin, quantity=str(qty))
        active_trim = next((order for order in sell_orders if order["role"] == "trim"), None)
        if active_trim:
            book["inventory_phase"] = "reprotecting"
            return self._cancel([active_trim])

        qty = self.oms.quantity(coin)
        protect = next((order for order in self.oms.active(coin, "protect")), None)
        if qty:
            if trigger is None or limit is None or qty < minimum_qty or qty * limit < minimum:
                book["exit_reason"] = "protection_below_minimum"
                self.oms.save("EXIT_REQUEST", coin=coin, reason=book["exit_reason"])
                return fills
            if protect and protect.get("status") != "NOT_TRIGGERED":
                if self.clock() - protect["created"] >= self.config["reconcile_halt_s"]:
                    book["exit_reason"] = "protection_unconfirmed"
                    self.oms.save("EXIT_REQUEST", coin=coin, reason=book["exit_reason"])
                    return self._cancel([protect])
                return fills
            if protect and not self._confirmed_protection(
                protect, qty=qty, price=limit, trigger=trigger,
            ):
                book["inventory_phase"] = "protection_canceling"
                return self._cancel([protect])
            if protect is None:
                if sell_orders:
                    return fills
                if bid is not None and bid <= trigger:
                    book["exit_reason"] = "stop_without_protection"
                    self.oms.save("EXIT_REQUEST", coin=coin, reason=book["exit_reason"])
                    return fills
                if self.oms.available_asset(coin) < qty:
                    self.oms.state["account_at"] = 0
                    self.last_account = 0
                    return fills
                book["inventory_phase"] = "reprotecting"
                self.oms.save("INVENTORY_PHASE", coin=coin, phase=book["inventory_phase"])
                self._submit(
                    coin, "protect", "SELL", "STOP_LIMIT", qty, max(maker, taker),
                    price=limit, trigger_price=trigger,
                )
                return fills
            book["inventory_phase"] = "protected"

        wanted_buy = None if trim_deferred else desired.get("buy") if desired else None
        if wanted_buy is None or stopping or self.recovery_only:
            if buy_orders:
                return self._cancel(buy_orders)
            return fills
        price, wanted_qty = wanted_buy
        wanted_qty = decimal(wanted_qty)
        if buy_orders:
            order = buy_orders[0]
            maintained_qty = min(
                wanted_qty,
                self._buy_cap(coin, price, maker, exclude_order=order),
            )
            if (
                maintained_qty < minimum_qty
                or maintained_qty * price < minimum
                or not self._same(order, qty=maintained_qty, price=price)
            ):
                return self._cancel(buy_orders)
            reason = self.validate_intent(
                coin, "buy", "BUY", maintained_qty,
                fee_rate=maker, price=price, own_order=order,
            )
            if reason:
                self.counts["resting_buy_rejected_" + reason] += 1
                return self._cancel(buy_orders)
            return fills
        buy_qty = min(wanted_qty, self._buy_cap(coin, price, maker))
        if buy_qty < minimum_qty or buy_qty * price < minimum:
            return fills
        if qty and not self._confirmed_protection(
            protect, qty=qty, price=limit, trigger=trigger,
        ):
            return fills
        if self.oms.can_buy(
            coin, buy_qty, price, maker, minimum, self.marks(),
            exit_fee_rate=taker, current_bid=bid,
        ):
            self._submit(coin, "buy", "BUY", "LIMIT", buy_qty, maker, price=price)
        return fills

    def shutdown_ready(self):
        for coin in set(self.oms.state["books"]) | set(self.markets):
            qty = self.oms.quantity(coin)
            active = self.oms.active(coin)
            if any(order["role"] != "protect" for order in active):
                return False
            if qty:
                protects = [order for order in active if order["role"] == "protect"]
                book = self.oms.book(coin)
                if (
                    len(protects) != 1
                    or book.get("desired_stop") is None
                    or book.get("stop_limit") is None
                    or not self._confirmed_protection(
                        protects[0], qty=qty,
                        price=book["stop_limit"], trigger=book["desired_stop"],
                    )
                ):
                    return False
        return True

    def report(self):
        now_ms = time.time_ns() // 1_000_000
        if self.raw:
            self.raw.flush()
        try:
            public = self.directory / "public"
            used = sum(path.stat().st_size for path in public.glob("*.jsonl.gz")) if public.exists() else 0
            free = shutil.disk_usage(self.directory).free
            self.storage_ok = used < self.config["public_storage_max_bytes"] and free > 1024 ** 3
        except OSError:
            used, free, self.storage_ok = 0, 0, False
        if not self.storage_ok:
            self.oms.halt("STORAGE_CAPACITY")
        positions = {
            coin: dict(
                quantity=str(self.oms.quantity(coin)),
                average=book.get("avg"),
                selected=book.get("selected"),
                wind_down=book.get("wind_down"),
                desired_stop=book.get("desired_stop"),
                stop_limit=book.get("stop_limit"),
                exit_reason=book.get("exit_reason"),
                campaign_id=book.get("campaign_id"),
                inventory_phase=book.get("inventory_phase"),
                campaign_realized_krw=book.get("campaign_realized"),
                campaign_budget_krw=book.get("campaign_budget"),
            )
            for coin, book in self.oms.state["books"].items()
            if self.oms.quantity(coin) or self.oms.active(coin)
        }
        result = dict(
            t_ms=now_ms,
            track="A-2",
            execution_version=EXECUTION_VERSION,
            mode=self.config["mode"],
            account_mode=(
                "isolated" if self.config["portfolio_isolation_required"]
                else "shared_owner_approved"
            ),
            capital_allocation_krw=self.config["capital_allocation_krw"],
            cash_reserve_krw=self.config["cash_reserve_krw"],
            shared_reserved_symbols=self.config["shared_reserved_symbols"],
            approval_mode=(
                "owner_unvalidated"
                if self.config["owner_unvalidated_live_approved"] else "evaluated"
            ),
            recovery_only=self.recovery_only,
            connected=self.connected,
            private_connected=self.private_connected,
            entry_paused=bool(self.activation_reasons()),
            activation_blocks=self.activation_reasons(),
            selected=self.oms.state["selected"],
            coverage_reasons=self.coverage_reasons,
            sizing_reasons=self.sizing_reasons,
            foreign_assets=self.oms.state.get("foreign_assets", []),
            foreign_order_coins=self.oms.state.get("foreign_order_coins", []),
            positions=positions,
            active_orders=[
                dict(
                    cid=order["cid"], coin=order["coin"], role=order["role"],
                    side=order["side"], type=order["type"], status=order["status"],
                    qty=order["qty"], filled=order["filled"],
                )
                for order in self.oms.active()
            ],
            equity_krw=str(self.oms.equity(self.marks())),
            free_cash_krw=str(self.oms.free_cash()),
            realized_krw=self.oms.state["realized"],
            day_realized_krw=self.oms.state["day_realized"],
            day_equity_pnl_krw=str(self.oms.day_pnl(self.marks())),
            day_external_flows_krw=self.oms.state.get("day_external_flows", "0"),
            day_stops=self.oms.state["day_stops"],
            halt=self.oms.state["halt"],
            storage=dict(ok=self.storage_ok, public_bytes=used, free_bytes=free),
            counts=dict(self.counts),
        )
        path = self.directory / "status.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(encoded(result) + "\n", encoding="utf-8")
        temporary.replace(path)
        self.last_report = self.clock()

    async def run(self, seconds=None):
        self.loop = asyncio.get_running_loop()
        tasks = []
        scan_task = None
        try:
            await self.scan()
            await self.refresh_account()
            if not self.markets:
                raise RuntimeError("Track A-2 has no eligible observable markets")
            self.store.event(
                "START",
                execution_version=EXECUTION_VERSION,
                recovery_only=self.recovery_only,
                config_digest=hashlib.sha256(self.config_path.read_bytes()).hexdigest(),
                selected=self.oms.state["selected"],
            )
            tasks = [
                asyncio.create_task(self.public_feed()),
                asyncio.create_task(private_follow(self)),
            ]
            started = time.monotonic()
            while True:
                self.wakeup.clear()
                if scan_task is not None and scan_task.done():
                    try:
                        self._apply_scan(scan_task.result())
                    except (CoinoneError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                        self.counts["scan_errors"] += 1
                        self.last_scan = self.clock()
                        self.store.event("SCAN_ERROR", error=type(exc).__name__)
                    finally:
                        scan_task = None
                if seconds is not None and time.monotonic() - started >= seconds:
                    self.stopping = True
                if self.controls()["stop"]:
                    self.stopping = True
                force = self.counts["private_order_events"] != self.last_private_events
                self.last_private_events = self.counts["private_order_events"]
                try:
                    fills, changed = await asyncio.to_thread(self.reconcile, force=force)
                    self.notify_fills(fills)
                    self.oms.roll_day(self.marks())
                    if fills or changed or self.clock() - self.last_account >= self.config["account_poll_s"]:
                        await self.refresh_account(reconcile=False)
                    now_ms = time.time_ns() // 1_000_000
                    for coin in list(self.markets):
                        market = self.markets[coin]
                        fresh = bool(self.connected and market.fresh(now_ms))
                        desired = self.desired(coin, now_ms) if fresh else None
                        if not fresh:
                            market.drain()
                        fills = await asyncio.to_thread(
                            self.drive, coin, desired, fresh=fresh, stopping=self.stopping
                        )
                        self.notify_fills(fills)
                        if fills:
                            self.oms.state["account_at"] = 0
                            self.last_account = 0
                        if self.oms.finish_flat(coin):
                            self.strategies.pop(coin, None)
                            self.strategy_params.pop(coin, None)
                except CoinoneError as exc:
                    self.counts["api_errors"] += 1
                    self.oms.state["account_at"] = 0
                    self.last_account = 0
                    self.store.event("API_ERROR", error=str(exc))
                    cancellable = [
                        order for order in self.oms.active()
                        if order["role"] in ("buy", "trim")
                    ]
                    if cancellable:
                        try:
                            self.notify_fills(await asyncio.to_thread(self._cancel, cancellable))
                        except (CoinoneError, RuntimeError):
                            self.counts["risk_cancel_errors"] += 1
                if self.stopping and self.shutdown_ready():
                    break
                if (
                    not self.stopping
                    and scan_task is None
                    and self.clock() - self.last_scan >= self.config["scan_seconds"]
                ):
                    # Discovery and metadata REST calls must not hold up stop,
                    # protection, cancellation, or account reconciliation.
                    self.last_scan = self.clock()
                    scan_task = asyncio.create_task(self._discover())
                if self.clock() - self.last_report >= 30:
                    self.report()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), self.config["decision_ms"] / 1000)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.stopping = True
            deadline = time.monotonic() + 10
            while self.markets and not self.shutdown_ready() and time.monotonic() < deadline:
                try:
                    fills, _ = await asyncio.to_thread(self.reconcile, force=True)
                    self.notify_fills(fills)
                    await self.refresh_account(reconcile=False)
                    for coin in list(self.markets):
                        await asyncio.to_thread(
                            self.drive, coin, None, fresh=False, stopping=True
                        )
                except (CoinoneError, RuntimeError, OSError):
                    self.counts["shutdown_errors"] += 1
                if not self.shutdown_ready():
                    await asyncio.sleep(.25)
            for task in tasks:
                task.cancel()
            if scan_task is not None:
                scan_task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if scan_task is not None:
                await asyncio.gather(scan_task, return_exceptions=True)
            try:
                self.report()
            finally:
                if self.raw:
                    self.raw.close()
                self.store.close()
                if self.http_pool:
                    self.http_pool.close()
