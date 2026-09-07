# ARX 검증 보고서

검증 기준 시각은 2026-09-07 KST다. 코드 경로 검증과 전략 수익성 검증은
별개이며, 이 보고서의 통과 결과는 현재 매수 신호나 기대수익을 뜻하지 않는다.

## 실제 공개 데이터 증거

API 키 없이 Bitget UTA v3의 unsigned 공개 `GET`만 호출해 한 번의 수집·저장·
재읽기를 완료했다. 최종 결과는 레포 밖
`D:\repos\trading-room-state\track-special-arx\marketdata-final-audit`에 보존했다.

- 최초/마지막 수신: `2026-09-07T02:51:25.571540+09:00` /
  `2026-09-07T02:51:27.988017+09:00`
- stream 27개, 저장 1,517건, 독립 재읽기 1,517건
- coverage 1,505건: 12개 candle stream에서 현재 진행 중인 마지막 1건씩 제외
- 기본 연구축은 현물 market `4H/1H/5m`, 선물 market/mark/index `4H/1H/5m`
- endpoint 오류 0건; liquidation history는 빈 응답이어서 gap 1건으로 기록
- ARX 현물과 `ARXUSDT` USDT perpetual identity 모두 검증
- private request, 주문, 계정/레버리지 설정, 이체: 0건
- receipt:
  `D:\repos\trading-room-state\track-special-arx\marketdata-final-audit\snapshot_receipts.jsonl`

당시 공개 응답은 선물이 online, 가격 tick `0.00001`, 수량 step/minimum `1`,
최소 명목 `5 USDT`, 최대 레버리지 `20`, funding interval `4`, maker/taker
`.0002`/`.0006`, tier 1 범위 `0–5000` 및 MMR `.025`임을 보였다. 이는 해당
시점 관찰값일 뿐 live 설정 보증이 아니다. instrument 응답에는 `settleCoin`이
없었고, USDT 선형 계약 및 base-coin 수량은 문서화된 `USDT-FUTURES` category와
주문 계약을 함께 사용해 명시적으로 판정했다.

## 코드·실행 검증

- `python -m unittest discover -s tests/special -t .`: 65 tests, OK
- `python -m unittest discover -s tests -t .`: 643 tests, OK
- strict mypy: 29 source files, no issues
- `python -m compileall -q track_special`: 통과
- `git diff --check`: 통과
- `doctor`: observe, credentials read false, exchange writes 0, live false
- `paper`: exchange writes 0, 명시적인 합성 intent/risk approval 요구
- `replay --scenario gap_collapse`: 합성 gap/청산/보호 실패를 손실로 보고
- `status`: 시작 대사 미완료를 `startup_reconciled=false`로 정직하게 보고
- `validate-live`: private request/write 0건으로 비정상 종료하고 모든 blocker 열거

합성 테스트는 의도·예약·ACK/체결 분리, 32자 deterministic `clientOid`, timeout
후 대사 전 재전송 차단, partial/cancel race, 단조 상태 전이, 보호 공백과 제어
상태를 검증한다. 보호는 server query, order ID, active 상태, trigger 기준,
covered quantity, stop price, freshness/deadline이 모두 확인되어야 active가 된다.
공식 UTA shape에 맞춘 일반 주문과 TPSL 보호주문 serializer는 순수 함수이며
서명·전송 기능이 없다.

독립 Orca reviewer가 모호/거절 UTA 응답을 ACK로 오인할 수 있는 경로와
`assetMode=multi_assets`가 단일 담보 검사에서 누락된 경로를 발견했다. 최종
구현은 성공 code, exact `clientOid`, non-null `orderId`를 모두 요구하고 문서상
모호 code는 `RESULT_UNKNOWN`으로 대사 전 재전송을 막는다. 계정 snapshot은
`assetMode=single_asset`를 명시적으로 요구하며 두 회귀 재현을 자동 테스트한다.

replay는 timestamp, mark/last, 명시 funding settlement, 수수료, 스프레드, 지연,
depth 기반 부분/미체결, 미보호 노출과 동일 봉의 불리한 순서를 반영한다. 장기
하락, 펌프 부재, probe만 체결된 뒤 급등, fake breakout, gap 붕괴, mark/last
괴리, funding 급등·주기 변경, 거래 중단, server stop 거부, 청산 뒤 잔존 주문,
ADL, 재시작 중복 주문의 합성 시나리오를 별도로 포함한다.

## 미검증 및 live 차단 사유

공개/합성 replay에는 충분한 ARX 과거 mark·order book·funding·tier 변화·실제
체결 기록이 없다. 따라서 cash, spot, no-pyramid, pyramid 비교 인터페이스가
있어도 수익성, 강제청산 회피, 실행 가능성을 입증하지 않는다.

인증된 사용자 계정의 UTA/Classic 종류, isolated/basic 조합, one-way mode,
자동 증거금 보충 차단, 전용 계정 분리, 외부 주문·포지션, 실제 position tier와
liquidation price, TPSL 접수·조회·부분체결 coverage·reduce-only 경쟁 동작은
확인하지 않았다. live transport와 private POST signer는 구현하지 않았다.
`live.example.yaml`의 자본, 배율, 위험, funding/보유기간, 청산 완충, 비상 정리,
종료일 및 승인 메타데이터도 모두 미승인/null이다. 그러므로 live는 의도적으로
차단되어 있으며, 이번 개발 결과만으로 승격할 수 없다.
