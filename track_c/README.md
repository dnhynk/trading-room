# C3 — 코인원 선행거래소 공정가 규칙형 메이커 운영

계약은 [bot/CONCEPT-C.md](../bot/CONCEPT-C.md), 현재 수리 감사는 [AUDIT-C3-20260905.md](AUDIT-C3-20260905.md), 최초 연구는 [AUDIT-20260905.md](AUDIT-20260905.md)다. 현재 `c3-rule-v2`이며 운용자본은 계좌 원화 + C 캠페인·이월 잔량의 매수호가 평가액이다. A/B·S1·C2 worker는 재개하지 않는다. 수익성은 아직 미입증이다.

## 구성

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
| `c3_upgrade.py` | live 설정·최신 장부를 보존하는 prepare / switch / resume |
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
python -m track_c.c3_upgrade prepare        # 새 코드 검증·서버 전체 C 테스트, 실행 엔진은 유지
python -m track_c.c3_upgrade switch         # 신규 진입 PAUSE → flat·실제 C 주문 확인 → 코드 전환, 설정 보존
python -m track_c.c3_upgrade resume         # 약 60초 이상 워밍업 뒤 새 코드·공정가·연결 검증 → 자체 PAUSE만 제거
python -m track_c.deploy_c3 install         # 플랫 확인 → C2·별도 녹화 정지 → 백업 → C3 관측 모드 기동
python -m track_c.deploy_c3 go-live         # 건강 확인 → live/funding → PAUSE 제거 → 재시작
python -m track_c.deploy_notify status
python -m unittest track_c.test_c3 track_c.test_runtime track_c.test_portfolio track_c.test_notices track_c.test_coinone
python -m track_c.c3_replay --config track_c/config-c3.json --contracts <contracts.json> --coinone <public files> --leaders <leaders files> --output out.json [--control flip|unconditional]
python -m track_c.fairfit --coinone <public files> --leaders <leaders files> --coins BTC ETH XRP SOL
```

`data/PAUSE`는 신규 진입만 막는다. `data/STOP`/SIGTERM은 C 주문·재고를 정리한 뒤 종료한다. 디스크 여유 1GiB 미만이면 녹화를 멈추고 신규 진입을 막는다. 재기동 시 저장된 캠페인·주문(익절 대기 포함)을 조회해 이어간다.

## 판정 절차

1. 9월 5일 테이프는 개발 표본이다. `evaluation-c3.json`의 고정 미래 구간과 소스/설정을 지키고, 워밍업·마지막 주문 청산을 포함한 테이프와 계약 캡처를 준비한다.
2. 동일 입력으로 기본·flip·unconditional 재생을 수행한다. `marked_net_krw`, `max_drawdown_krw`, `stress_krw`, `residuals`, `quality`를 보고 실현손익만으로 판단하지 않는다. 대기열은 동일 가격의 잔량이며 더 좋은 가격의 호가를 중복 대기열로 합치지 않는다.
3. `python -m track_c.c3_evidence --protocol track_c/evaluation-c3.json --base base.json --flip flip.json --unconditional unconditional.json --output evidence.json`. 평가 창 미완료·코드/설정 변화·대조군 시각 불일치는 HOLD다. 2일 블록 구간은 근사이며 통과도 REVIEW_ONLY, 자동 증액 없음. 별도로 1초 이상 지연·비용 스트레스와 실체결 대조를 확인한다.

## 알림

사용자 지시(2026-09-05): **매수 진입 알림은 보내지 않고 체결 알림은 청산만 보낸다.** 재기동 전 큐에 남은 진입 알림도 suppressed로 보존하고 발송하지 않는다. 매수 체결은 장부·캠페인 통계에 계속 반영한다. 매도 없는 잔량 이월도 발송하지 않는다. `notifications/status.json`의 `trade_notifications: exits_only`로 적용 여부를 확인한다.

기존 릴레이가 새 청산 사유(`take_profit`·`defend`·`stop`·`dust`·`take_rejected`)와 `take` 체결 라벨을 읽으려면 `python -m track_c.deploy_notify install`로 릴레이를 갱신한다. 원화는 소수점 없이 표시하고 내부 Decimal은 유지한다.

## 롤백

현재 C3 갱신의 직전 config·unit·장부 사본은 `data/upgrade-c3/<release>/`에 있다. 롤백도 신규 진입 PAUSE 후 캠페인·실제 C 주문이 없는 상태를 같은 작업 안에서 확인하고, 현재 장부와 호환되는 C3 코드로만 전환한다. live 설정과 최신 장부를 보존한다. C3가 선행거래소를 녹화하므로 별도 leaders 서비스를 동시에 켜지 않는다. `data/migration-c3/`는 과거 C2→C3 이력이며 C2 자동 복원 지시가 아니다. 실거래 뒤 장부를 과거 사본으로 되돌리지 않는다.
