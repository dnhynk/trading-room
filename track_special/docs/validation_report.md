# ARX 검증 보고서

이 구현은 합성 테스트에서 의도·예약·ACK/체결 분리, timeout 대사, partial/cancel race, 보호 공백과 제어 상태를 검증한다. replay는 수수료·펀딩·스프레드·지연·부분/미체결과 동일 봉의 불리한 순서를 보수적으로 반영한다.

제한: 공개/합성 replay는 실체결 검증 또는 손익 하한이 아니며, marketdata 수집 모듈과 실운영 live adapter는 아직 없다. live validation은 fail-closed이며 어떤 private write도 호출하지 않는다.
