"""감독 세션의 Slack 알림 렌더러 (읽기 전용, 주문을 내지 않는다).

    python -m common.notify <payload.json>          # 파일로
    python -m common.notify -                       # stdin 으로
    from common.notify import send; send(kind, head, fields=[...], lines=[...], ctx="...")

payload = {"kind": "진입", "head": "MUBARAK 롱 · 09-04 00:18 KST",
           "fields": [["매수 (메이커)", "952개 @ 0.02887"], ...],   # 2열 표, 라벨/값
           "lines": ["한 줄 설명", ...],                            # 본문(생략 가능)
           "ctx": "회색 꼬리말"}                                     # 맥락(생략 가능)

`kind` 는 KINDS 의 키이고 제목 접두·이모지·좌측 색 바를 정한다(CLAUDE.md 감독 알림 채널:
`[진입]`·`[추가]`·`[부분청산]`·`[전량청산]`·`[손절]`·`[이상]`·`[복구]`·`[종목변경]`·`[야간리포트]`).
**하단 계기판은 자동이다.** 이벤트 상세 아래에 잔고·가용·누적 손익·엔진 수익률·캠페인 횟수·승률을
필드로 붙인다. 누적수익은 엔진 `FILL`/`STOP_HIT`의 수수료 포함 순손익만 합산하며
사람 체결·입출금은 넣지 않는다. 수익률의 분모는 당일 최신 `SIZING.wallet`(현재 엔진 운용자본)이다.
승률은 당일 시작해 종료된 캠페인 중 수수료 포함 순손익이 양수인 비율이며 진행 중 캠페인은 제외한다.
캠페인 실현·미실현은 이벤트 상세에서만 표시해 하단과 중복하지 않는다. 잔고는
`logs/state-<SYMBOL>.json` 중 가장 최근 것의 `acct` 를 읽는다(`balance=False` 로 잔고·가용을 끈다). 거래소를 호출하지
않으며 상태가 2분 넘게 낡으면 잔고 값에 나이를 표시한다.

당일 표시값을 중간에 새로 시작할 때는 `logs/notify-baseline.json`에 `day`, `start`, `wallet`을 둔다.
그 날의 누적 손익·수익률·캠페인 횟수·승률은 `start` 이후 엔진 이벤트만 세고, 원본 로그와 엔진 장부는 바꾸지 않는다.

규약: 원본 JSON·로그 줄·oid·내부 경로·`.env` 값(webhook 포함)은 절대 본문에 넣지 않는다.
이벤트를 한두 줄로 가공해 보내고, 루틴 `SIZING`·변화 없는 `PARAMS` 는 보내지 않는다.

C 알림은 track="C", data_dir=<C data>, env_path=<기존 서버 .env>를 명시한다.
C 계기판은 해당 SQLite 장부와 status.json만 읽고 KST 당일 손익·전액 복리 자본을
원화로 표시한다. C 데이터가 없으면 확인 불가로 표시하며 A/B 장부를 대신 읽지 않는다."""
from common.paths import runtime_root
import glob
import http.client
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(runtime_root(ROOT), "logs")

# 제목 접두 → (이모지, 좌측 색 바)
KINDS = {
    "진입":       ("🟢", "#2f81f7"),
    "추가":       ("🔵", "#2f81f7"),
    "부분청산":   ("💰", "#1a7f37"),
    "전량청산":   ("✅", "#1a7f37"),
    "손절":       ("🔻", "#cf222e"),
    "이상":       ("🚨", "#cf222e"),
    "복구":       ("🩹", "#bf8700"),
    "종목변경":   ("🔄", "#8250df"),
    "야간리포트": ("🌙", "#6e7781"),
    "정산":       ("💵", "#1a7f37"),
    "상태":       ("📊", "#6e7781"),
    "부팅":       ("⚙️", "#6e7781"),
}


class NotificationError(RuntimeError):
    """Only sanitized diagnostics may escape a request containing a webhook."""
    def __init__(self, message, *, status=None, uncertain=False):
        super().__init__(message)
        self.status, self.uncertain = status, uncertain


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NotificationError("notify: redirect refused", status=code)


def _hook(env_path=None):
    """`.env` 의 slack-webhook-url. 값은 반환만 하고 절대 출력·기록하지 않는다."""
    try:
        raw = Path(env_path or os.path.join(ROOT, ".env")).read_bytes()
        body = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig")
    except (OSError, UnicodeError):
        raise NotificationError("notify: webhook configuration unreadable") from None
    values = set()
    for ln in body.splitlines():
        k, sep, v = ln.strip().partition("=")
        if sep and k.lower().replace("_", "-") == "slack-webhook-url":
            values.add(v.strip().strip("\"'"))
    if len(values) != 1:
        raise NotificationError("notify: webhook missing or conflicting")
    hook = values.pop()
    try:
        parsed = urllib.parse.urlsplit(hook)
    except ValueError:
        raise NotificationError("notify: invalid webhook destination") from None
    if parsed.scheme != "https" or parsed.netloc not in ("hooks.slack.com", "hooks.slack-gov.com") or not parsed.path.startswith("/services/") or parsed.query or parsed.fragment:
        raise NotificationError("notify: invalid webhook destination")
    return hook


def _latest_state():
    """가장 최근 `logs/state-*.json` 의 (나이, 상태)."""
    best = None
    for p in glob.glob(os.path.join(LOGS, "state-*.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                s = json.load(fh)
        except Exception:
            continue
        age = time.time() - os.path.getmtime(p)
        if best is None or age < best[0]:
            best = (age, s)
    return best


def _engine_day(day):
    """KST 당일 엔진 순손익·운용자본·캠페인/승패. 수동 장부·계좌 흐름은 제외한다."""
    cutoff = None; wallet = None
    try:
        with open(os.path.join(LOGS, "notify-baseline.json"), encoding="utf-8") as fh:
            baseline = json.load(fh)
        if baseline.get("day") == day:
            cutoff = baseline.get("start")
            if float(baseline.get("wallet") or 0.0) > 0:
                wallet = float(baseline["wallet"])
    except (OSError, ValueError, TypeError):
        pass
    pnl = 0.0; campaigns = 0; wins = 0; closed = 0
    active = {}
    pending_stops = {}

    def finish(key):
        nonlocal wins, closed
        campaign_pnl = active.pop(key, None)
        pending_stops.pop(key, None)
        if campaign_pnl is not None:
            closed += 1
            if campaign_pnl > 1e-9:
                wins += 1

    try:
        with open(os.path.join(LOGS, "events.jsonl"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if f'"t": "{day}' not in line: continue
                try: j = json.loads(line)
                except ValueError: continue
                if cutoff and (j.get("t") or "") < cutoff: continue
                ev = j.get("ev")
                if ev in ("FILL", "STOP_HIT"):
                    event_pnl = float(j.get("pnl") or 0.0)
                    pnl += event_pnl   # 엔진 pnl은 이미 수수료 차감
                    key = (j.get("symbol"), j.get("side"))
                    if ev == "FILL":
                        if key in pending_stops:
                            finish(key)
                        role = j.get("role")
                        qty = float(j.get("qty") or 0.0)
                        pos_qty = float(j.get("pos_qty") or 0.0)
                        if role == "buy" and qty > 0 and pos_qty <= qty + 1e-9 and key not in active:
                            active[key] = 0.0
                            campaigns += 1
                        if key in active:
                            active[key] += event_pnl
                            if role != "buy" and pos_qty <= 1e-9:
                                finish(key)
                    elif key in active:
                        oid = j.get("oid") or j.get("sec") or j.get("t")
                        prior = pending_stops.get(key)
                        if prior is not None and prior != oid:
                            finish(key)
                        if key in active:
                            active[key] += event_pnl
                            pending_stops[key] = oid
                elif ev == "SIZING" and float(j.get("wallet") or 0.0) > 0:
                    wallet = float(j["wallet"])
    except OSError:
        pass
    for key in list(pending_stops):
        finish(key)
    return pnl, wallet, campaigns, wins, closed


def summary_fields(day=None, balance=True, *, track="AB", data_dir=None):
    """이벤트 상세 아래에 붙는 공통 계기판 필드."""
    if track == "SPECIAL-ARX":
        # ARX cards carry their own immutable USDT campaign snapshot. Never
        # append the A/B dashboard or the C KRW ledger to a special-track card.
        return []
    if track == "C":
        from track_c.ops.notices import summary_fields as c_summary
        return c_summary(data_dir, day=day, balance=balance)
    if track not in ("AB", "A", "B"):
        raise ValueError("notify: unknown track")
    day = day or time.strftime("%Y-%m-%d")
    pnl, wallet, campaigns, wins, closed = _engine_day(day)
    out = []
    latest = _latest_state()
    a = (latest[1].get("acct") or {}) if latest else {}
    if balance and a.get("equity") is not None:
        age = latest[0]; stale = f" · {int(age // 60)}분 전" if age > 120 else ""
        out.extend((("잔고", f"${float(a['equity']):,.2f}{stale}"),
                    ("가용", f"${float(a.get('avail', a['equity'])):,.2f}")))
    out.append(("누적 손익", f"{pnl:+,.2f} USDT"))
    out.append(("엔진 수익률", f"{pnl / wallet * 100:+,.2f}%" if wallet else "계산 불가"))
    out.append(("캠페인 횟수", f"{campaigns:,}회"))
    out.append(("승률", f"{wins / closed * 100:.1f}% · {wins}승/{closed}회" if closed else "계산 전 · 0회 종료"))
    return out


def _blocks(kind, head, fields=None, lines=None, ctx=None, balance=True, *, track="AB", data_dir=None):
    emoji, _ = KINDS.get(kind, ("•", "#6e7781"))
    blocks = [{"type": "header",
               "text": {"type": "plain_text", "text": f"{emoji}  {kind} · {head}", "emoji": True}}]
    has_detail = bool(fields or lines)
    if fields:
        fs = [{"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in fields]
        for i in range(0, len(fs), 10):
            blocks.append({"type": "section", "fields": fs[i:i + 10]})
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    metrics = [{"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in summary_fields(balance=balance, track=track, data_dir=data_dir)]
    if metrics:
        if has_detail:
            blocks.append({"type": "divider"})
        blocks.append({"type": "section", "fields": metrics})
    if ctx:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]})
    return blocks


def send(kind, head, fields=None, lines=None, ctx=None, balance=True, *, track="AB", data_dir=None, env_path=None):
    """한 건을 보낸다. 성공하면 (200, 'ok')."""
    _, color = KINDS.get(kind, ("•", "#6e7781"))
    blocks = _blocks(kind, head, fields, lines, ctx, balance, track=track, data_dir=data_dir)
    req = urllib.request.Request(
        _hook(env_path), data=json.dumps({"attachments": [{"color": color, "blocks": blocks}]}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=15) as r:
            status, body = r.status, r.read(1024).decode()
        if status != 200 or body.strip() != "ok":
            raise NotificationError("notify: delivery acknowledgement unavailable", status=status, uncertain=True)
        return status, "ok"
    except urllib.error.HTTPError as exc:
        raise NotificationError(f"notify: HTTP {int(exc.code)}", status=int(exc.code)) from None
    except (OSError, UnicodeError, http.client.HTTPException):
        raise NotificationError("notify: network failure; delivery uncertain", uncertain=True) from None


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "-"
    p = json.load(sys.stdin if src == "-" else open(src, encoding="utf-8"))
    try:
        print(send(p["kind"], p["head"], p.get("fields"), p.get("lines"), p.get("ctx"),
                   p.get("balance", True), track=p.get("track", "AB"), data_dir=p.get("data_dir"), env_path=p.get("env_path")))
    except NotificationError as exc:
        print(json.dumps(dict(ok=False, error=str(exc), status=exc.status, delivery_uncertain=exc.uncertain)))
        raise SystemExit(1)
