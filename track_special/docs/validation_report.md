# ARX 검증 보고서

이 구현은 합성 테스트에서 의도·예약·ACK/체결 분리, timeout 대사, partial/cancel race, 보호 공백과 제어 상태를 검증한다. replay는 수수료·펀딩·스프레드·지연·부분/미체결과 동일 봉의 불리한 순서를 보수적으로 반영한다.

공개 marketdata 수집기는 unsigned GET만 사용하며 futures/spot instrument,
ticker/book, funding/history, tier, OI, fills, completed-candle 판정,
liquidation을 별도 외부-state stream으로 보존한다. endpoint 하나가 불가해도
receipt의 error/gap으로 남고 성공 stream은 삭제되지 않는다.

제한: 공개/합성 replay는 실체결 검증 또는 손익 하한이 아니며, 실운영 live
adapter는 아직 없다. live validation은 fail-closed이며 어떤 private write도
호출하지 않는다.
