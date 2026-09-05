"""Coinone scanner + existing causal signals + persistent single-campaign OMS."""
import argparse
import asyncio
from collections import Counter
from decimal import Decimal as D
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import signal
import time
import urllib.request

from .coinone import CoinoneError, Credentials, decimal
from .execution import CoinoneExecution
from .marketdata import Market
from .oms import OMS
from .settings import load
from .sizing import size_order
from .store import Store, encoded


def entry_signal_current(market, sig, received, now, limit):
    """Re-evaluate after awaited I/O, including the original completed second."""
    return (market.fresh(now)
            and 0 <= now-received <= limit
            and 0 <= now-(sig["t"]+1)*1000 <= limit
            and 0 <= now-(market.features.f.get("t", -2)+1)*1000 <= limit
            and market.features.bid <= sig["mid"]
            and market.features.f.get("bs10", 0) > .5
            and market.features.f.get("v", -1) > -1)


class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.directory = Path(cfg["data_directory"])
        self.store = Store(self.directory)
        self.client = CoinoneExecution(Credentials.read(cfg["env_path"], profile=cfg["credential_profile"]), timeout=5)
        if cfg.get('policy') in ('quantitative','rule'):
            from .portfolio import Portfolio
            self.oms=Portfolio(cfg,self.client,self.store)
        else:
            self.oms = OMS(cfg, self.client, self.store)
        self.markets, self.foreign_assets = {}, set(cfg["excluded_symbols"])
        self.account_available, self.account_reserved, self.account_at = D(0), D(0), 0
        self.connected, self.stopping, self.generation = False, False, 0
        self.queue = asyncio.Queue(maxsize=200)
        self.counts = Counter()
        self.last_scan = self.last_account = self.last_report = 0
        self.raw, self.raw_hour, self.storage_ok = None, None, True

    async def refresh_account(self):
        rows = await asyncio.to_thread(self.client.balances)
        orders = await asyncio.to_thread(self.client.active_orders)
        krw = [r for r in rows if r.get("currency") == "KRW"]
        if len(krw) != 1:
            self.account_at = 0
            raise CoinoneError("KRW balance missing or ambiguous")
        self.account_available = decimal(krw[0]["available"])
        self.account_reserved = decimal(krw[0]["limit"])
        current = self.oms.campaign["coin"] if self.oms.campaign else None
        self.foreign_assets = set(self.cfg["excluded_symbols"])
        for row in rows:
            if row["currency"] != "KRW" and row["currency"] != current and decimal(row["available"])+decimal(row["limit"]) > 0:
                self.foreign_assets.add(row["currency"])
        own_ids = set(self.oms.state["orders"])
        own_exchange_ids = {o.get("exchange_id") for o in self.oms.state["orders"].values() if o.get("exchange_id")}
        for order in orders:
            if order.get("user_order_id") in own_ids or order.get("order_id") in own_exchange_ids:
                continue
            self.foreign_assets.add(order["target_currency"])
            if str(order.get("user_order_id", "")).startswith("tc-"):
                self.oms.halt("UNJOURNALED_TRACK_C_ORDER")
        self.oms.sync_cash(self.account_available+self.account_reserved)
        self.account_at = self.last_account = time.time()

    def cash(self):
        if not self.cfg["funding_confirmed"]:
            return D(0)
        return max(D(0), min(D(self.oms.state["cash_krw"]), self.account_available))

    async def scan(self):
        contracts, tickers = await asyncio.to_thread(self.client.universe)
        eligible = {r["target_currency"]:r for r in contracts if r.get("trade_status") == 1 and r.get("maintenance_status") == 0 and {"limit", "market", "stop_limit"} <= set(r.get("order_types", []))}
        ranks = [r["target_currency"] for r in sorted(tickers, key=lambda r:decimal(r.get("quote_volume") or 0), reverse=True)]
        desired = []
        current = self.oms.campaign["coin"] if self.oms.campaign else None
        for coin in ([current] if current else [])+self.cfg["benchmark_symbols"]+ranks:
            if coin in eligible and coin not in desired and (coin == current or coin not in self.foreign_assets):
                desired.append(coin)
            if len(desired) >= self.cfg["max_symbols"]:
                break
        added = {}
        for coin in desired:
            try:
                fees = await asyncio.to_thread(self.client.fees, coin)
                units = await asyncio.to_thread(self.client.price_units, coin)
                candles = await asyncio.to_thread(self.client.candles, coin)
                if coin in self.markets:
                    market = self.markets[coin]
                    market.fees, market.units, market.contract = fees, units, eligible[coin]
                    market.seed(candles)
                else:
                    market = Market(coin, self.cfg, eligible[coin], units, fees, candles)
                added[coin] = market
            except (CoinoneError, ValueError, KeyError, TypeError):
                self.counts["scan_market_error"] += 1
                if coin == current and coin in self.markets:
                    added[coin] = self.markets[coin]
        changed = set(added) != set(self.markets)
        self.markets = added
        if changed:
            self.generation += 1
        self.last_scan = time.time()
        self.store.event("SCAN", symbols=list(added), excluded=sorted(self.foreign_assets))

    async def feed(self):
        from websockets.asyncio.client import connect
        backoff = 1
        while not self.stopping:
            if not self.markets:
                await asyncio.sleep(1)
                continue
            generation = self.generation
            expected = {(coin, ch) for coin in self.markets for ch in ("ORDERBOOK", "TRADE")}
            try:
                async with connect("wss://stream.coinone.co.kr", open_timeout=10, ping_interval=15, ping_timeout=15, close_timeout=3, max_queue=2048) as ws:
                    for coin, channel in sorted(expected):
                        await ws.send(encoded(dict(request_type="SUBSCRIBE", channel=channel, topic=dict(quote_currency="KRW", target_currency=coin))))
                    last_ping = time.monotonic()
                    while not self.stopping and generation == self.generation:
                        if time.monotonic()-last_ping >= 15:
                            await ws.send('{"request_type":"PING"}')
                            last_ping = time.monotonic()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 2)
                        except asyncio.TimeoutError:
                            continue
                        recv = time.time_ns()//1_000_000
                        message = json.loads(raw)
                        response = message.get("response_type")
                        if response == "ERROR":
                            raise CoinoneError("public subscription rejected")
                        data, channel = message.get("data") or {}, message.get("channel")
                        coin = data.get("target_currency")
                        if response == "SUBSCRIBED":
                            expected.discard((coin, channel))
                            self.connected = not expected
                        if response != "DATA" or coin not in self.markets:
                            continue
                        self.record(recv, message)
                        for sig in self.markets[coin].feed(channel, data, recv):
                            if self.queue.full():
                                self.queue.get_nowait()
                                self.counts["dropped_signal"] += 1
                            self.queue.put_nowait((coin, sig, recv))
                        self.counts["messages"] += 1
                    backoff = 1
            except (OSError, CoinoneError, ValueError, TimeoutError):
                self.counts["ws_errors"] += 1
            except Exception:
                # Library connection errors must not print response/header objects.
                self.counts["ws_errors"] += 1
            finally:
                self.connected = False
                # Rebuild normalization state after any disconnect. Quiet periods
                # must not warm sigma using repeated stale observations.
                for m in self.markets.values() if generation == self.generation else []:
                    rows = m.features.candles
                    from bot.signal import Features
                    m.features = Features(self.cfg["signal"])
                    m.features.seed_candles(rows)
                    m.received = m.exchange_ms = 0
            if not self.stopping:
                await asyncio.sleep(backoff)
                backoff = min(backoff*2, 15)

    async def candle_poll(self):
        while not self.stopping:
            await asyncio.sleep(30)
            for m in list(self.markets.values()):
                try:
                    rows = await asyncio.to_thread(self.client.candles, m.coin)
                    m.seed(rows)
                except (CoinoneError, ValueError, KeyError, TypeError):
                    self.counts["candle_errors"] += 1

    def record(self, recv, message):
        if not self.storage_ok:
            return
        hour = time.strftime("%Y%m%d-%H", time.gmtime(recv/1000))
        if hour != self.raw_hour:
            if self.raw:
                self.raw.close()
            folder = self.directory/"public"
            folder.mkdir(exist_ok=True)
            self.raw = gzip.open(folder/(hour+".jsonl.gz"), "at", encoding="utf-8")
            self.raw_hour = hour
        self.raw.write(encoded(dict(received_ms=recv, message=message))+"\n")

    def report(self):
        now = int(time.time()*1000)
        if self.raw:
            self.raw.flush()
        size = sum(p.stat().st_size for p in (self.directory/"public").glob("*.gz"))
        self.storage_ok = size < self.cfg.get('public_storage_max_bytes',512*1024**2) and shutil.disk_usage(self.directory).free > 1024**3
        storage = None
        if self.cfg.get('policy')=='rule':
            from .storage import capacity
            storage = capacity(self.directory,self.cfg['public_storage_max_bytes'])
            self.storage_ok = storage['ok']
        result = dict(t_ms=now, mode=self.cfg["mode"], connected=self.connected,
                      funding_confirmed=self.cfg["funding_confirmed"], capital_krw=str(self.oms.equity),
                      capital_mode=self.cfg["capital_mode"], account_krw_available=str(self.account_available),
                      account_krw_reserved=str(self.account_reserved), order_cash_available=str(self.cash()),
                      external_capital_flows=self.oms.state["external_flows"], initial_equity=self.oms.state["initial_equity"],
                      position=self.oms.campaign, halt=self.oms.state["halt"], realized=self.oms.state["realized"],
                      today_realized=self.oms.state["day_realized"], counts=dict(self.counts), storage_ok=self.storage_ok,
                      markets={c:dict(fresh=m.fresh(now), counts=m.counts, features=m.features.f if m.features is not None else {}, fees=m.fees) for c,m in self.markets.items()})
        if storage is not None: result['storage']=storage
        tmp = self.directory/"status.tmp"
        tmp.write_text(encoded(result)+"\n", encoding="utf-8")
        tmp.replace(self.directory/"status.json")
        print(encoded(dict(kind="HEARTBEAT", mode=self.cfg["mode"], connected=self.connected, symbols=list(self.markets), funding_confirmed=self.cfg["funding_confirmed"], position=bool(self.oms.campaign), halt=self.oms.state["halt"])), flush=True)
        self.last_report = time.time()

    async def run(self, seconds=None):
        egress = await asyncio.to_thread(lambda: urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip())
        if egress != self.cfg["expected_egress_ip"]:
            raise RuntimeError("server egress does not match the authorized Coinone IP")
        await self.refresh_account()
        await self.scan()
        if not self.markets:
            raise RuntimeError("no observable markets")
        self.store.event("START", config=self.cfg, code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        print(encoded(dict(kind="START", mode=self.cfg["mode"], symbols=list(self.markets), funding_confirmed=self.cfg["funding_confirmed"])), flush=True)
        feed = asyncio.create_task(self.feed())
        candles = asyncio.create_task(self.candle_poll())
        start = time.monotonic()
        try:
            while True:
                now = int(time.time()*1000)
                if seconds is not None and time.monotonic()-start >= seconds:
                    self.stopping = True
                if (self.directory/"STOP").exists():
                    self.stopping = True
                c = self.oms.campaign
                market = self.markets.get(c["coin"]) if c else None
                opposite = False
                candidates = []
                while not self.queue.empty():
                    coin, sig, received = self.queue.get_nowait()
                    self.counts["signals"] += 1
                    self.store.event("SIGNAL", coin=coin, signal=sig, received=received,
                                     pre_gate=not sig.get("shadow") and sig["sig"] == "DIP_SLOWING",
                                     no_decay=sig.get("src") == "v" and sig["sig"] == "DIP_SLOWING" and sig.get("bs10", 0) > .5,
                                     strict=sig.get("src") == "v" and sig["sig"] == "DIP_SLOWING" and sig.get("bs10", 0) > .5 and bool(sig.get("sell_decay")))
                    recent = 0 <= now-received <= self.cfg["quote_max_age_ms"] and 0 <= now-(sig["t"]+1)*1000 <= self.cfg["quote_max_age_ms"]
                    if recent and c and coin == c["coin"] and sig["sig"] == "POP_STALLING" and sig.get("src") == "v":
                        opposite = True
                    if recent and not sig.get("shadow") and sig["sig"] == "DIP_SLOWING" and sig.get("src") == "v" and sig.get("bs10", 0) > .5:
                        candidates.append((coin, sig, received))
                try:
                    await asyncio.to_thread(self.oms.drive, bid=market.features.bid if market else None,
                                            fresh=bool(market and self.connected and market.fresh(now)),
                                            feature=market.features.f if market else None, opposite=opposite, stopping=self.stopping)
                    if self.stopping and not self.oms.campaign and not self.oms.active():
                        break
                    if time.time()-self.last_account >= 15:
                        await self.refresh_account()
                    if not self.oms.campaign and time.time()-self.last_scan >= self.cfg["scan_seconds"]:
                        await self.scan()
                    for coin, sig, received in candidates:
                        m = self.markets.get(coin)
                        can_submit = self.cfg["mode"] == "live" and self.cfg["funding_confirmed"] and not (self.directory/"PAUSE").exists()
                        if can_submit and not self.oms.campaign and not self.stopping:
                            # Account I/O may consume the whole signal lifetime.
                            # Build the size from current depth only AFTER it ends.
                            await self.refresh_account()
                        now = int(time.time()*1000)
                        if self.stopping or self.oms.campaign or not m or coin in self.foreign_assets or not self.connected or not self.storage_ok:
                            continue
                        if not entry_signal_current(m, sig, received, now, self.cfg["quote_max_age_ms"]):
                            self.counts["expired_signal_refusal"] += 1
                            continue
                        if not m.features.candles or now-m.features.candles[-1]["ts"]-60000 > 90000:
                            self.counts["stale_candle_refusal"] += 1
                            continue
                        vol, count = m.volume(now)
                        if count < self.cfg["min_trades_10s"]:
                            self.counts["sparse_trade_refusal"] += 1
                            continue
                        if any(D(v) for v in m.fees.values()):
                            self.counts["nonzero_fee_refusal"] += 1
                            continue
                        cash = self.cash() if self.cfg["funding_confirmed"] else self.oms.equity
                        plan = size_order(self.cfg, market=m.contract, units=m.units, book=m.book, feature=sig, fees=m.fees,
                                          equity=self.oms.equity, cash=cash, daily_remaining=self.oms.remaining_risk(), volume_10s=vol)
                        self.store.event("SIZE", coin=coin, plan=plan, hypothetical=not self.cfg["funding_confirmed"])
                        self.counts["size_"+(plan.get("reason") or "eligible")] += 1
                        if plan.get("reason") or not can_submit or (self.directory/"PAUSE").exists():
                            continue
                        if D(plan["notional_krw"]) > self.cash():
                            continue
                        await asyncio.to_thread(self.oms.enter, coin, plan, sig, m.contract["min_order_amount"])
                except CoinoneError as exc:
                    self.counts["account_errors"] += 1
                    self.account_at = 0
                    self.store.event("API_ERROR", error=str(exc))
                    if self.oms.campaign:
                        self.oms.request_exit("account_error")
                if time.time()-self.last_report >= 30:
                    self.report()
                await asyncio.sleep(.5 if self.oms.campaign else .2)
        finally:
            self.stopping = True
            feed.cancel()
            candles.cancel()
            await asyncio.gather(feed, candles, return_exceptions=True)
            self.report()
            if self.raw:
                self.raw.close()
            self.store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--seconds", type=float)
    args = parser.parse_args()
    runner = Runner(load(args.config))
    def stop(*_):
        runner.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    asyncio.run(runner.run(args.seconds))


if __name__ == "__main__":
    main()
