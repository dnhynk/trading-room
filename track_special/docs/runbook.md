# ARX 운영 절차

기본은 `observe`이며 `python -m track_special doctor`로 설정과 무쓰기 경계를 확인한다. `observe`, `paper`, `replay`는 거래소 쓰기를 하지 않는다.

`pause-entries`는 새 진입만 막고, `cancel-entry-orders`는 진입 주문에 취소 요청을 남기며, `request-exit`은 EXIT_ONLY로 전환한다. `validate-live`는 개발 빌드에서 항상 비운영으로 보고하며 계정/마진/포지션/레버리지 변경을 하지 않는다.

보호 공백, DB 오류, 통신 결과 불명은 신규 진입을 중지한다. ACK는 체결이나 보호 확인이 아니며, timeout 뒤에는 동일 clientOid를 먼저 대사한다.
