"""감독 세션의 Slack 알림 렌더러 (읽기 전용, 주문을 내지 않는다).

    python -m bot.notify <payload.json>          # 파일로
    python -m bot.notify -                       # stdin 으로
    from bot.notify import send; send(kind, head, fields=[...], lines=[...], ctx="...")

payload = {"kind": "진입", "head": "MUBARAK 롱 · 09-04 00:18 KST",
           "fields": [["매수 (메이커)", "952개 @ 0.02887"], ...],   # 2열 표, 라벨/값
           "lines": ["한 줄 설명", ...],                            # 본문(생략 가능)
           "ctx": "회색 꼬리말"}                                     # 맥락(생략 가능)

`kind` 는 KINDS 의 키이고 제목 접두·이모지·좌측 색 바를 정한다(CLAUDE.md 감독 알림 채널:
`[진입]`·`[추가]`·`[부분청산]`·`[전량청산]`·`[손절]`·`[이상]`·`[복구]`·`[종목변경]`·`[야간리포트]`).
**잔고 줄은 자동이다** — `logs/state-<SYMBOL>.json` 중 가장 최근 것의 `acct` 를 읽어
"잔고 $X · 가용 $Y · 미실현 ±Z" 를 꼬리말 위에 붙인다(`balance=False` 로 끈다). 거래소를
호출하지 않으므로 공짜이고, 엔진이 죽어 상태가 낡았으면 그 나이를 함께 적는다.

규약: 원본 JSON·로그 줄·oid·내부 경로·`.env` 값(webhook 포함)은 절대 본문에 넣지 않는다.
이벤트를 한두 줄로 가공해 보내고, 루틴 `SIZING`·변화 없는 `PARAMS` 는 보내지 않는다."""
import glob
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")

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


def _hook():
    """`.env` 의 slack-webhook-url. 값은 반환만 하고 절대 출력·기록하지 않는다."""
    for ln in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
        k, _, v = ln.strip().partition("=")
        if k == "slack-webhook-url":
            return v.strip().strip('"')
    raise SystemExit("notify: .env 에 slack-webhook-url 이 없다")


def balance_line():
    """가장 최근 `logs/state-*.json` 의 계좌 스냅샷 한 줄. 없으면 None."""
    best = None
    for p in glob.glob(os.path.join(LOGS, "state-*.json")):
        try:
            s = json.load(open(p, encoding="utf-8"))
            a = s.get("acct") or {}
            if a.get("equity") is None:
                continue
        except Exception:
            continue
        age = time.time() - os.path.getmtime(p)
        if best is None or age < best[0]:
            best = (age, a)
    if best is None:
        return None
    age, a = best
    upl = a.get("upl_all") or 0.0
    txt = f"잔고 *${a['equity']:,.2f}* · 가용 ${a.get('avail', a['equity']):,.2f}"
    if abs(upl) >= 0.005:
        txt += f" · 미실현 {upl:+,.2f}"
    if age > 120:                       # 엔진이 멈췄거나 책이 없다 — 숫자를 믿지 말라고 알린다
        txt += f"  (상태 {int(age // 60)}분 전)"
    return txt


def send(kind, head, fields=None, lines=None, ctx=None, balance=True):
    """한 건을 보낸다. 성공하면 (200, 'ok')."""
    emoji, color = KINDS.get(kind, ("•", "#6e7781"))
    blocks = [{"type": "header",
               "text": {"type": "plain_text", "text": f"{emoji}  {kind} · {head}", "emoji": True}}]
    if fields:
        fs = [{"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in fields]
        for i in range(0, len(fs), 10):                 # Slack 은 section 당 필드 10개까지
            blocks.append({"type": "section", "fields": fs[i:i + 10]})
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    tail = [t for t in ((balance_line() if balance else None), ctx) if t]
    if tail:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": t} for t in tail]})
    req = urllib.request.Request(
        _hook(), data=json.dumps({"attachments": [{"color": color, "blocks": blocks}]}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode()


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "-"
    p = json.load(sys.stdin if src == "-" else open(src, encoding="utf-8"))
    print(send(p["kind"], p["head"], p.get("fields"), p.get("lines"), p.get("ctx"),
               p.get("balance", True)))
