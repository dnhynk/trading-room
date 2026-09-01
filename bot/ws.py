"""Bitget v2 WebSocket client + raw recorder.
  python -m bot.ws record           # record public channels from params.json["record"] (hot-reloaded) + private channels
  python -m bot.ws probe [SYMBOL]   # 20s check: prints login/subscribe acks and the first message of every channel
Files: data/ws/pub-YYYYMMDD-HH.jsonl (UTC hour; gzipped once the hour closes) and data/ws/prv-YYYYMMDD.jsonl.
Line = "<recv_ms>\\t<raw message>". Connection lifecycle lines are JSON objects with key "local" (WS_UP/WS_DOWN/WS_STALE)."""
import asyncio, base64, glob, gzip, hashlib, hmac, json, os, shutil, sys, threading, time
import websockets
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "ws")
PARAMS = os.path.join(ROOT, "params.json")
PUB_URL, PRV_URL = "wss://ws.bitget.com/v2/ws/public", "wss://ws.bitget.com/v2/ws/private"
INST = "USDT-FUTURES"
PRIVATE_ARGS = ([{"instType": INST, "channel": c, "instId": "default"} for c in ("orders", "fill", "positions", "orders-algo")]
                + [{"instType": INST, "channel": "account", "coin": "default"}])
PING_S, STALE_S = 25, 45   # app-level "ping" cadence; reconnect if nothing (not even pong) arrives for STALE_S


def load_params():
    """dict, or None when the file is missing/invalid (callers keep their previous value)."""
    try:
        with open(PARAMS, encoding="utf-8") as f: return json.load(f)
    except Exception: return None

def strat_for(params, symbol=None):
    """엔진 하나가 쓰는 strat: 공통 `strat` 위에 `books[symbol]`을 덮는다. `books`가 없으면 지금과 완전히 같다.
    symbol=None 이면 `strat.symbol`(포트폴리오의 기본 엔진). 심볼별로 다를 수 있는 것은 sides·wallet_frac 같은 것뿐이고
    나머지는 공통 strat 을 그대로 쓴다 — 심볼마다 규칙을 따로 두면 그건 다른 전략이지 포트폴리오가 아니다."""
    from bot.signal import STRAT
    sp = {**STRAT, **((params or {}).get("strat") or {})}
    sym = symbol or sp.get("symbol")
    return {**sp, **(((params or {}).get("books") or {}).get(sym) or {}), "symbol": sym}

def portfolio(params):
    """엔진을 돌릴 심볼들. `books`가 있으면 그 키들, 없으면 `strat.symbol` 하나."""
    b = list((params or {}).get("books") or {})
    return b or [((params or {}).get("strat") or {}).get("symbol")]

def outside_books(params, symbol):
    """`books`가 있는데 그 심볼이 없으면 포트폴리오 밖 계약이다 — 아무 엔진도 소유하지 않고 지갑 몫도 없다
    (`wallet_frac`이 공통 기본값 1.0으로 떨어져 지갑 전액으로 사이징한다). 엔진은 시작을 거부하고 감시견은 다시 올리지 않는다.
    `books`가 없으면 예전 단일 엔진이라 판정하지 않는다."""
    books = (params or {}).get("books") or {}
    return bool(books) and symbol not in books

def load_states():
    """엔진마다 logs/state-<SYMBOL>.json 을 쓴다(동시 기록자가 한 파일을 덮어쓰지 않도록). {심볼: 스냅샷}.
    하나도 없으면 예전 단일 logs/state.json 으로 물러선다 — 전환 직후 한 번만 해당된다."""
    out = {}
    for p in glob.glob(os.path.join(ROOT, "logs", "state-*.json")):
        try:
            with open(p, encoding="utf-8") as f: st = json.load(f)
            if st.get("symbol"): out[st["symbol"]] = st
        except Exception: pass
    if not out:
        try:
            with open(os.path.join(ROOT, "logs", "state.json"), encoding="utf-8") as f: st = json.load(f)
            if st.get("symbol"): out[st["symbol"]] = st
        except Exception: pass
    return out

def pub_args(params, extra=()):
    """Subscription args for params["record"] = {symbol: [channel, ...]} plus extra (symbol, channel) pairs."""
    rec = (params or {}).get("record") or {}
    pairs = [(s, c) for s, chs in rec.items() for c in chs] + list(extra)
    return [{"instType": INST, "channel": c, "instId": s} for s, c in dict.fromkeys(pairs)]


class WS:
    """One connection that keeps itself alive: app-level ping, staleness check, reconnect + re-login + re-subscribe.
    on_msg(raw) receives every exchange message except pong; on_local(dict) receives lifecycle events."""
    def __init__(self, url, on_msg, on_local, auth=None, name="pub"):
        self.url, self.on_msg, self.on_local, self.auth, self.name = url, on_msg, on_local, auth, name
        self.args, self.connected, self._ws, self._rx = [], False, None, 0.0
        self._pending, self._sub_t = set(), 0.0          # subscriptions sent but not yet acknowledged; connected only once all are

    async def run(self):
        backoff = 1
        while True:
            t_up = time.time()
            try:
                async with websockets.connect(self.url, ping_interval=None, open_timeout=15, max_size=1 << 22) as ws:
                    self._ws, self._rx = ws, time.time()
                    if self.auth: await self._login(ws)
                    self._pending, self._sub_t = {self._key(a) for a in self.args}, time.time()
                    if self.args: await self._op(ws, "subscribe", self.args)
                    else: self._up()
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        async for m in ws:
                            self._rx = time.time()
                            if m == "pong": continue
                            if self._pending: self._ack(m)
                            self.on_msg(m)
                    finally:
                        pinger.cancel()
                err = "closed"
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:160]}"
            self.connected, self._ws = False, None
            if time.time() - t_up > 60: backoff = 1
            self.on_local({"local": "WS_DOWN", "name": self.name, "err": err, "retry_s": backoff})
            await asyncio.sleep(backoff); backoff = min(backoff * 2, 30)

    async def _login(self, ws):
        key, secret, pw = self.auth
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac.new(secret.encode(), f"{ts}GET/user/verify".encode(), hashlib.sha256).digest()).decode()
        await ws.send(json.dumps({"op": "login", "args": [{"apiKey": key, "passphrase": pw, "timestamp": ts, "sign": sign}]}))
        for _ in range(20):  # the ack arrives before any data
            m = await asyncio.wait_for(ws.recv(), 10)
            if m == "pong": continue
            self.on_msg(m)
            j = json.loads(m)
            if j.get("event") == "login" and str(j.get("code")) == "0": return
            if j.get("event") in ("login", "error"): raise RuntimeError(f"login: {m[:200]}")
        raise RuntimeError("login: no ack")

    async def _pinger(self, ws):
        try:
            last = time.time()
            while True:
                await asyncio.sleep(5)
                if time.time() - self._rx > STALE_S:
                    self.on_local({"local": "WS_STALE", "name": self.name, "silent_s": round(time.time() - self._rx)})
                    await ws.close(); return
                if self._pending and time.time() - self._sub_t > 10:      # never run half-subscribed: reconnect and subscribe again
                    self.on_local({"local": "WS_SUB_TIMEOUT", "name": self.name, "pending": len(self._pending)})
                    await ws.close(); return
                if time.time() - last >= PING_S:
                    await ws.send("ping"); last = time.time()
        except Exception: pass

    async def _op(self, ws, op, args):
        for i in range(0, len(args), 10):
            await ws.send(json.dumps({"op": op, "args": args[i:i + 10]}))

    @staticmethod
    def _key(a): return json.dumps(a, sort_keys=True)

    def _up(self):
        self.connected = True
        self.on_local({"local": "WS_UP", "name": self.name, "subs": len(self.args), "ack_ms": int((time.time() - self._sub_t) * 1000)})

    def _ack(self, m):
        """Bitget answers every subscribe arg with {"event":"subscribe","arg":{...}} (or "error"): connected means all of them answered.
        A refused arg is dropped from the set (the others keep flowing) and reported as WS_SUB_ERROR."""
        if not m.startswith('{"event"'): return
        try: j = json.loads(m)
        except ValueError: return
        k = self._key(j.get("arg") or {})
        if j.get("event") == "subscribe": self._pending.discard(k)
        elif j.get("event") == "error" and k in self._pending:
            self._pending.discard(k); self.args = [a for a in self.args if self._key(a) != k]
            self.on_local({"local": "WS_SUB_ERROR", "name": self.name, "msg": m[:200]})
        if not self._pending and not self.connected: self._up()

    async def set_args(self, args):
        """Desired subscription set. Diff is applied live on an open socket; run() (re)subscribes the full set on connect."""
        key = self._key
        old, new = {key(a): a for a in self.args}, {key(a): a for a in args}
        add, rem = [a for k, a in new.items() if k not in old], [a for k, a in old.items() if k not in new]
        self.args = list(new.values())
        if self._ws:
            try:
                if rem: await self._op(self._ws, "unsubscribe", rem)
                if add: self._pending |= {key(a) for a in add}; self._sub_t = time.time(); await self._op(self._ws, "subscribe", add)
            except Exception: pass  # connection is going down; reconnect resubscribes self.args


class Recorder:
    """Append-only raw log. hourly=True: one UTC-hour file, gzipped after rotation (leftovers of a crashed run at startup)."""
    def __init__(self, prefix, hourly):
        os.makedirs(DATA, exist_ok=True)
        self.prefix, self.hourly, self.key, self.f = prefix, hourly, None, None
        if hourly:
            cur = self._name(self._key(time.time()))
            for fn in os.listdir(DATA):
                if fn.startswith(prefix + "-") and fn.endswith(".jsonl") and fn != cur: self._gzip_async(os.path.join(DATA, fn))

    def _key(self, t): return time.strftime("%Y%m%d-%H" if self.hourly else "%Y%m%d", time.gmtime(t))
    def _name(self, key): return f"{self.prefix}-{key}.jsonl"

    def write(self, raw):
        t = time.time(); key = self._key(t)
        if key != self.key:
            if self.f:
                self.f.close()
                if self.hourly: self._gzip_async(os.path.join(DATA, self._name(self.key)))
            self.f, self.key = open(os.path.join(DATA, self._name(key)), "a", encoding="utf-8", buffering=1 << 16), key
        self.f.write(f"{int(t * 1000)}\t{raw}\n")

    def flush(self):
        if self.f: self.f.flush()

    @staticmethod
    def _gzip_async(path):
        def job():
            with open(path, "rb") as src, gzip.open(path + ".gz", "wb", compresslevel=6) as dst: shutil.copyfileobj(src, dst)
            os.remove(path)
        threading.Thread(target=job, daemon=True).start()


def _creds():
    from bot.bitget import from_env
    b = from_env(); return (b.key, b.secret, b.passphrase)

async def record():
    pub_rec, prv_rec = Recorder("pub", hourly=True), Recorder("prv", hourly=False)
    stats, lag = {}, [0, 0.0]   # per-channel counts; (n, sum ms) exchange ts -> local receive, trade pushes only
    def log(s): print(time.strftime("%H:%M:%S ") + s, flush=True)
    def on_msg(rec, raw):
        rec.write(raw)
        try:
            j = json.loads(raw); k = (j.get("arg") or {}).get("channel") or j.get("event") or "?"
            stats[k] = stats.get(k, 0) + 1
            if k == "trade" and "ts" in j: lag[0] += 1; lag[1] += time.time() * 1000 - int(j["ts"])
            if "event" in j: log(f"{rec.prefix} {raw[:300]}")
        except Exception: pass
    def on_local(ev):
        (prv_rec if ev.get("name") == "prv" else pub_rec).write(json.dumps(ev)); log(json.dumps(ev))
    pub = WS(PUB_URL, lambda r: on_msg(pub_rec, r), on_local, name="pub")
    prv = WS(PRV_URL, lambda r: on_msg(prv_rec, r), on_local, auth=_creds(), name="prv")
    await prv.set_args(PRIVATE_ARGS)
    tasks = [asyncio.create_task(pub.run()), asyncio.create_task(prv.run())]
    mtime, t_stat = None, time.time()
    while True:
        try: m = os.path.getmtime(PARAMS)
        except OSError: m = 0
        if m != mtime:
            mtime = m; p = load_params()
            if p is None: log("PARAMS_INVALID keeping previous subscriptions")
            else:
                args = pub_args(p); await pub.set_args(args); log("SUBS " + " ".join(f"{a['instId']}:{a['channel']}" for a in args))
        pub_rec.flush(); prv_rec.flush()
        if time.time() - t_stat >= 900:
            log("REC " + " ".join(f"{k}={v}" for k, v in sorted(stats.items())) + (f" lag_ms={lag[1] / lag[0]:.0f}" if lag[0] else "")
                + f" up={pub.connected}/{prv.connected}")
            stats.clear(); lag[:] = [0, 0.0]; t_stat = time.time()
        await asyncio.sleep(1)

async def probe(sym):
    seen = set()
    def on_msg(raw):
        j = json.loads(raw); k = (j.get("arg") or {}).get("channel") or j.get("event") or "?"
        if "event" in j: print(f"ack {raw[:300]}", flush=True)
        elif k not in seen: seen.add(k); print(f"{k}: {raw[:500]}", flush=True)
    on_local = lambda ev: print(ev, flush=True)
    pub = WS(PUB_URL, on_msg, on_local); prv = WS(PRV_URL, on_msg, on_local, auth=_creds(), name="prv")
    await pub.set_args(pub_args({"record": {sym: ["trade", "books15", "ticker", "candle1m"]}}))
    await prv.set_args(PRIVATE_ARGS)
    tasks = [asyncio.create_task(pub.run()), asyncio.create_task(prv.run())]
    await asyncio.sleep(20)
    for t in tasks: t.cancel()
    print("seen:", sorted(seen))

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "record": asyncio.run(record())
    elif cmd == "probe": asyncio.run(probe(sys.argv[2] if len(sys.argv) > 2 else "TRUMPUSDT"))
    else: print(__doc__)
