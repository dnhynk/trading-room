# ARX 검증 보고서

이 구현은 합성 테스트에서 의도·예약·ACK/체결 분리, deterministic clientOid, timeout 후 대사 전 재전송 차단, partial/cancel race, 단조 상태 전이, 보호 공백과 제어 상태를 검증한다. 보호는 order ID·active 조회·trigger 기준·covered quantity·deadline을 모두 받아야 active로 기록된다. replay는 timestamp·mark/last·명시 funding settlement·수수료·스프레드·지연·depth 기반 부분/미체결·미보호 노출과 동일 봉의 불리한 순서를 보수적으로 반영한다.

제한: 공개/합성 replay는 실체결 검증 또는 손익 하한이 아니며, cash/spot/no-pyramid/pyramid 비교도 수익성 주장에 쓰지 않는다. live validation은 fail-closed이며 어떤 private write나 endpoint 호출도 하지 않는다; UTA v3 order serializer는 shape 테스트 전용이다.
