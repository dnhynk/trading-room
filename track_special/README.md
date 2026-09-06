# Special Track — ARX Futures Campaign

ARX의 상승 국면에만 참여하는 독립 USDT 선형 무기한 선물 long-only 연구 트랙이다. 작은 `PROBE`로 시작해 새 확인이 생길 때만 `BUILD`하고, 일부 이익을 확정한 뒤 잔여 추세를 추종한다. 하락·무신호 구간의 정상 상태는 무포지션이다.

이 트랙은 기존 Track B를 이름만 바꾼 구현이 아니다. B의 포지션·주문·장부·자본 풀을 가져오지 않고, Track C의 Coinone 원화 장부나 운영 프로세스와도 연결하지 않는다. 현재 운영 계약은 `observe`, `live_enabled=false`이며 개발 세션에서 실주문, 계정 모드 변경, 레버리지 변경, 이체를 수행하지 않는다.

핵심 경계는 다음과 같다.

- 데이터: ARX 현물과 `ARXUSDT` 선물을 별도 series로 저장하고 exchange/receive time, 순서, 중복, 결측을 보존한다.
- 전략: `WATCH → PROBE → BUILD → RIDE → HARVEST → FLAT → COOLDOWN`은 주문을 만들지 않고 의도만 만든다.
- 위험: 기존 포지션과 전송 중·미체결·결과 미확정 예약을 한 트랜잭션에서 합산한다. 원금 손실, 이익 반납, gross stop risk, 명목금액, 격리증거금, 청산 완충을 독립 제한한다.
- 실행: risk gate의 짧은 수명 승인을 발송 직전에 다시 검증한다. ACK, 체결, 보호 확인을 서로 다른 상태로 관리한다.
- 원장: 증거금 반환, 청산 명목금액, 미실현이익을 실현이익으로 기록하지 않는다. 실현 수수료·펀딩의 중복 차감을 금지한다.

독립 실행 경로와 승격 관문은 구현 완료 후 이 문서에 갱신한다. 현재 계약·마이그레이션 기준은 [architecture_contracts.md](docs/architecture_contracts.md)와 [migration_plan.md](docs/migration_plan.md)다.

