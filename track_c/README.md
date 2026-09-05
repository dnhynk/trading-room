# C3 — 코인원 선행거래소 공정가 규칙형 메이커 운영

계약은 [bot/CONCEPT-C.md](../bot/CONCEPT-C.md), 현재 수리 기록은 [REPAIR-C3-20260906.md](REPAIR-C3-20260906.md), 최초 연구는 [AUDIT-20260905.md](AUDIT-20260905.md)다. C3 v4는 BTC만 실매매하며 ETH·XRP·SOL은 기록 전용이다. 신규 주문 시 공정가 편차≥2틱·공정가≥익절가·30/10초 하락 veto/브레이크·코인원 32초 순매수 비중≤0을 적용한다. 2틱은 사용자 지정 기본값이며 수익 최적값이 아니다. 운용자본은 계좌 원화 + C 캠페인·이월 잔량의 매수호가 평가액이다. A/B·S1·C2 worker는 재개하지 않는다. 수익성은 아직 미입증이다.

BTC 단독 전환은 **2026-09-06 00:06:30.977 KST**, 진입 하한2틱 재개는 **00:57:27.869 KST**다. 이는 이전 v3 평가이며 현재 v4 통합 평가는 **2026-09-06T02:59:02.270+09:00부터10×24시간**이다. 사용자 후속 지시로 Slack은 기존 BTC 시작부터 **진입 주문 편차2틱 이상** 캠페인만 집계한다. `data/notifications/baseline.json`의 `coins:["BTC"]`·`entry_dev_min_ticks:2.0`은 날짜가 바뀌어도 유지한다. 해당 캠페인의 모든 부분체결·수수료를 포함하며 편차 미만/불명 캠페인은 손익·수익률 분자·횟수·승률에서 제외한다. 손익은 `+0.9원`처럼 소수점 첫째 자리까지 표시한다. 원본 장부·실제 자본·위험 예산은 변경하지 않으며 과거 Slack 메시지의 삭제/수정도 하지 않는다.

## 구성

2026-09-06 승인된 통합 변경은 [REPAIR-C3-20260906.md](REPAIR-C3-20260906.md), 손절·수량·가치 계산은 [EXIT-C3-20260906.md](EXIT-C3-20260906.md)를 따른다. v4는 기준금액2만원, 진입 시 변동성 손절가 고정, 최소 청산금액105% 수량 보정, 표본이 있을 때의 보유 가치 청산을 사용한다. 실제 전환 여부와 시각은 `bot/TRACKS.json` 및 서버 상태가 진실이다.

| 파일 | 역할 |
|---|---|
| `fair.py` | 선행거래소 잔량가중 중간가(설정 별칭 microprice) × 과거 300초 비율 중앙값의 가중 평균, 편차(틱) |
| `rule.py` | 사전 등록 규칙: 진입/취소/익절/방어/손절/시간, 크기 |
| `c3_runner.py` | 실행기. 코인원 공개·개인 WS, 선행거래소 녹화(`leaders.py` 내장), 포트폴리오 OMS 구동 |
| `oms.py`·`portfolio.py` | 장부. `take` 역할(post-only 지정가 매도), `residuals` 잔량 이월 |
| `leaders.py` | 업비트·빗썸 최상단 호가/체결 녹화(`data/leaders/YYYYMMDD-HH.jsonl.gz`). 엔진이 꺼진 동안만 서비스로 단독 실행 |
| `c3_replay.py` | 녹화 재생(같은 코드 경로), 대조군 `--control flip|unconditional` |
| `fairfit.py` | 축약 회귀 진단·AR(1) 근사 반감기. 공적분/정보지분/가중치 채택 출력 없음 |
| `accounting.py` | 이월 잔량을 포함한 자본·평가손익 |
| `c3_evidence.py` | 고정 미래 창의 일별 순자산·대조군 차이·블록 하한, 자동 증액 없음 |
| `exit_model.py` | 과거300초 희소 변동성 손절과 완료된 과거 경로의 보유 가치 대용치 |
| `http_pool.py`·`recovery.py` | 요청 무재시도 연결 풀, 비정상 종료 뒤 독점 writer로 주문 정합·거래소 보호 |
| `c3_observations.py`·`c3_live_evidence.py` | 비결정 관측 기록과 실제 체결/미체결·수량별 매도 VWAP·원화 비용/지연 분석 |
| `replay_stream.py`·`c3_identity.py` | 시간순 압축 임시 spool·제한된 호가 창·온라인 순자산 집계, 소스/설정 동일성 |
| `c3_upgrade.py` | prepare / switch / resume. 현재 `execution`은 승인된 v4 설정과 실행 보호를 교체한다. 과거 v3는 설정 5개, `--profile btc-only`는 종목 설정 2개, `--profile entry-2`는 `entry_ticks`만 교체. 나머지 live 설정·최신 장부 보존 |
| `deploy_c3.py` | `install`(관측 모드) / `go-live` / `status` |

설정은 `config-c3.json`(체크인 값은 observe/funding false). 서버 `config.json`이 실제 모드다.

## 서비스 (AWS `i-0db0329527b7b3533`)

| 서비스 | 역할 |
|---|---|
| trading-room-c.service | `track_c.c3_runner` (코인원 녹화 + 선행거래소 녹화 + 매매) |
| trading-room-c-leaders.service | 엔진이 내려간 동안의 선행거래소 녹화. 엔진과 동시에 켜지 않는다 |
| trading-room-c-notify.service | 읽기 전용 C Slack 릴레이, 진입 알림 없이 청산 체결 발송 |
| trading-room-c-model.timer | C2 학습. disabled, 켜지 않는다 |

```powershell
python -m track_c.deploy_c3 status          # AWS 신원 확인 + 엔진/공정가/선택/캠페인/선행거래소/알림
python -m track_c.c3_upgrade prepare --profile execution # 승인된 v4/2만원/실행 통합, 나머지 설정 보존
python -m track_c.c3_upgrade switch         # 신규 진입 PAUSE → flat·실제 C 주문 확인 → 코드 전환, 설정 보존
python -m track_c.c3_upgrade resume         # 공정가 약60초 + 변동성300초 워밍업 → 새 코드·연결 검증 → 자체 PAUSE만 제거
python -m track_c.deploy_c3 install         # 플랫 확인 → C2·별도 녹화 정지 → 백업 → C3 관측 모드 기동
python -m track_c.deploy_c3 go-live         # 건강 확인 → live/funding → PAUSE 제거 → 재시작
python -m track_c.deploy_notify status
python -m unittest track_c.test_c3 track_c.test_runtime track_c.test_portfolio track_c.test_notices track_c.test_coinone
python -m track_c.c3_replay --config track_c/config-c3.json --contracts <contracts.json> --coinone <public files> --leaders <leaders files> --output out.json [--control flip|unconditional]
python -m track_c.fairfit --coinone <public files> --leaders <leaders files> --coins BTC ETH XRP SOL
python -m track_c.c3_live_evidence --data-dir <C data> --start-ms <evaluation start> --output <report.json>
```

`data/PAUSE`는 신규 진입만 막는다. `data/STOP`/SIGTERM은 C 주문·재고를 정리한 뒤 종료한다. 디스크 여유 1GiB 미만이면 녹화를 멈추고 신규 진입을 막는다. 재기동 시 저장된 캠페인·주문(익절 대기 포함)을 조회해 이어간다.

`execution` 전환은 약60초 공정가 +300초 변동성 이력이 준비된 뒤 재개한다. 보유 가치 경로20개는 재개 조건이 아니며 부족하면 기존 보호 청산을 계속 쓴다. 주 루프의20초 watchdog과 `ExecStopPost` 복구는 프로세스 장애에 대응한다. 복구는 단일 writer를 인수해 고정 `stop/stop_limit` 주문을 남기거나 이미 손절/시간이 지났으면 소유 수량을 청산한다. 거래소/API/호스트 단절·갭에서도 손실 상한을 보장하는 장치는 아니다. 원래 C 익절은 post-only 대기이며 STOP_LIMIT과 동시에 수량을 예약하지 않는다.

녹화 public 한도16GiB, 루트32GiB, 실제 여유1GiB 보호선을 사용한다. 상태 `storage`가 public/leader/관측 파일의 완료 시간당 증가율과10일 공간 전망을 제공한다. 분석은 열린 gzip의 완전한 줄까지 읽되 불완전 꼬리를 품질 문제로 표시한다. 고정 전향 평가에는 완결·해시 고정된 입력을 사용한다.

## 판정 절차

1. v4 통합 전 관측과 스모크 테이프는 개발 표본이다. `evaluation-c3.json`의 새 설정 재개 시각부터 10×24시간 구간과 소스/설정을 지킨다. 이전 4종목 및 BTC 하한 0틱·v3 하한 2틱 평가는 `evaluations/`에 보존하고 합산하지 않는다. 서버의 현재 사본은 `data/evaluation/evaluation-c3.json`, 불변 사본은 같은 폴더의 `c3-v4-execution-<start_ms>.json`이다. 시작 전 워밍업과 끝의 청산 여유를 포함한 테이프·계약 캡처를 준비하고 재생에 `--start-ms <start_ms> --end-ms <end_ms>`를 전달한다. 시작 전에는 진입하지 않는다.
2. 동일 입력으로 기본·flip·unconditional 재생을 수행한다. `marked_net_krw`, `max_drawdown_krw`, `stress_krw`, `residuals`, `quality`를 보고 실현손익만으로 판단하지 않는다. 대기열은 동일 가격의 잔량이며 더 좋은 가격의 호가를 중복 대기열로 합치지 않는다.
3. `python -m track_c.c3_evidence --protocol track_c/evaluation-c3.json --base base.json --flip flip.json --unconditional unconditional.json --output evidence.json`. 재개 시각에 고정한 24시간 순자산 블록으로 전환 이전 손익을 제외한다. 평가 창 미완료·코드/설정 변화·대조군 시각 불일치는 HOLD다. 2블록 구간은 근사이며 통과도 REVIEW_ONLY, 자동 증액 없음. 별도로 1초 이상 지연·비용 스트레스와 실체결 대조를 확인한다. flip은 편차 부호만 반전하고 모멘텀/flow 게이트는 유지한다. unconditional은 A/B/C·편차 취소/방어·브레이크를 제거하되 기존 데이터 가용성·익절·손절·시간·실행 위험 규칙을 유지한다.

## 알림

사용자 지시(2026-09-05): **매수 진입 알림은 보내지 않고 체결 알림은 청산만 보낸다.** 재기동 전 큐에 남은 진입 알림도 suppressed로 보존하고 발송하지 않는다. 매수 체결은 장부·캠페인 통계에 계속 반영한다. 매도 없는 잔량 이월도 발송하지 않는다. `notifications/status.json`의 `trade_notifications: exits_only`로 적용 여부를 확인한다.

기존 릴레이가 새 청산 사유(`take_profit`·`defend`·`stop`·`brake`·`dust`·`take_rejected`)와 `take` 체결 라벨을 읽으려면 `python -m track_c.deploy_notify install`로 릴레이를 갱신한다. 손익은 원화 소수점 첫째 자리, 잔고·가격은 정수로 표시하고 내부 Decimal은 유지한다.

## 롤백

현재 C3 갱신의 직전 config·unit·장부 사본은 `data/upgrade-c3/<release>/`에 있다. 롤백도 신규 진입 PAUSE 후 캠페인·실제 C 주문이 없는 상태를 같은 작업 안에서 확인하고, 현재 장부와 호환되는 C3 코드로만 전환한다. live 설정과 최신 장부를 보존한다. C3가 선행거래소를 녹화하므로 별도 leaders 서비스를 동시에 켜지 않는다. `data/migration-c3/`는 과거 C2→C3 이력이며 C2 자동 복원 지시가 아니다. 실거래 뒤 장부를 과거 사본으로 되돌리지 않는다.
