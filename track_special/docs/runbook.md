# ARX 운영 절차

기본은 `observe`이며 `python -m track_special doctor`로 설정과 무쓰기 경계를 확인한다. `collect`/`observe`는 unsigned public GET과 외부-state append만 수행한다. `paper`/`replay`는 거래소 쓰기를 하지 않으며 합성 결과에는 수익성 증거가 아니라는 꼬리표가 붙는다.

`pause-entries`는 새 진입만 막고, `cancel-entry-orders`는 진입 주문에 취소 요청을 남기며, `request-exit`은 EXIT_ONLY로 전환한다. `validate-live`는 개발 빌드에서 항상 비운영으로 보고하며 계정/마진/포지션/레버리지 변경을 하지 않는다.

보호 공백, DB 오류, 통신 결과 불명은 신규 진입을 중지한다. ACK는 체결이나 보호 확인이 아니며, timeout 뒤에는 동일 clientOid를 먼저 대사한다. `RESULT_UNKNOWN`은 clientOid의 거래소 대사에서 terminal 상태 또는 제한 시간이 기록된 not-found가 확인되기 전에는 절대 재전송하지 않는다.

프로세스 시작마다 `startup_reconciled=false`다. REST 스냅샷으로 ARX 포지션,
일반/전략 주문, 보호 수량, 외부 주문·포지션 부재를 확인하고 이후 증분과
합친 뒤에만 진입 예약을 열 수 있다. 결과 불명 중 취소 요구는 상태를
`cancel_pending`으로 덮지 않고 대사 대기로 남긴다.

상태 DB는 `TRADING_ROOM_HOME` 또는 레포 형제 `trading-room-state`의 절대 경로에만 둔다. 프로세스 잠금은 DB보다 먼저 획득하며, 남아 있는 잠금은 stale로 추정해 삭제하지 않고 운영자가 소유자를 대사한다. 보호 완료는 주문 ID, 거래소 active 조회, trigger 기준, 커버 수량, 유효 시각이 모두 있는 경우에만 인정한다.

감축은 신규·증액보다 항상 우선한다. `pause-entries`(신규만 중지), `request-exit`(EXIT_ONLY), `emergency-halt`(긴급 중지), `cancel-entry-orders`(기존 진입 취소 대기)는 서로 다른 제어이며, 취소 ACK만으로 포지션/보호 대사가 끝난 것은 아니다. `propose-risk-change`는 해시·최악손실·지연·승인대기만 외부 상태에 기록하며 한도나 실거래를 바꾸지 않는다.
