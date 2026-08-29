"""Minimal Bitget v2 USDT-M futures REST client (stdlib only)."""
import os, time, hmac, hashlib, base64, json, urllib.request, urllib.error, urllib.parse

BASE = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
MARGIN_COIN = "USDT"


class BitgetError(Exception):
    def __init__(self, code, msg, http=None):
        super().__init__(f"{code}: {msg}")
        self.code, self.msg, self.http = code, msg, http


class Bitget:
    def __init__(self, key, secret, passphrase):
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.offset_ms = 0
        self.hedge = True  # Bitget posMode; refreshed by refresh_mode()

    # ---- transport -------------------------------------------------------
    def _sign(self, ts, method, path_q, body):
        pre = ts + method + path_q + body
        return base64.b64encode(hmac.new(self.secret.encode(), pre.encode(), hashlib.sha256).digest()).decode()

    def _req(self, method, path, params=None, body=None, auth=True):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        data = json.dumps(body, separators=(",", ":")) if body is not None else ""
        ts = str(int(time.time() * 1000) + self.offset_ms)
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if auth:
            headers.update({
                "ACCESS-KEY": self.key,
                "ACCESS-SIGN": self._sign(ts, method, path + q, data),
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-PASSPHRASE": self.passphrase,
            })
        req = urllib.request.Request(BASE + path + q, data=data.encode() if data else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                j = json.loads(raw)
                raise BitgetError(j.get("code"), j.get("msg"), e.code)
            except ValueError:
                raise BitgetError("HTTP", raw[:300], e.code)
        j = json.loads(raw)
        if j.get("code") != "00000":
            raise BitgetError(j.get("code"), j.get("msg"))
        return j["data"]

    def get(self, path, auth=True, **params):
        return self._req("GET", path, params=params or None, auth=auth)

    def post(self, path, **body):
        return self._req("POST", path, body=body)

    # ---- public ----------------------------------------------------------
    def sync_time(self):
        srv = int(self.get("/api/v2/public/time", auth=False)["serverTime"])
        self.offset_ms = srv - int(time.time() * 1000)
        return self.offset_ms

    def ticker(self, symbol):
        return self.get("/api/v2/mix/market/ticker", auth=False, symbol=symbol, productType=PRODUCT)[0]

    def contract(self, symbol):
        return self.get("/api/v2/mix/market/contracts", auth=False, symbol=symbol, productType=PRODUCT)[0]

    def history_candles(self, symbol, granularity, end_ms, limit=200):
        """Candles that started before end_ms (public history endpoint, max 200), oldest->newest, same dicts as candles()."""
        rows = self.get("/api/v2/mix/market/history-candles", auth=False, symbol=symbol, productType=PRODUCT,
                        granularity=granularity, endTime=str(int(end_ms)), limit=str(limit))
        out = [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                "c": float(r[4]), "v": float(r[5]), "qv": float(r[6])} for r in rows]
        out.sort(key=lambda x: x["ts"])
        return out

    def candles(self, symbol, granularity, limit=200):
        """Returns list of dicts oldest->newest. Last element is the still-open candle."""
        rows = self.get("/api/v2/mix/market/candles", auth=False, symbol=symbol,
                        productType=PRODUCT, granularity=granularity, limit=str(limit))
        out = [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                "c": float(r[4]), "v": float(r[5]), "qv": float(r[6])} for r in rows]
        out.sort(key=lambda x: x["ts"])
        return out

    # ---- account ---------------------------------------------------------
    def account(self, symbol):
        return self.get("/api/v2/mix/account/account", symbol=symbol, productType=PRODUCT, marginCoin=MARGIN_COIN)

    def refresh_mode(self, symbol):
        a = self.account(symbol)
        self.hedge = a.get("posMode") == "hedge_mode"
        return a

    def positions(self):
        return self.get("/api/v2/mix/position/all-position", productType=PRODUCT, marginCoin=MARGIN_COIN)

    def position(self, symbol):
        for p in self.positions():
            if p["symbol"] == symbol and float(p.get("total", 0)) > 0:
                return p
        return None

    def set_position_mode(self, mode="one_way_mode"):
        return self.post("/api/v2/mix/account/set-position-mode", productType=PRODUCT, posMode=mode)

    def set_margin_mode(self, symbol, mode="isolated"):
        return self.post("/api/v2/mix/account/set-margin-mode", symbol=symbol, productType=PRODUCT,
                         marginCoin=MARGIN_COIN, marginMode=mode)

    def set_leverage(self, symbol, lev, hold_side=None):
        body = dict(symbol=symbol, productType=PRODUCT, marginCoin=MARGIN_COIN, leverage=str(lev))
        if hold_side:
            body["holdSide"] = hold_side
        return self.post("/api/v2/mix/account/set-leverage", **body)

    def set_margin(self, symbol, hold_side, amount):
        """Isolated margin adjust: amount>0 adds, <0 releases."""
        return self.post("/api/v2/mix/account/set-margin", symbol=symbol, productType=PRODUCT,
                         marginCoin=MARGIN_COIN, holdSide=hold_side, amount=str(amount))

    # ---- orders ----------------------------------------------------------
    def market_order(self, symbol, side, size, sl=None, tp=None, trade_side="open", reduce_only=False, client_oid=None):
        """side: 'buy'=long / 'sell'=short. In hedge mode side is the POSITION direction and
        trade_side ('open'|'close') says whether we add to or reduce it (close long = buy/close)."""
        body = dict(symbol=symbol, productType=PRODUCT, marginMode="isolated", marginCoin=MARGIN_COIN,
                    size=str(size), side=side, orderType="market", force="gtc")
        if self.hedge:
            body["tradeSide"] = trade_side
        elif reduce_only:
            body["reduceOnly"] = "YES"
        if sl is not None:
            body["presetStopLossPrice"] = str(sl)
        if tp is not None:
            body["presetStopSurplusPrice"] = str(tp)
        if client_oid:
            body["clientOid"] = client_oid
        return self.post("/api/v2/mix/order/place-order", **body)

    def limit_order(self, symbol, side, price, size, trade_side="open", post_only=True, client_oid=None, sl=None, tp=None):
        """Maker entry/exit. hedge: side=position direction, trade_side open|close. post_only rejects if it would take."""
        body = dict(symbol=symbol, productType=PRODUCT, marginMode="isolated", marginCoin=MARGIN_COIN,
                    size=str(size), price=str(price), side=side, orderType="limit",
                    force="post_only" if post_only else "gtc")
        if self.hedge:
            body["tradeSide"] = trade_side
        elif trade_side == "close":
            body["reduceOnly"] = "YES"
        if client_oid:
            body["clientOid"] = client_oid
        if sl is not None:
            body["presetStopLossPrice"] = str(sl)
        if tp is not None:
            body["presetStopSurplusPrice"] = str(tp)
        return self.post("/api/v2/mix/order/place-order", **body)

    def place_plan_order(self, symbol, side, size, trigger, trade_side="open", sl=None, tp=None, order_type="market", price=None):
        """Conditional (stop) order: fires a market/limit order when mark price hits trigger."""
        body = dict(planType="normal_plan", symbol=symbol, productType=PRODUCT, marginMode="isolated",
                    marginCoin=MARGIN_COIN, size=str(size), triggerPrice=str(trigger), triggerType="mark_price",
                    side=side, orderType=order_type)
        if self.hedge:
            body["tradeSide"] = trade_side
        elif trade_side == "close":
            body["reduceOnly"] = "YES"
        if price is not None:
            body["price"] = str(price)
        if sl is not None:
            body["presetStopLossPrice"] = str(sl)
        if tp is not None:
            body["presetStopSurplusPrice"] = str(tp)
        return self.post("/api/v2/mix/order/place-plan-order", **body)

    def cancel_order(self, symbol, order_id=None, client_oid=None):
        """By orderId, or by clientOid for a submission whose response was lost."""
        body = dict(symbol=symbol, productType=PRODUCT)
        if order_id: body["orderId"] = order_id
        else: body["clientOid"] = client_oid
        return self.post("/api/v2/mix/order/cancel-order", **body)

    def close_position(self, symbol, hold_side=None):
        body = dict(symbol=symbol, productType=PRODUCT)
        if hold_side:
            body["holdSide"] = hold_side
        return self.post("/api/v2/mix/order/close-positions", **body)

    def place_pos_tpsl(self, symbol, hold_side, sl=None, tp=None):
        """Position-level SL/TP (whole position, follows size changes). Market execution on trigger."""
        out = {}
        for plan, px in (("pos_loss", sl), ("pos_profit", tp)):
            if px is None:
                continue
            out[plan] = self.post("/api/v2/mix/order/place-tpsl-order", symbol=symbol, productType=PRODUCT,
                                  marginCoin=MARGIN_COIN, planType=plan, triggerPrice=str(px),
                                  triggerType="mark_price", executePrice="0", holdSide=hold_side)
        return out

    def modify_pos_tpsl(self, symbol, order_id, trigger, hold_side):
        """Move an existing position SL/TP trigger in place (no window without protection). Not yet exercised against the live API."""
        return self.post("/api/v2/mix/order/modify-tpsl-order", symbol=symbol, productType=PRODUCT, marginCoin=MARGIN_COIN,
                         orderId=order_id, triggerPrice=str(trigger), triggerType="mark_price", executePrice="0", holdSide=hold_side, size="")

    def cancel_plan(self, symbol, order_id, plan_type="profit_loss"):
        return self.post("/api/v2/mix/order/cancel-plan-order", symbol=symbol, productType=PRODUCT,
                         marginCoin=MARGIN_COIN, orderIdList=[{"orderId": order_id}], planType=plan_type)

    def pending_orders(self, symbol):
        return self.get("/api/v2/mix/order/orders-pending", symbol=symbol, productType=PRODUCT)

    def pending_plan_orders(self, symbol):
        return self.get("/api/v2/mix/order/orders-plan-pending", symbol=symbol, productType=PRODUCT,
                        planType="profit_loss")

    def order_detail(self, symbol, order_id=None, client_oid=None):
        q = dict(symbol=symbol, productType=PRODUCT)
        if order_id: q["orderId"] = order_id
        else: q["clientOid"] = client_oid
        return self.get("/api/v2/mix/order/detail", **q)

    def fills(self, symbol, limit=20):
        return self.get("/api/v2/mix/order/fills", symbol=symbol, productType=PRODUCT, limit=str(limit))


def from_env(env_path=None):
    from dotenv import load_dotenv
    load_dotenv(env_path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    return Bitget(os.environ["BITGET_API_KEY"], os.environ["BITGET_SECRET_KEY"], os.environ["BITGET_PASSPHRASE"])
