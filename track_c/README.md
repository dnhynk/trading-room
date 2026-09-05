# C3 — 코인원 선행거래소 공정가 규칙형 메이커 운영

계약은 [bot/CONCEPT-C.md](../bot/CONCEPT-C.md), 근거는 [AUDIT-20260905.md](AUDIT-20260905.md). 운용자본은 계좌 원화 전액 + C 보유수량의 매수호가 평가액이며 복리다. A/B·S1은 재개하지 않는다. C2 문서(`MODEL.md`, `VALIDATION-C2.md`, `README-C1.md`)는 역사 기록이다.

## 구성

| 파일 | 역할 |
|---|---|
| `fair.py` | 선행거래소 마이크로프라이스 × 과거 300초 비율 중앙값의 가중 평균, 편차(틱) |
| `rule.py` | 사전 등록 규칙: 진입/취소/익절/방어/손절/시간, 크기 |
| `c3_runner.py` | 실행기. 코인원 공개·개인 WS, 선행거래소 녹화(`leaders.py` 내장), 포트폴리오 OMS 구동 |
| `oms.py`·`portfolio.py` | 장부. `take` 역할(post-only 지정가 매도), `residuals` 잔량 이월 |
| `leaders.py` | 업비트·빗썸 최상단 호가/체결 녹화(`data/leaders/YYYYMMDD-HH.jsonl.gz`). 엔진이 꺼진 동안만 서비스로 단독 실행 |
| `c3_replay.py` | 녹화 재생(같은 코드 경로), 대조군 `--control flip|unconditional` |
| `fairfit.py` | 오차수정 계수·반감기·Gonzalo–Granger 가중치·정보지분 진단 |
| `deploy_c3.py` | `install`(관측 모드) / `go-live` / `status` |

설정은 `config-c3.json`(체크인 값은 observe/funding false). 서버 `config.json`이 실제 모드다.

## 서비스 (AWS `i-0db0329527b7b3533`)

| 서비스 | 역할 |
|---|---|
| trading-room-c.service | `track_c.c3_runner` (코인원 녹화 + 선행거래소 녹화 + 매매) |
| trading-room-c-leaders.service | 엔진이 내려간 동안의 선행거래소 녹화. 엔진과 동시에 켜지 않는다 |
| trading-room-c-notify.service | 읽기 전용 C Slack 릴레이(변경 없음) |
| trading-room-c-model.timer | C2 학습. disabled, 켜지 않는다 |

```powershell
python -m track_c.deploy_c3 status          # AWS 신원 확인 + 엔진/공정가/선택/캠페인/선행거래소/알림
python -m track_c.deploy_c3 install         # 플랫 확인 → C2·별도 녹화 정지 → 백업 → C3 관측 모드 기동
python -m track_c.deploy_c3 go-live         # 건강 확인 → live/funding → PAUSE 제거 → 재시작
python -m track_c.deploy_notify status
python -m unittest track_c.test_c3 track_c.test_runtime track_c.test_portfolio track_c.test_notices track_c.test_coinone
python -m track_c.c3_replay --config track_c/config-c3.json --contracts <contracts.json> --coinone <public files> --leaders <leaders files> --output out.json [--control flip|unconditional]
python -m track_c.fairfit --coinone <public files> --leaders <leaders files> --coins BTC ETH XRP SOL
```

`data/PAUSE`는 신규 진입만 막는다. `data/STOP`/SIGTERM은 C 주문·재고를 정리한 뒤 종료한다. 디스크 여유 1GiB 미만이면 녹화를 멈추고 신규 진입을 막는다. 재기동 시 저장된 캠페인·주문(익절 대기 포함)을 조회해 이어간다.

## 판정 절차

1. 5거래일 이상 `data/public`·`data/leaders`를 모은다(엔진 또는 leaders 서비스 중 하나가 항상 녹화).
2. `c3_replay`를 기본·flip·unconditional로 돌려 캠페인(서로 다른 체결 이벤트)과 KST 일별 결과를 본다.
3. CONCEPT-C의 채택 기준을 만족할 때만 크기를 늘린다. 변형은 `entry_ticks +0.5`, `leader_price mid`, `leader_weights`(fairfit) 세 가지뿐이다.

## 알림

기존 릴레이가 새 청산 사유(`take_profit`·`defend`·`stop`·`dust`·`take_rejected`)와 `take` 체결 라벨을 읽으려면 `python -m track_c.deploy_notify install`로 릴레이를 갱신한다. 원화는 소수점 없이 표시하고 내부 Decimal은 유지한다.

## 롤백

`data/migration-c3/<release>/`에 설치 직전 config·unit·장부 사본이 있다. 롤백은 플랫 확인 → `trading-room-c.service` 정지 → unit/config 복원 → `trading-room-c-leaders.service` 재개 순서다. 실거래 뒤 장부를 과거 사본으로 되돌리지 않는다.
