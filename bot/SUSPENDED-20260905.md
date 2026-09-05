# A/B 일시정지 기록 — 2026-09-05

사용자: Hunter B 중단, A/B 일시정지·향후 재개 가능성 보존, C 국내 스캘핑에 집중. 남은 포지션은 "시장가 정리".

- 16:26 KST: 원래 params·상태·공유 풀을 `logs/suspensions/track-ab-20260905-162614/`에 보관, PAUSE 후 신규 진입 중단. hunt 감시견과 자식을 종료했다.
- 16:29~31: STOP으로 cycle 종료. 종료 뒤 남은 엔진 DASH 익절 주문 하나를 식별·취소한 다음 시장가 청산했다. 수동/타인 주문의 일괄 취소는 하지 않았다.
- 16:31:18: DASHUSDT long **0.43 @ 69.31**, 주문 `1480033807485259779`. 거래소 체결 이익 +0.0215 USDT, 청산 수수료 0.01192132. 청산 체결만의 순손익 +0.00957868; 최초 진입 수수료는 이미 엔진 장부에 있어 여기서 중복 차감하지 않는다. 이 숫자는 전체 캠페인 수익률이 아니다.
- 16:32:14: 계좌 잔여 포지션 0·엔진 일반 주문 0 확인 후 record/cycle/nightly/sweep 감시견·남은 자식·watch_cycle을 종료했다. hunt는 앞서 종료, select는 원래 미실행. A/B Python 프로세스 잔여 0, 관련 Windows 예약 작업 검색 결과 없음.
- 수동 체결을 `logs/manual.jsonl`에 주문 ID 기준 1회 기록했다. 종료 전 DASH 파생 상태와 풀을 추가 보관하고, 거래소 flat 증거에 맞춰 `logs/state-DASHUSDT.json`만 flat으로 정합했다. 공유 풀은 `Pool.release()`로 DASH 소유분만 반환했다. 엔진 누적 실현/원본 이벤트는 보존, 풀 claim 잔여 0.

`params.json`은 이 중단 작업에서 수정하지 않았다. A/B 전략·숫자·연구 자료는 남아 있다. 루트 STOP/PAUSE와 추적되는 `bot/TRACKS.json`으로 정지 의도를 보존한다. 시작 방지 장치는 감시견과 cycle CLI에 적용된다. 정지 파일이 Git에 포함되지 않는 새 체크아웃에서도 A/B가 `active`가 아니면 시작하지 않는다.

## 나중에 재개할 때

1. 사용자가 재개할 트랙을 지정하면 해당 CONCEPT·RULES·이 기록을 읽는다. 기존 운용 계좌의 포지션·일반 주문·플랜·공유 풀을 새로 조회한다. 이 문서의 0은 중단 당시 관측이다.
2. 현재 params와 아카이브의 `params.json.before`를 비교한다. A/B는 프로필이 다르므로 백업 전체를 무조건 덮어쓰지 않는다. 사용자가 지정한 트랙에 맞춰 hunt/select 소유자와 운용 한도를 정한다.
3. C와 별도 계좌·프로세스·장부인지 확인한다. DASH 예전 보유 상태는 아카이브에만 있으며 현재 상태/풀에는 복원하지 않는다.
4. 사용자 재개 지시에 맞춰 `bot/TRACKS.json`의 해당 트랙만 `active`로 갱신한다. STOP/PAUSE 제거와 감시견 기동은 그 계좌의 안전 확인 후 마지막에 한다. 감독 소유자는 하나만 둔다.
5. 필요한 코드 검증 및 `bot.preflight`, START.build·거래소 상태를 확인하고 Monitor를 시작한다. 중단 작업만을 이유로 과거 감시견을 먼저 켜지 않는다.

감사 파일: `request.json`, `before-stop.json`, `close-intent.json`, `close-fills.json`, `verified-exchange.json`, `processes-stopped.json`, `reconciliation.json`. 운영 아카이브는 로컬 `logs/`에 있으며 Git에 포함되지 않는다. AWS C 배포에 .env·Bitget 장부·A/B 런타임을 함께 업로드하지 않는다.

16:46 최종 조회에서도 계좌 포지션·엔진 주문·엔진 자식은 0이다. 전체 단위 테스트 370개와 기준 재생(+1.326)은 통과했다. 기존 live preflight는 의도적으로 정지한 5개 감시견·낡은 5개 상태에서만 FAIL(10)을 냈다. 중단 전 preflight는 PASS였고, 이 정지 상태의 FAIL은 재기동 지시가 아니다.
