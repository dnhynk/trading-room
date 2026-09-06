"""Run recorded Coinone messages through the live Track A-2 Runtime and OMS."""
import copy
from collections import Counter
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile

from track_a_2.execution.store import Store
from track_a_2.market.feed import Market
from track_a_2.market.select import ranked, retain
from track_a_2.replay.loader import Observation
from track_a_2.replay.sim import ReplayClock, SimClient
from track_a_2.runtime import Runtime
from track_c.execution.coinone import decimal


def _book(row):
    bids = sorted(
        ((decimal(level["price"], positive=True), decimal(level["qty"])) for level in row.get("bids", []) if decimal(level["qty"]) > 0),
        reverse=True,
    )
    asks = sorted(
        (decimal(level["price"], positive=True), decimal(level["qty"])) for level in row.get("asks", []) if decimal(level["qty"]) > 0
    )
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return None
    return dict(
        bids=[dict(price=str(price), qty=str(qty)) for price, qty in bids],
        asks=[dict(price=str(price), qty=str(qty)) for price, qty in asks],
    )


def liquidation_equity(client, markets, taker_fee, depth_fraction):
    value = client.cash
    for coin, qty in client.assets.items():
        if qty <= 0:
            continue
        remaining = qty
        gross = D(0)
        market = markets.get(coin)
        for level in ((market.book if market else None) or {}).get("bids", [])[:5]:
            take = min(remaining, decimal(level["qty"]) * depth_fraction)
            gross += take * decimal(level["price"])
            remaining -= take
            if remaining <= 0:
                break
        # Unseen liquidation depth is worth zero in the conservative NAV.
        value += gross * (D(1) - taker_fee[coin])
    return value


def _events(store):
    return [
        (kind, json.loads(body))
        for kind, body in store.db.execute("SELECT kind,body FROM events ORDER BY seq")
    ]


def run_observation(
    observation, config, *, capital="1000000", maker_fee="0", taker_fee="0",
    latency_ms=250, depth_fraction=None, strategy=None,
):
    """Replay one observation with the production strategy/runtime/OMS boundary.

    Public messages retain recorded receive/exchange times. Maker buys use queue
    ahead and cancel races; market/protective sells consume a configurable
    fraction of the recorded top-five bid depth after the configured latency.
    """
    observation = observation if isinstance(observation, Observation) else Observation(observation)
    replay_config = copy.deepcopy(config)
    if strategy:
        replay_config["strategy"].update(strategy)
    observed = list(observation.manifest["coins"])
    replay_config.update(
        status="active", mode="live", execution_enabled=True,
        portfolio_isolation_confirmed=True,
        universe=observed,
        basket_size=min(replay_config["basket_size"], len(observed)),
        max_open_books=min(replay_config["max_open_books"], len(observed)),
        state_directory="../trading-room-state/track-a-2",
    )
    if not replay_config["basket_size"] or not replay_config["max_open_books"]:
        raise ValueError("observation has no replayable markets")
    depth_fraction = decimal(
        replay_config["depth_fraction"] if depth_fraction is None else depth_fraction,
        positive=True,
    )
    latency_ms = int(latency_ms)
    if latency_ms < 0 or depth_fraction > 1:
        raise ValueError("invalid replay latency or depth fraction")
    fee_rows = {
        coin: {"maker": str(decimal(maker_fee)), "taker": str(decimal(taker_fee))}
        for coin in observed
    }
    if any(decimal(value) >= 1 for row in fee_rows.values() for value in row.values()):
        raise ValueError("invalid replay fee rate")
    maker_rows = {coin: decimal(row["maker"]) for coin, row in fee_rows.items()}
    taker_rows = {coin: decimal(row["taker"]) for coin, row in fee_rows.items()}
    seed_markets = observation.seed["markets"]
    contracts = [seed_markets[coin]["contract"] for coin in observed]
    tickers = [seed_markets[coin]["ticker"] for coin in observed]
    candidates, reasons = ranked(contracts, tickers, replay_config)
    plan = retain([], set(), candidates, replay_config["basket_size"])

    temporary = tempfile.TemporaryDirectory()
    store = None
    try:
        parent = Path(temporary.name)
        root = parent / "trading-room"
        config_path = root / "track_a_2" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps(replay_config), encoding="utf-8")
        (root / "config").mkdir()
        (root / "config" / "tracks.json").write_text(
            json.dumps(dict(tracks={"A-2": {"status": "active", "execution_enabled": True}})),
            encoding="utf-8",
        )
        clock = ReplayClock(observation.seed["captured_ms"] / 1000)
        client = SimClient(
            capital, fee_rows, clock=clock, latency_ms=latency_ms,
            depth_fraction=str(depth_fraction),
        )
        store = Store(parent / "trading-room-state" / "track-a-2")
        runtime = Runtime(
            replay_config, config_path=config_path, root=root,
            client=client, store=store, clock=clock,
        )
        runtime.submission_guard = lambda *args, **kwargs: (lambda: None)
        runtime.connected = runtime.private_connected = runtime.storage_ok = True
        runtime.markets = {}
        for coin in plan["watch"]:
            seed = seed_markets[coin]
            candles = seed["candles"]
            market = Market(
                coin, replay_config, seed["contract"], seed["units"], fee_rows[coin],
                candles.get("1m", ()), candles.get("15m", ()), candles.get("1d", ()),
                now_ms=observation.seed["captured_ms"],
            )
            market.book = _book(seed["orderbook"])
            if market.book:
                market.book_received = market.book_exchange = observation.seed["captured_ms"]
            runtime.markets[coin] = market
            client.set_book(coin, market.book)
        runtime.oms.sync_account(
            client.balances(), client.active_orders(),
            {coin: market.contract["qty_unit"] for coin, market in runtime.markets.items()},
        )
        runtime.oms.set_selection(plan, reasons)
        steps = {
            name: market.contract["qty_unit"]
            for name, market in runtime.markets.items()
        }
        peak = decimal(capital)
        drawdown = D(0)
        observations = 0
        first_ms = last_ms = None
        held_ms = 0
        previous_ms = observation.seed["captured_ms"]
        previously_held = False

        def account(now_ms, *, advance=True):
            clock.set_ms(now_ms)
            if advance:
                client.advance()
            fills, changed = runtime.reconcile(force=True)
            runtime.notify_fills(fills)
            # Match the live loop's order: establish the day-open mark before
            # account refresh can observe a new UTC date.
            runtime.oms.roll_day(runtime.marks())
            if (
                fills
                or changed
                or runtime.clock() - runtime.last_account >= replay_config["account_poll_s"]
            ):
                runtime.oms.sync_account(client.balances(), client.active_orders(), steps)
                runtime.last_account = runtime.clock()

        def decide(now_ms):
            for name, market in list(runtime.markets.items()):
                fresh = market.fresh(now_ms)
                desired = runtime.desired(name, now_ms) if fresh else None
                if not fresh:
                    market.drain()
                runtime.notify_fills(runtime.drive(name, desired, fresh=fresh))
            # Zero-latency scenarios can settle on the same decision boundary;
            # positive latency remains pending until a later event or tick.
            account(now_ms)
            for name in runtime.markets:
                if runtime.oms.finish_flat(name):
                    runtime.strategies.pop(name, None)
                    runtime.strategy_params.pop(name, None)

        def measure(now_ms):
            nonlocal peak, drawdown, held_ms, previous_ms, previously_held
            if previously_held:
                held_ms += max(0, now_ms - previous_ms)
            nav = liquidation_equity(client, runtime.markets, taker_rows, depth_fraction)
            peak = max(peak, nav)
            drawdown = max(drawdown, peak - nav)
            previous_ms = now_ms
            previously_held = any(qty > 0 for qty in client.assets.values())

        interval = int(replay_config["decision_ms"])
        next_tick = observation.seed["captured_ms"] + interval
        for received_ms, message in observation.rows():
            while next_tick < received_ms:
                account(next_tick)
                decide(next_tick)
                measure(next_tick)
                next_tick += interval
            clock.set_ms(received_ms)
            client.advance()
            first_ms = received_ms if first_ms is None else first_ms
            last_ms = received_ms
            data = message.get("data") or {}
            coin = data.get("target_currency")
            channel = message.get("channel")
            changed = False
            tick_due = next_tick == received_ms
            if message.get("response_type") == "DATA" and coin in runtime.markets:
                market = runtime.markets[coin]
                revision = market.revision
                if channel == "TRADE":
                    # The exchange can fill an existing order even when this
                    # process later rejects a duplicate or delayed feed event.
                    client.on_trade(coin, data)
                    market.feed(channel, data, received_ms)
                    client.on_book(coin, market.book)
                elif channel == "ORDERBOOK":
                    market.feed(channel, data, received_ms)
                    client.on_book(coin, market.book)
                changed = market.revision != revision
            account(received_ms, advance=False)
            if changed or tick_due:
                decide(received_ms)
            measure(received_ms)
            while next_tick <= received_ms:
                next_tick += interval
            observations += 1
        nav = liquidation_equity(client, runtime.markets, taker_rows, depth_fraction)
        events = _events(store)
        fills = [body for kind, body in events if kind == "FILL" and decimal(body.get("qty") or 0) > 0]
        signal_counts = Counter()
        arm_counts = Counter()
        for kind, body in events:
            if kind == "SIGNAL":
                signal = body.get("signal") or {}
                signal_counts[f"{signal.get('sig', 'unknown')}:{signal.get('src', 'unknown')}"] += 1
            elif kind == "STRATEGY_ARM":
                labels = sorted(
                    f"{cause.get('sig', 'unknown')}:{cause.get('src', 'unknown')}"
                    for cause in body.get("causes") or []
                )
                arm_counts["+".join(labels) or "state_only"] += 1
        flat = sum(kind == "FLAT" for kind, _ in events)
        open_campaigns = sum(bool(book.get("campaign_open")) for book in runtime.oms.state["books"].values())
        campaigns = flat + open_campaigns
        result = dict(
            initial_equity_krw=str(decimal(capital)),
            liquidation_equity_krw=str(nav),
            net_pnl_krw=str(nav - decimal(capital)),
            realized_pnl_krw=runtime.oms.state["realized"],
            max_drawdown_krw=str(drawdown),
            campaigns=campaigns,
            cycles=sum(fill["role"] == "trim" for fill in fills),
            buys=sum(fill["role"] == "buy" for fill in fills),
            stops=sum(
                fill["role"] in ("protect", "exit")
                or fill["role"] == "trim" and fill.get("reason") == "strategy_risk_trim"
                for fill in fills
            ),
            fills=len(fills),
            open_inventory={coin: str(qty) for coin, qty in client.assets.items() if qty > 0},
            active_orders=len(client.active_orders()),
            messages=observations,
            duration_ms=0 if first_ms is None else last_ms - first_ms,
            time_in_market_fraction=(
                0.0 if first_ms is None or last_ms == first_ms
                else held_ms / (last_ms - first_ms)
            ),
            selected=plan["selected"],
            selection_reasons=reasons,
            halt=runtime.oms.state["halt"],
            data_digest=observation.data_digest(),
            execution=dict(
                maker_fee={coin: str(value) for coin, value in maker_rows.items()},
                taker_fee={coin: str(value) for coin, value in taker_rows.items()},
                latency_ms=int(latency_ms),
                depth_fraction=str(depth_fraction),
            ),
            signal_counts=dict(sorted(signal_counts.items())),
            arm_counts=dict(sorted(arm_counts.items())),
        )
        return result
    finally:
        if store is not None:
            store.close()
        temporary.cleanup()
