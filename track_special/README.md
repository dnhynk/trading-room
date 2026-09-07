# Special Track — ARX Futures Campaign

ARX의 독립 USDT 선형 무기한 선물 long-only 연구 트랙이다. 현재 사용자 목표는 **본격 상승 전에 Bitget 전체 가용 USDT를 기준으로 약 10배 롱 노출을 분할 매집하며, 해당 시드 전액 손실을 감수하는 것**이다. 평균가 아래 추가 매수를 허용하되 최초 시드와 10배 상한을 늘리지 않는다. 현재 구현은 Classic v2 계좌 조회, 공개 시세에 따른 적응형 분할 매집의 모의 운전과 Slack 모의 체결 보고다. 자본 정의와 구현 범위는 [선행 보유 목표](docs/preposition_objective.md)에 기록한다.

기존 `aggressive_bounded_research`와 작은 `PROBE` 이후 수익 중 `BUILD`하는 UTA 전략은 비교 기준으로 보존한다. 단일 전액 주문인 `full_seed_10x_preposition_research`도 비교 설정이며 현재 매집기에 사용하지 않는다. 기본 실행 설정은 기존 관측 모드를 유지한다.

이 트랙은 기존 Track B를 이름만 바꾼 구현이 아니다. B의 포지션·주문·장부·자본 풀을 가져오지 않고, Track C의 Coinone 원화 장부나 운영 프로세스와도 연결하지 않는다. 현재 운영 계약은 `observe`, `live_enabled=false`이며 개발 세션에서 실주문, 계정 모드 변경, 레버리지 변경, 이체를 수행하지 않는다.

핵심 경계는 다음과 같다.

- 데이터: ARX 현물과 `ARXUSDT` 선물을 별도 series로 저장하고 exchange/receive time, 순서, 중복, 결측을 보존한다.
- 전략: `WATCH → PROBE → BUILD → RIDE → HARVEST → FLAT → COOLDOWN`은 주문을 만들지 않고 의도만 만든다.
- 위험: 기존 포지션과 전송 중·미체결·결과 미확정 예약을 한 트랜잭션에서 합산한다. 원금 손실, 이익 반납, gross stop risk, 명목금액, 격리증거금, 청산 완충을 독립 제한한다.
- 실행: risk gate의 짧은 수명 승인을 발송 직전에 다시 검증한다. ACK, 체결, 보호 확인을 서로 다른 상태로 관리한다.
- 원장: 증거금 반환, 청산 명목금액, 미실현이익을 실현이익으로 기록하지 않는다. 실현 수수료·펀딩의 중복 차감을 금지한다.

기존 실행 경로는 다음과 같다. `collect`와 `observe`는 unsigned 공개 GET을
호출한다. 별도 `accumulate` 명령은 명시한 환경 파일의 Classic 읽기 키로
계좌를 확인하고, 공개 데이터에 따른 모의 매집을 수행한다.

```powershell
python -m track_special doctor
python -m track_special --state-directory D:\repos\trading-room-state\track-special-arx\marketdata collect
python -m track_special --config track_special/configs/paper.yaml paper
python -m track_special replay --scenario gap_collapse
python -m track_special status
python -m track_special report
python -m track_special pause-entries
python -m track_special cancel-entry-orders
python -m track_special request-exit
python -m track_special emergency-halt
python -m track_special propose-risk-change --change leverage --before 3 --after 4 --worst-loss UNKNOWN
python -m track_special --config track_special/configs/live.example.yaml validate-live
python -m track_special --state-directory D:\repos\trading-room-state\track-special-arx\adaptive-accumulation accumulate --env-file D:\repos\trading-room\.env --notify-slack
python -m track_special --state-directory D:\repos\trading-room-state\track-special-arx\adaptive-accumulation accumulation-status
python -m track_special --state-directory D:\repos\trading-room-state\track-special-arx\adaptive-accumulation stop-accumulation
```

`validate-live`는 null 설정, 사용자 승인, 인증 계정/포지션, 서버측 보호 및
live adapter 부재를 목록으로 반환하고 종료 코드 2를 낸다. 이는 진단이며
private API 호출이나 설정 변경을 하지 않는다. 현재 계약·마이그레이션 기준은
[architecture_contracts.md](docs/architecture_contracts.md)와
[migration_plan.md](docs/migration_plan.md)다.

적응형 매집의 설정은 `configs/accumulation.paper.json`에 있다. 소량 확보,
눌림에서 매도 압력 완화, 변동성 축소, 박스 상단의 매수 압력에 따라 매수량을
달리한다. 고정 시간마다 사거나 첫 주문에 전액을 넣지 않는다. 조회 실패,
호가·체결 부족, 미완성 봉, 이미 소비한 신호는 추가 매수의 근거로 쓰지 않는다.
상승 전 목표 확보와 수익성은 보장하지 않는다. 현재 모의 체결은 실체결 증거가 아니다.
