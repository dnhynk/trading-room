# trading-room — 순환매 엔진 레포 규약

이 레포의 세션은 둘 중 하나다. 사용자가 말하지 않으면 감독 세션이다.
- **감독 세션**(기본): `bot/BOOT.md`대로 부팅한다(메모리 → CONCEPT/RULES → preflight → Monitor). 매매의 95%는 결정론 엔진(`bot/cycle.py`)이 하고, 세션은 이상 이벤트 판단·종목/방향·params 한 줄 조정만 한다. 컨텍스트를 가볍게 유지한다 — 코드 수정은 하지 않고 사용자에게 수정 세션을 제안한다.
- **수정 세션**(사용자가 "수정 세션" / "메커니즘 바꾸자"라고 열 때): 메모리 `cycle-harness-plan` → `bot/CONCEPT.md` → `bot/RULES.md` → `bot/NEXT.md`(미뤄둔 작업과 착수 조건)를 읽고 아래 "엔진 변경 절차"대로 일한다. 끝나면 메모리·RULES를 현재 계약으로 갱신하고 한 줄 보고 후 종료한다.

## 두 세션이 동시에 있을 때
- 감독 세션이 프로세스와 `params.json`의 주인이다. 수정 세션은 코드·문서·테스트만 만지고, 재기동은 포지션 0일 때 직접 하거나(재기동 전후를 보고) 감독 세션에 넘긴다. params 변경은 한 세션만, 사용자에게 말하고.
- 같은 파일을 두 세션이 고치지 않는다. 감독 세션은 `START` 이벤트가 자기가 일으킨 게 아니면 RULES.md와 메모리를 다시 읽는다.
- 메모리 갱신은 수정 세션이 한다. 감독 세션은 관측 사실(사고·수치)만 덧붙인다.

## 진실의 순서
`bot/CONCEPT.md`(사용자의 자연어 전략, 최상위 계약) → `bot/RULES.md`(코드가 그것을 구현하는 방식) → `params.json`(숫자). 결정·감사·증거는 메모리 `cycle-harness-plan`. 기억이나 추측이 아니라 파일을 읽고 답한다.

## 계정 규칙 (Bitget 헤지 모드, 사용자가 같은 계정을 가끔 손으로도 쓴다)
- 엔진은 `params.json`의 (symbol, side)를 배타 소유한다 — 시작은 `TRUMPUSDT` long이었고, **종목·방향은 `bot/select.py`(감시견 `select`)가 RULES의 전환 규칙대로 flat에서 자동으로 바꾼다**(`SYMBOL_SWITCH` 알림). `params.json`의 `strat.symbol/side/sides`·`record`는 select가 쓰고, 나머지 키는 사람(감독 세션)이 쓴다. 엔진 주문은 clientOid `cycL-`/`cycS-`, 엔진 스탑은 그 방향의 pos_loss 플랜. 그 밖의 포지션·주문·플랜은 사용자 것이며 절대 건드리지 않는다.
- 거래소 스탑(돈 한도 pos_loss)을 없애는 변경은 하지 않는다. 구조가는 소프트(derisk 근거)다 — B, 사용자 결정 2026-08-30. `bot/trade.py`는 조회(`status`)와 사용자가 시킨 수동 조작에만 쓴다.
- spot에 들어오는 수수료 페이백은 `sweep` 감시견이 선물 계좌로 옮긴다(복리). spot USDT를 다른 데 쓰지 않는다.
- `.env`(API 키)는 읽기만. 출력·전송 금지.

## 엔진 변경 절차 (수정 세션)
CONCEPT/RULES 문장 먼저 → `bot/signal.py`·`bot/cycle.py` 최소 diff → `python -m unittest bot.test_signal bot.test_cycle` → `python -m bot.backtest data/ws/pub-20260829-0[4-6].jsonl.gz`(기준 테이프; 현재 수치는 RULES.md) → `-m bot.cycle` 자식만 Stop-Process(감시견이 5초 뒤 올림; 포지션·스탑·사이즈는 state.json에 남음) → `python -m bot.preflight`. 코드 변경 없는 재기동 금지. `mode`·`sides`(쌍검) 변경은 사용자 승인 후 포지션 0에서만(live 엔진은 플랫이 될 때까지 스스로 보류한다); 심볼과 방향은 select 규칙(RULES 도구 절)이 정한다 — 손으로 바꾸려면 select를 세우고 한다. 숫자는 전부 휴리스틱이고 근거는 야간 리포트·replay·tune(리포트 우선, `--apply`는 승인 후).

## 운영 메모 (Windows)
- 감시견 5개는 PowerShell `Start-Process -WindowStyle Hidden`으로 분리 실행: `python -m bot.supervise record|cycle|nightly|sweep|select`. pid는 `logs/<job>.pid`, 로그는 `logs/<job>.log`.
- 자식 프로세스 찾기: `Get-CimInstance Win32_Process`에서 CommandLine이 `-m bot.cycle`이고 `supervise`가 아닌 것.
- Git Bash heredoc에 한글을 넣으면 깨진다 — 파일은 Write/Edit 도구로.
- 제어 파일(레포 루트): `STOP`(주문 취소 후 종료, 감시견 정지), `PAUSE`(담기만 중단), `RESUME`(HALT 해제). Monitor는 `python -u -m bot.watch_cycle 3600`(알림·체결·재기동·야간 REPORT·60분 HB만). /loop 금지, 루틴 이벤트는 한 줄 또는 무응답.
