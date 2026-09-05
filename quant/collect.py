"""Bounded public WebSocket + closed-candle capture. Never reads credentials.

python -m quant.collect --seconds 1800 --output data/quant/domestic-001
"""
import argparse
import asyncio
from collections import Counter
import datetime as dt
import json
from pathlib import Path
import time
import urllib.request

from .config import Config, canonical, source_hashes, file_hash
from .venues import WS, BITHUMB_TICKS, UPBIT_TICKS, Normalizer, subscriptions, research_config, message, tick_at


def now():
    return time.time_ns() // 1000000


def get(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        value = json.load(response)
    if isinstance(value, dict) and (value.get("result") == "error" or value.get("success") is False or value.get("error")):
        raise ValueError("public endpoint returned an error")
    return value


def candle_url(venue, coin, minutes, count=200):
    if venue == "coinone":
        return f"https://api.coinone.co.kr/public/v2/chart/KRW/{coin}?interval={minutes}m&size={count}"
    if venue == "korbit":
        return f"https://api.korbit.co.kr/v2/candles?symbol={coin.lower()}_krw&interval={minutes}&limit={count}"
    return f"https://api.{venue}.com/v1/candles/minutes/{minutes}?market=KRW-{coin}&count={count}"


def candles(venue, raw, minutes, available):
    rows = []
    for x in (raw["chart"] if venue == "coinone" else raw["data"] if venue == "korbit" else raw):
        if venue in {"coinone", "korbit"}:
            r = dict(ts=int(x["timestamp"]), o=float(x["open"]), h=float(x["high"]), l=float(x["low"]), c=float(x["close"]), v=float(x["target_volume" if venue == "coinone" else "volume"]))
        else:
            t = int(dt.datetime.fromisoformat(x["candle_date_time_utc"]).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            r = dict(ts=t, o=float(x["opening_price"]), h=float(x["high_price"]), l=float(x["low_price"]), c=float(x["trade_price"]), v=float(x["candle_acc_trade_volume"]))
        if r["ts"] + minutes * 60000 <= available:
            rows.append(r)
    return sorted(rows, key=lambda r: r["ts"])


async def capture(args):
    import websockets  # optional: replay/strategy itself remains standard library
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    manifest = dict(start=now(), seconds=args.seconds, requested_coins=args.coins, code=source_hashes(), venues={})
    (out / "manifest.json").write_text(canonical(manifest), encoding="utf-8")

    async def venue_run(venue):
        folder = out / venue
        folder.mkdir()
        errors, public = [], []
        counts = Counter()
        normalizer = Normalizer(venue, args.coins)

        async def fetch(url):
            raw = await asyncio.to_thread(get, url)
            received = now()
            public.append(dict(url=url, available_ms=received, response=raw))
            await asyncio.sleep(.15)  # below each public quota, per venue
            return raw, received

        try:
            url = {"coinone": "https://api.coinone.co.kr/public/v2/markets/KRW", "korbit": "https://api.korbit.co.kr/v2/currencyPairs"}.get(venue, f"https://api.{venue}.com/v1/market/all?isDetails=true")
            meta, _ = await fetch(url)
            if venue == "coinone":
                available = {x["target_currency"]: x for x in meta["markets"] if x["trade_status"] == 1 and x.get("maintenance_status", 0) == 0}
            elif venue == "korbit":
                available = {x["baseCurrency"].upper(): x for x in meta["data"] if x["quoteCurrency"] == "krw" and x["status"] == "launched"}
            else:
                available = {x["market"][4:]: x for x in meta if x["market"].startswith("KRW-") and x.get("market_warning", "NONE") == "NONE"}
            coins = [c for c in args.coins if c in available]
            if not coins:
                raise ValueError("no supported requested coins")
            contracts, seed = {}, {}
            for c in coins:
                if venue == "coinone":
                    raw, _ = await fetch(f"https://api.coinone.co.kr/public/v2/range_units/KRW/{c}")
                    ladder = sorted([[float(x["range_min"]), float(x["price_unit"])] for x in raw["range_price_units"]])
                    qs, minimum = float(available[c]["qty_unit"]), float(available[c]["min_order_amount"])
                elif venue == "korbit":
                    raw, _ = await fetch(f"https://api.korbit.co.kr/v2/tickSizePolicy?symbol={c.lower()}_krw")
                    ladder = sorted([[float(x["priceGte"]), float(x["tickSize"])] for x in raw["data"][0]["tickSizePolicy"]])
                    qs, minimum = 1e-8, float(available[c]["minOrderValue"])
                else:
                    ladder, qs, minimum = BITHUMB_TICKS if venue == "bithumb" else UPBIT_TICKS, 1e-8, 5000
                history = {}
                for minutes, key in ((1, "c1"), (15, "c15")):
                    raw, received = await fetch(candle_url(venue, c, minutes))
                    history[key] = candles(venue, raw, minutes, received)
                if len(history["c1"]) < 30 or len(history["c15"]) < 100:
                    raise ValueError(f"insufficient closed history: {c}")
                history["available_ms"] = received
                seed[c + "KRW"] = history
                contracts[c + "KRW"] = dict(sides=["long"], qstep=qs, tick=tick_at(ladder, history["c1"][-1]["c"]), min_order=minimum, price_ladder=ladder)
            cfg = research_config(Config.read("quant/configs/reference.json"), venue, contracts, args.equity)
            (folder / "config.json").write_text(cfg.text, encoding="utf-8")
            (folder / "seed.json").write_text(canonical(seed), encoding="utf-8")
            (folder / "public.json").write_text(canonical(public), encoding="utf-8")
            public.clear()
            start = now()
            deadline = time.monotonic() + args.seconds
            with (folder / "raw.jsonl").open("x", encoding="utf-8") as raw_file, (folder / "tape.jsonl").open("x", encoding="utf-8") as tape, (folder / "candles.jsonl").open("x", encoding="utf-8") as rest_file:
                async def candle_poll():
                    while time.monotonic() < deadline:
                        await asyncio.sleep(min(30, max(.1, deadline - time.monotonic())))
                        for c in coins:
                            if time.monotonic() >= deadline:
                                return
                            try:
                                raw, received = await fetch(candle_url(venue, c, 1, 3))
                                rest_file.write(canonical(public[-1]) + "\n")
                                public.clear()
                                rows = candles(venue, raw, 1, received)
                                if rows:
                                    data = [[r[k] for k in ("ts", "o", "h", "l", "c", "v")] for r in rows]
                                    # REST returns only already-closed candles. Their arrival is preserved.
                                    delivered = now()
                                    tape.write(f"{delivered}\t" + canonical(message(c + "KRW", "candle1m", data, delivered)) + "\n")
                            except Exception as e:
                                errors.append(dict(t=now(), source="candles", coin=c, error=type(e).__name__))
                poll = asyncio.create_task(candle_poll())
                try:
                    async with websockets.connect(WS[venue], ping_interval=20, ping_timeout=20, open_timeout=15, max_size=2**23) as ws:
                        for request in subscriptions(venue, coins):
                            await ws.send(canonical(request))
                        print(canonical(dict(venue=venue, status="capturing", coins=coins, seconds=args.seconds)), flush=True)
                        while time.monotonic() < deadline:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=min(5, max(.1, deadline - time.monotonic())))
                            except asyncio.TimeoutError:
                                continue
                            received = now()
                            if isinstance(raw, bytes):
                                raw = raw.decode()
                            raw_file.write(canonical(dict(recv_ms=received, message=raw)) + "\n")
                            event = normalizer.parse(received, raw)
                            if event:
                                tape.write(f"{received}\t" + canonical(event.message) + "\n")
                                counts[event.channel] += 1
                            if counts.total() % 100 == 0:
                                tape.flush()
                                raw_file.flush()
                finally:
                    poll.cancel()
                    await asyncio.gather(poll, return_exceptions=True)
            result = dict(start=start, end=now(), coins=coins, counts=dict(counts), quality=dict(normalizer.quality), errors=errors)
        except Exception as e:
            errors.append(dict(t=now(), error=type(e).__name__, detail=str(e)))
            result = dict(end=now(), counts=dict(counts), errors=errors)
        result["files"] = {p.name: file_hash(p) for p in folder.iterdir() if p.is_file()}
        (folder / "capture.json").write_text(canonical(result), encoding="utf-8")
        print(canonical(dict(venue=venue, status="finished", counts=dict(counts), errors=len(errors))), flush=True)
        manifest["venues"][venue] = result

    await asyncio.gather(*(venue_run(v) for v in args.venues))
    manifest["end"] = now()
    (out / "manifest.json").write_text(canonical(manifest), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venues", nargs="+", choices=list(WS), default=list(WS))
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "XRP", "DOGE", "SOL", "TRUMP"])
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--equity", type=float, default=1000000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 86400 or len(set(args.venues)) != len(args.venues) or any(not c.isalnum() or c != c.upper() for c in args.coins):
        parser.error("invalid duration/venues/coins")
    asyncio.run(capture(args))


if __name__ == "__main__":
    main()
