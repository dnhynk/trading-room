# trading-room — 순환매 엔진 레포 규약

## 현재 트랙 C 운영 (2026-09-05)

C는 C3 `track_c.c3_runner`(선행거래소 공정가 규칙형 메이커)와 state v3 포트폴리오를 사용한다. `bot/CONCEPT-C.md`가 계약, `track_c/README.md`가 운영, `track_c/AUDIT-20260905.md`가 근거다. `python -m track_c.deploy_c3 status`로 mode·PAUSE·공정가·선행거래소 연결·캠페인·잔량 이월을 확인한다. C2 학습형 정책·`deploy_quant`·모델 worker는 폐기됐고 재기동하지 않는다. Slack 원화는 소수점 없이 표시한다. 아래 A/B 변경·감독 규칙은 A/B에 해당하며 C 배포는 C의 flat·장부 호환성 절차를 따른다.

`bot/TRACKS.json`과 `bot/BOOT.md`의 현재 운영 상태를 먼저 읽는다. A/B는 사용자 지시로 일시정지했고 아래 A/B 감시견·Monitor를 자동 재개하지 않는다. C는 AWS `trading-room-c.service`로 실거래하며 운용자본은 계좌 잔액 전체 복리다. C 계약은 `bot/CONCEPT-C.md`다. 알림은 독립 `trading-room-c-notify.service`가 `bot.notify`의 C 원화 경로로 보낸다. C `data/ledger.sqlite`/`status.json`만 읽으며 A/B USDT 계기판을 섞지 않는다. `python -m track_c.deploy_notify status`로 기존 worker를 확인하고 중복 릴레이를 시작하지 않는다. C 알림의 당일은 KST이며 엔진의 UTC 일일 위험 제한과 구분한다. C 알림 작업 때문에 매매 엔진을 재기동하지 않는다. 상세 운영·장애 확인은 `track_c/README.md`다.

이 레포의 세션은 둘 중 하나다. 사용자가 말하지 않으면 감독 세션이다.
- **감독 세션**(기본): `bot/BOOT.md`대로 부팅한다(메모리 → CONCEPT/RULES → preflight → Monitor). 매매의 95%는 결정론 엔진(`bot/cycle.py`)이 하고, 세션은 이상 이벤트 판단·종목/방향·params 한 줄 조정만 한다. 컨텍스트를 가볍게 유지한다 — 코드 수정은 하지 않고 사용자에게 수정 세션을 제안한다.
- **수정 세션**(사용자가 "수정 세션" / "메커니즘 바꾸자"라고 열 때): 메모리 `cycle-harness-plan` → `bot/CONCEPT.md`(+ 트랙 B를 만지면 `bot/CONCEPT-B.md`) → `bot/RULES.md` → `bot/NEXT.md`(미뤄둔 작업과 착수 조건)를 읽고 아래 "엔진 변경 절차"대로 일한다. 끝나면 메모리·RULES를 현재 계약으로 갱신하고 한 줄 보고 후 종료한다.

## 두 세션이 동시에 있을 때
- 감독 세션이 프로세스와 `params.json`의 주인이다. 수정 세션은 코드·문서·테스트만 만지고, 재기동은 포지션 0일 때 직접 하거나(재기동 전후를 보고) 감독 세션에 넘긴다. params 변경은 한 세션만, 사용자에게 말하고.
- 같은 파일을 두 세션이 고치지 않는다. 감독 세션은 `START` 이벤트가 자기가 일으킨 게 아니면 RULES.md와 메모리를 다시 읽는다.
- 메모리 갱신은 수정 세션이 한다. 감독 세션은 관측 사실(사고·수치)만 덧붙인다.

## 진실의 순서
`bot/CONCEPT.md`(트랙 A = 순환매의 최상위 계약) · **`bot/CONCEPT-B.md`(트랙 B = 작전코인 세력대항, 순환매가 아니다 — 두 문서는 서로의 문장을 상속하지 않는다)** → `bot/RULES.md`(코드가 그것을 구현하는 방식) → `params.json`(숫자). 결정·감사·증거는 메모리 `cycle-harness-plan`. 기억이나 추측이 아니라 파일을 읽고 답한다.

## 계정 규칙 (Bitget 헤지 모드 + 크로스 마진(2026-08-30, 쌍검용; 주문은 계정의 marginMode를 따라간다), 사용자가 같은 계정을 가끔 손으로도 쓴다)
- 엔진은 `params.json`의 (symbol, sides)를 배타 소유한다 — 2026-08-30 22:36부터 쌍검(`sides: ["long","short"]`, 사용자 승인). **종목은 바구니다**: `bot/select.py`(감시견 `select`)가 `books`를 `select.n`(4)종목 균등으로 유지한다 — 자격을 잃은 책은 `wind_down`으로 담기를 멈춰 flat이 되면 빼고(`BOOK_WIND_DOWN`/`BOOK_DROP`), 빈 슬롯은 점수 순으로 채운다(`BOOK_ADD`). **순위 때문에 들고 있는 종목을 갈아치우지는 않는다**(RULES scan·select 절). `params.json`의 `books`·`strat.symbol/side/sides`·`record`는 select가 쓰고, 나머지 키는 사람(감독 세션)이 쓴다. 엔진 주문은 clientOid `cycL-`/`cycS-`, 엔진 스탑은 그 방향의 pos_loss 플랜. 그 밖의 포지션·주문·플랜은 사용자 것이며 절대 건드리지 않는다.
- 거래소 스탑(돈 한도 pos_loss)을 없애는 변경은 하지 않는다. 구조가는 소프트(derisk 근거)다 — B, 사용자 결정 2026-08-30. `bot/trade.py`는 조회(`status`)와 사용자가 시킨 수동 조작에만 쓴다.
- spot에 들어오는 수수료 페이백은 `sweep` 감시견이 선물 계좌로 옮긴다(복리). spot USDT를 다른 데 쓰지 않는다.
- `.env`(API 키)는 읽기만. 출력·전송 금지.

## 엔진 변경 절차 (수정 세션)
CONCEPT/RULES 문장 먼저 → `bot/signal.py`·`bot/cycle.py` 최소 diff → `python -m unittest bot.test_signal bot.test_cycle` → `python -m bot.backtest data/ws/pub-20260829-0[4-6].jsonl.gz`(기준 테이프; 현재 수치는 RULES.md) → `-m bot.cycle` 자식만 Stop-Process(감시견이 5초 뒤 올림; 포지션·스탑·사이즈는 state.json에 남음) → `python -m bot.preflight`. 코드 변경 없는 재기동 금지. `mode`·`sides`(쌍검) 변경은 사용자 승인 후 포지션 0에서만(live 엔진은 플랫이 될 때까지 스스로 보류한다); 심볼과 방향은 select 규칙(RULES 도구 절)이 정한다 — 손으로 바꾸려면 select를 세우고 한다. 숫자는 전부 휴리스틱이고 근거는 야간 리포트·replay·tune(리포트 우선, `--apply`는 승인 후).

## 운영 메모 (Windows)
- 감시견 5개는 PowerShell `Start-Process -WindowStyle Hidden`으로 분리 실행: `python -m bot.supervise record|cycle|nightly|sweep|select`(트랙 B 는 select 대신 `hunt`). pid는 `logs/<job>.pid`, 로그는 `logs/<job>.log`. `cycle` 감시견은 `books`의 심볼마다 자식 엔진 하나를 띄운다(`logs/cycle-<SYMBOL>.log`) — 심볼이 늘어도 감시견은 5개다.
- **감시견을 세우면 자식도 같이 죽는다**(job object, 2026-09-03 빌드부터). 그 전 빌드로 뜬 감시견을 세울 땐 자식(`-u -m bot.ws record` 등)이 살아남으니 같은 명령에서 자식까지 세운다 — 고아 recorder 가 새 recorder 와 같은 테이프를 쓰면 중복·깨진 줄이 생긴다(2026-09-03 15:30~20:02 에 당함).
- 자식 프로세스 찾기: `Get-CimInstance Win32_Process`에서 CommandLine이 `-m bot.cycle`이고 `supervise`가 아닌 것.
- **파괴적 명령은 안전 확인과 같은 명령 안에서 한다**(2026-09-02에 당함: flat 확인 assert를 앞 명령에 두고 `Stop-Process`를 뒷 명령에 뒀더니, assert가 실패했는데도 kill이 그대로 나가 포지션을 든 엔진이 죽었다 — 감시견이 6초 만에 복구하고 스탑도 되살아났지만 운이 좋았다). 그리고 **CommandLine 글롭은 그 명령을 실행 중인 셸 자신도 잡는다** — 필터에 심볼을 넣으면 그 문자열이 든 bash/powershell까지 매칭된다. `-m bot.cycle <SYM>`으로 정확히 맞추고 `bash`·`powershell` 제외.
- Git Bash heredoc에 한글을 넣으면 깨진다 — 파일은 Write/Edit 도구로.
- 제어 파일(레포 루트): `STOP`(주문 취소 후 종료, 감시견 정지), `PAUSE`(담기만 중단), `RESUME`(HALT 해제). Monitor는 `python -u -m bot.watch_cycle 3600`(알림·체결·재기동·야간 REPORT·60분 HB만). /loop 금지, 루틴 이벤트는 한 줄 또는 무응답.

## 감독 알림 채널 (사용자 지시 2026-09-03)
- 감독 세션은 부팅·preflight 뒤 `python -u -m bot.watch_cycle 3600`에 **무기한** 붙는다. 체결(`FILL`)·전량 청산(`CLOSE`)·손절·HALT·ERROR·웹소켓 장애·감시견 재기동·종목 교체·야간 리포트·SWEEP·60분 HB 등 Monitor가 내는 운영 이벤트는 채팅 세션에 중계하지 않고 `.env`의 `slack-webhook-url`로 보낸다.
- **전송은 `python -m bot.notify <payload.json>`으로 한다**(`bot/notify.py`, 읽기 전용). curl로 텍스트를 직접 쏘지 않는다 — 세션이 바뀌어도 알림 모양이 같아야 한다. payload는 `{"kind", "head", "fields": [[라벨, 값], ...], "lines": [...], "ctx"}`이고 `kind`가 제목 접두·이모지·색 바를 정한다(아래 목록이 그대로 `KINDS`의 키다). **모든 카드는 제목 바로 아래에 이벤트 상세를 먼저 놓고, 그 아래 공통 계기판 `잔고 | 가용 | 누적 손익 | 엔진 수익률 | 캠페인 횟수 | 승률`을 자동으로 붙인다(하단 context에는 넣지 않는다). 캠페인 실현·미실현은 이벤트 상세에만 두어 중복하지 않는다.** 누적 손익은 KST 당일 엔진 `FILL`/`STOP_HIT`의 수수료 포함 순손익만 합산하고 사람 체결·입출금·계좌 증감은 제외한다. 캠페인 횟수는 같은 날 엔진 포지션이 flat에서 첫 진입 체결된 횟수다(동일 주문 부분체결은 1회). 승률은 같은 날 시작해 종료된 캠페인 중 수수료 포함 순손익이 양수인 비율이며 진행 중 캠페인은 제외한다. 엔진 수익률은 이 손익을 당일 **최신** `SIZING.wallet`(입금 등 자본 변동을 반영한 현재 엔진 운용자본)로 나눈다. 잔고·가용은 최신 `logs/state-<SYMBOL>.json`을 읽으며 상태가 2분 넘게 낡으면 잔고에 나이를 표시한다(`balance: false`는 잔고·가용만 끄고 엔진 손익·수익률·캠페인 횟수·승률은 유지한다). 당일 계기판을 중간에 초기화하라는 지시가 있으면 `logs/notify-baseline.json`의 `day`·`start`·`wallet` 이후만 네 캠페인 지표에 반영한다. 이는 표시 기준선이며 원본 이벤트와 엔진 장부는 수정하지 않는다.
- **원본 JSON/로그 줄을 그대로 보내지 않는다.** 이벤트를 해석해 한두 줄로 가공하고 필요한 정보만 보낸다. 제목은 `[진입]`·`[추가]`·`[부분청산]`·`[전량청산]`·`[손절]`·`[이상]`·`[복구]`·`[종목변경]`·`[야간리포트]`처럼 즉시 구분되게 한다. 공통 필드는 `KST 시각 | 종목 방향 | 핵심 결과`이며, 체결은 역할·수량·가격·수수료 포함 순손익·남은 수량·당일 누적 실현손익, 이상은 원인·현재 포지션/스탑·자동 복구 여부·필요 조치만 적는다. 긴 신호 특성값, oid, 내부 경로, 변화 없는 필드는 생략한다. 같은 주문의 연속 부분체결은 가능하면 합쳐 한 건으로 보낸다.
- 루틴 `SIZING`은 단독 전송하지 않는다. 60분 HB는 현재 `params.books`의 활성 책만 `상태 | 포지션·평단·미실현 | 스탑 | 당일 실현 | WS/errors` 순으로 요약한다. 과거에 빠진 책의 stale state나 변화 없는 신호 목록은 보내지 않는다.
- webhook 값은 읽기만 하고 화면·로그·명령행·오류 메시지에 절대 노출하지 않는다. Slack 전송 본문에도 webhook이나 다른 `.env` 값을 넣지 않는다.
- Slack 릴레이는 한 세션만 소유한다. 시작 전에 기존 `watch_cycle`/Slack 릴레이를 확인해 중복 알림을 만들지 않는다. 새 릴레이는 시작 시점의 파일 끝에서 따라가므로 과거 이벤트를 재전송하지 않는다.
- Slack 전송 실패, Monitor 종료, preflight FAIL처럼 Slack만으로 정상 감독을 지속할 수 없는 경우에만 채팅 세션에 짧게 보고한다. 사용자가 중단을 명시할 때까지 감독을 끝내거나 완료 응답을 보내지 않는다.
