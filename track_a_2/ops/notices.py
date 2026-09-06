"""Read-only A-2 ledger replay and Slack card presentation.

Private order and campaign identifiers never enter rendered card text.  This
module reads the durable A-2 ledger and status only; it cannot call Coinone.
"""
from contextlib import closing
import datetime as dt
from decimal import Decimal as D, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import sqlite3
import time


KST = dt.timezone(dt.timedelta(hours=9))
TERMINAL = {
    "FILLED", "CANCELED", "NOT_TRIGGERED_CANCELED", "CANCELED_NO_ORDER",
    "CANCELED_LIMIT_PRICE_EXCEED", "CANCELED_UNDER_PRODUCT_UNIT", "REJECTED",
}
HISTORY = (
    "ORDER_INTENT", "FILL", "ORDER_STATUS", "FLAT", "SCAN", "HALT",
    "ORDER_REJECTED", "ORDER_UNCERTAIN", "CAMPAIGN_LATE_PNL", "API_ERROR",
)
REPLAY_VERSION = 1


class DataUnavailable(RuntimeError):
    pass


def number(value):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("nonfinite A-2 amount") from None
    if not result.is_finite():
        raise ValueError("nonfinite A-2 amount")
    return result


def krw(value, signed=False):
    rounded = number(value).quantize(D("0.1"), rounding=ROUND_HALF_UP)
    if not rounded:
        rounded = D(0)
    return format(rounded, ("+" if signed else "") + ",.1f") + "원"


def qty(value):
    rendered = format(number(value), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def stamp(milliseconds):
    return dt.datetime.fromtimestamp(int(milliseconds) / 1000, KST).strftime(
        "%m-%d %H:%M:%S KST"
    )


def payload(kind, head, fields=None, lines=None, *, t_ms=None):
    """Build an A-2 card using the existing shared Block Kit vocabulary."""
    return dict(
        track="A2",
        kind=kind,
        head="A-2 · " + head,
        fields=fields or [],
        lines=lines or [],
        ctx=(
            "코인원 현물 · " + stamp(t_ms or time.time() * 1000)
            + " · A-2 원화 장부"
        ),
    )


class Source:
    def __init__(self, directory):
        if not directory:
            raise DataUnavailable("A-2 data directory is required")
        self.directory = Path(directory).resolve()

    def connect(self):
        path = self.directory / "a2-ledger.sqlite"
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)

    def read(self, after=None):
        try:
            with closing(self.connect()) as database:
                database.execute("BEGIN")
                row = database.execute("SELECT body FROM state WHERE id=1").fetchone()
                state = json.loads(row[0])
                maximum = database.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM events"
                ).fetchone()[0]
                if after is None:
                    sql = (
                        "SELECT seq,t_ms,kind,body FROM events WHERE kind IN ("
                        + ",".join("?" for _ in HISTORY)
                        + ") ORDER BY seq"
                    )
                    rows = database.execute(sql, HISTORY).fetchall()
                else:
                    rows = database.execute(
                        "SELECT seq,t_ms,kind,body FROM events "
                        "WHERE seq>? AND seq<=? ORDER BY seq LIMIT 1000",
                        (after, maximum),
                    ).fetchall()
            status = json.loads(
                (self.directory / "status.json").read_text(encoding="utf-8")
            )
            if (
                state.get("version") != 1
                or not state.get("capital_initialized")
                or status.get("track") != "A-2"
                or status.get("execution_version") is None
            ):
                raise ValueError("A-2 capital state unavailable")
            return (
                state,
                status,
                [(seq, t_ms, kind, json.loads(body)) for seq, t_ms, kind, body in rows],
                maximum,
            )
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
            raise DataUnavailable("A-2 ledger/status unavailable") from None


def _campaign(context, campaign_id, coin):
    if not campaign_id:
        raise DataUnavailable("A-2 campaign identity unavailable")
    campaigns = context.setdefault("campaigns", {})
    return campaigns.setdefault(
        campaign_id,
        dict(
            campaign_id=campaign_id,
            coin=coin,
            qty="0",
            buy_gross="0",
            sell_gross="0",
            fees="0",
            realized="0",
            orders=[],
        ),
    )


class Replay:
    """Reconstruct only the accounting needed for notifications."""

    def __init__(self, context=None):
        self.context = context or dict(orders={}, campaigns={}, selected=[])
        self.context.setdefault("orders", {})
        self.context.setdefault("campaigns", {})
        self.context.setdefault("selected", [])

    def apply(self, row):
        seq, t_ms, kind, body = row
        orders = self.context["orders"]
        if kind == "ORDER_INTENT":
            order = body.get("order")
            if not isinstance(order, dict) or not order.get("cid"):
                raise DataUnavailable("A-2 order context unavailable")
            kept = {
                key: order.get(key)
                for key in (
                    "cid", "coin", "role", "side", "type", "qty", "price",
                    "trigger_price", "campaign_id", "purpose", "reason",
                )
            }
            orders[order["cid"]] = kept
            campaign = _campaign(
                self.context, order.get("campaign_id"), order.get("coin")
            )
            if order["cid"] not in campaign["orders"]:
                campaign["orders"].append(order["cid"])
            return None
        if kind == "FILL":
            cid = body.get("cid")
            order = orders.get(cid) or {
                key: body.get(key)
                for key in ("coin", "role", "side", "campaign_id", "reason")
            }
            coin = body.get("coin") or order.get("coin")
            campaign_id = body.get("campaign_id") or order.get("campaign_id")
            campaign_known = campaign_id in self.context["campaigns"]
            campaign = _campaign(self.context, campaign_id, coin)
            amount = number(body.get("qty") or 0)
            gross = number(body.get("gross_delta") or 0)
            fee = number(body.get("fee") or 0)
            pnl = number(body.get("pnl") or 0)
            if amount < 0 or gross < 0 or fee < 0:
                raise DataUnavailable("A-2 fill values are invalid")
            side = body.get("side") or order.get("side")
            before = number(campaign["qty"])
            first = bool(side == "BUY" and amount > 0 and before == 0)
            if amount:
                if side == "BUY":
                    campaign["qty"] = str(before + amount)
                    campaign["buy_gross"] = str(
                        number(campaign["buy_gross"]) + gross
                    )
                elif side == "SELL":
                    remaining = before - amount
                    if remaining < 0:
                        raise DataUnavailable("A-2 notification inventory is negative")
                    campaign["qty"] = str(remaining)
                    campaign["sell_gross"] = str(
                        number(campaign["sell_gross"]) + gross
                    )
                else:
                    raise DataUnavailable("A-2 fill side unavailable")
            campaign["fees"] = str(number(campaign["fees"]) + fee)
            campaign["realized"] = str(number(campaign["realized"]) + pnl)
            fact = dict(
                type="fill" if amount else "correction",
                seq=seq,
                t_ms=t_ms,
                coin=coin,
                cid=cid,
                campaign_id=campaign_id,
                side=side,
                role=body.get("role") or order.get("role"),
                purpose=order.get("purpose"),
                reason=body.get("reason") or order.get("reason"),
                qty=str(amount),
                gross=str(gross),
                fee=str(fee),
                pnl=str(pnl),
                price=body.get("price"),
                first=first,
                remaining=campaign["qty"],
                campaign_realized=campaign["realized"],
            )
            # A late correction is first journaled as CAMPAIGN_LATE_PNL.  The
            # following zero-quantity FILL carries the order cumulative update,
            # but must not create a duplicate Slack card after the campaign was
            # already removed from replay context by FLAT.
            if not amount and pnl and not campaign_known:
                return None
            return fact if amount or pnl else None
        if kind == "ORDER_STATUS":
            order = orders.get(body.get("cid"))
            if (
                order
                and order.get("role") == "protect"
                and body.get("status") == "NOT_TRIGGERED"
            ):
                return dict(
                    type="protection",
                    seq=seq,
                    t_ms=t_ms,
                    coin=order["coin"],
                    campaign_id=order.get("campaign_id"),
                    order=order,
                )
            return None
        if kind == "FLAT":
            campaign = body.get("campaign") or {}
            campaign_id = campaign.get("campaign_id")
            prior = self.context["campaigns"].pop(campaign_id, None)
            if prior:
                for cid in prior.get("orders", ()):
                    orders.pop(cid, None)
            return None
        if kind == "SCAN":
            current = list(body.get("selected") or ())
            prior = self.context.get("selected", [])
            self.context["selected"] = current
            if prior and current != prior:
                return dict(
                    type="selection", seq=seq, t_ms=t_ms, old=prior, new=current
                )
            return None
        if kind in ("HALT", "ORDER_REJECTED", "ORDER_UNCERTAIN", "API_ERROR"):
            return dict(type=kind, seq=seq, t_ms=t_ms, body=body)
        if kind == "CAMPAIGN_LATE_PNL":
            return dict(type=kind, seq=seq, t_ms=t_ms, body=body)
        return None


def _protection(state, coin):
    matches = [
        order
        for order in state.get("orders", {}).values()
        if order.get("coin") == coin
        and order.get("role") == "protect"
        and order.get("status") == "NOT_TRIGGERED"
        and not order.get("cancel_requested")
    ]
    if len(matches) != 1:
        phase = (state.get("books", {}).get(coin) or {}).get("inventory_phase")
        return "확인 중" + (" · " + str(phase) if phase else "")
    order = matches[0]
    return (
        "확인됨 · " + qty(order.get("qty") or 0) + "개 · 트리거 "
        + krw(order.get("trigger_price") or 0)
    )


def operating_fields(state, status):
    mode = "HALT" if state.get("halt") else (
        "진입 일시정지" if status.get("entry_paused") else "LIVE"
    )
    stream = (
        "공개·개인 연결"
        if status.get("connected") and status.get("private_connected")
        else "연결 확인 필요"
    )
    positions = []
    protections = []
    for coin, book in sorted(state.get("books", {}).items()):
        amount = sum((number(lot[0]) for lot in book.get("lots", [])), D(0))
        if amount <= 0:
            continue
        text = coin + " " + qty(amount) + "개"
        if book.get("avg") is not None:
            text += " · 평단 " + krw(book["avg"])
        phase = book.get("inventory_phase")
        if phase:
            text += " · " + str(phase)
        positions.append(text)
        protections.append(coin + " · " + _protection(state, coin))
    return [
        ["상태", mode + " · " + stream],
        ["선정 종목", ", ".join(status.get("selected") or ()) or "준비 중"],
        ["포지션", "\n".join(positions) or "없음"],
        ["거래소 보호 주문", "\n".join(protections) or "없음"],
    ]


def summary_fields(directory, *, balance=True):
    try:
        state, status, _, _ = Source(directory).read()
        with closing(Source(directory).connect()) as database:
            rows = database.execute(
                "SELECT kind,body FROM events "
                "WHERE kind IN ('FLAT','CAMPAIGN_LATE_PNL') ORDER BY seq"
            ).fetchall()
        completed = {}
        for kind, raw in rows:
            body = json.loads(raw)
            if kind == "FLAT":
                campaign = body.get("campaign") or {}
                identity = campaign.get("campaign_id")
                if identity and campaign.get("realized") is not None:
                    completed[identity] = number(campaign["realized"])
            else:
                identity = body.get("campaign_id")
                if identity in completed:
                    completed[identity] += number(body.get("pnl") or 0)
        open_count = sum(
            bool(book.get("campaign_open"))
            for book in state.get("books", {}).values()
        )
        values = list(completed.values())
        wins = sum(value > 0 for value in values)
        losses = sum(value < 0 for value in values)
        ties = len(completed) - wins - losses
        fields = []
        if balance:
            age = max(0, time.time() - float(number(status["t_ms"]) / D(1000)))
            stale = " · " + str(int(age // 60)) + "분 전" if age > 120 else ""
            fields.extend(
                (
                    ("A-2 자본", krw(status.get("equity_krw") or 0) + stale),
                    ("가용", krw(status.get("free_cash_krw") or 0)),
                )
            )
        fields.extend(
            (
                ("누적 손익", krw(state.get("realized") or 0, True)),
                ("일일 평가손익 (UTC)", krw(status.get("day_equity_pnl_krw") or 0, True)),
                ("캠페인", f"완료 {len(completed)} / 진행 {open_count}"),
                (
                    "승률",
                    (
                        f"{wins / len(completed) * 100:.1f}% · "
                        f"{wins}승 {losses}패 {ties}보합"
                        if completed
                        else "계산 전 · 완료 0회"
                    ),
                ),
            )
        )
        return fields
    except (DataUnavailable, OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return [("A-2 계기판", "확인 불가")]


def heartbeat(state, status, now, *, boot=False):
    return payload(
        "부팅" if boot else "상태",
        "알림 연결 · 현재 운용 상태" if boot else "60분 운용 현황",
        operating_fields(state, status),
        ["A-2 원화 장부만 읽는 독립 알림입니다."] if boot else None,
        t_ms=int(now * 1000),
    )


def fill_payload(fact, state):
    amount = number(fact["qty"])
    gross = number(fact["gross"])
    price = gross / amount if amount else number(fact.get("price") or 0)
    coin = fact["coin"]
    if fact["type"] == "correction":
        return payload(
            "정산",
            f"{coin} · 체결 정산",
            [["손익 정정", krw(fact["pnl"], True)]],
            ["늦게 확정된 거래소 정산값을 원래 캠페인에 귀속했습니다."],
            t_ms=fact["t_ms"],
        )
    if fact["side"] == "BUY":
        kind = "진입" if fact.get("first") else "추가"
        return payload(
            kind,
            f"{coin} 롱 · {stamp(fact['t_ms'])}",
            [
                ["매수 체결", "메이커 지정가"],
                ["체결 수량", qty(amount) + "개"],
                ["평균 체결가", krw(price)],
                ["체결금액", krw(gross)],
                ["수수료", krw(fact["fee"])],
                ["보호 주문", _protection(state, coin)],
            ],
            ["실제 코인원 체결을 A-2 장부에 반영했습니다."],
            t_ms=fact["t_ms"],
        )
    remaining = number(fact["remaining"])
    reason = str(fact.get("reason") or "")
    risk = fact.get("role") in ("protect", "exit") or reason.startswith(
        ("stop", "exchange_stop", "protection_", "protect_")
    )
    kind = "부분청산" if remaining > 0 else "손절" if risk else "전량청산"
    path = {
        "protect": "거래소 STOP_LIMIT",
        "trim": "전략 시장가 매도",
        "exit": "복구·위험 시장가 매도",
    }.get(fact.get("role"), "시장가 매도")
    return payload(
        kind,
        f"{coin} · {stamp(fact['t_ms'])}",
        [
            ["매도 체결", path],
            ["체결 수량", qty(amount) + "개"],
            ["평균 체결가", krw(price)],
            ["체결금액", krw(gross)],
            ["체결 순손익", krw(fact["pnl"], True)],
            ["캠페인 실현손익", krw(fact["campaign_realized"], True)],
            ["남은 수량", qty(remaining) + "개"],
        ],
        t_ms=fact["t_ms"],
    )


def protection_payload(fact):
    order = fact["order"]
    return payload(
        "상태",
        f"{fact['coin']} · 보호 주문 확인",
        [
            ["보호 수량", qty(order.get("qty") or 0) + "개"],
            ["트리거", krw(order.get("trigger_price") or 0)],
            ["지정가", krw(order.get("price") or 0)],
            ["거래소 상태", "NOT_TRIGGERED 확인"],
        ],
        t_ms=fact["t_ms"],
    )


def event_payload(fact):
    kind = fact["type"]
    body = fact.get("body") or {}
    if kind == "HALT":
        reason = str(body.get("reason") or "UNKNOWN")
        reason = reason if reason.replace("_", "").isalnum() else "UNKNOWN"
        return payload(
            "이상", "거래 중지", [["중지 사유", reason]],
            ["신규 진입을 멈추고 기존 재고 보호 상태를 확인합니다."],
            t_ms=fact["t_ms"],
        )
    if kind == "ORDER_UNCERTAIN":
        return payload(
            "이상", "주문 응답 확인 필요",
            lines=["거래소 주문 상태를 장부에서 계속 대사합니다."],
            t_ms=fact["t_ms"],
        )
    if kind == "ORDER_REJECTED" and body.get("transmitted") is not False:
        return payload(
            "이상", "거래소 주문 거절",
            lines=["주문은 재사용하지 않고 최신 계좌·호가에서 다시 판단합니다."],
            t_ms=fact["t_ms"],
        )
    if kind == "CAMPAIGN_LATE_PNL":
        return payload(
            "정산", "이전 캠페인 정산",
            [["손익 정정", krw(body.get("pnl") or 0, True)]],
            ["새 캠페인의 손익·위험예산에는 섞지 않았습니다."],
            t_ms=fact["t_ms"],
        )
    if kind == "selection":
        return payload(
            "종목변경", "감시 종목 갱신",
            lines=["현재: " + (", ".join(fact["new"]) or "없음")],
            t_ms=fact["t_ms"],
        )
    if kind == "API_ERROR":
        return payload(
            "이상", "계정 API 확인 필요",
            lines=["계좌·주문 조회 상태를 엔진 장부에서 계속 대사합니다."],
            t_ms=fact["t_ms"],
        )
    return None
