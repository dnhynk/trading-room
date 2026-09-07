# Track C 다종목 연구 편입부

이 패키지는 A-2가 녹화한 Coinone·Upbit·Bithumb 원본 세션을 C의 시장 상태와 공개 큐 재생으로 비교하는 **오프라인 연구 전용 경계**다. 인증·주문 클라이언트와 실거래 모드는 없다. 기존 `track_c/` 밖에 둔 이유는 C-BTC 고정 모델이 `track_c` 아래의 모든 Python 소스를 해시하기 때문이다.

원본 gzip과 manifest를 덮어쓰지 않는다. 각 파일의 SHA-256을 검증한 뒤 한 수집 프로세스의 `sequence` 순서로 읽는다. `received_ms`나 거래소별 우선순위로 재정렬하지 않는다. 시간별 세션은 각각 모든 거래소가 재연결된 구간이므로 경계에서 열린 반사실 주문을 검열하고 시장·참조 상태를 초기화한다. 세션 경계를 연속 시장이나 독립 표본으로 가장하지 않는다.

비교 정책은 고정되어 있다.

| 정책 | 사전에 알려진 진입 전제 |
|---|---|
| P0 | C 공통 관측·주문관리 조건 안에서 새 A-2 `DIP_SLOWING` |
| P1 | P0 + C 외부 할인 적격성 |
| P2 | 새 C 현지 매도 에피소드 + 외부 할인 적격성 |
| P3 | P2의 같은 결정 시점까지 A-2 감속이 관측되어 TTL 안에 있음 |

P0는 A-2의 감속 **진입 전제**를 동결한 비교군이지, 외부정보를 전혀 쓰지 않는 과거 A-2 전체 전략이 아니다. P0도 공통 주문 생성과 열린 주문 관리에서 C의 외부 참조가격 준비·하한을 사용한다. 네 정책은 그 공통 관측 가능 집단에서 진입 정보의 차이만 보도록 동일한 `0:minimum` 후보, 수수료·지연·깊이·청산 가정을 사용한다. 모든 트리거의 합집합과 각 정책의 비선택 사유도 남긴다.

외부 기준가격 불확실군을 공통 주문 게이트에서 조용히 버리지 않도록 별도 `local_minimum_passive_fill_markout` 진단을 기록한다. 이는 외부 안전 게이트를 푼 정책이 아니라, 외부 기준 상태별로 로컬 최소수량 지정가의 모의 체결과 1·5·30·120초 실행가능 마크아웃을 비교하는 비주문 계측이다.

세션 경계 결과는 완결 현금손익, 신선 호가로 평가한 잔여 재고, 평가불능 잔여 재고, 잔여 재고를 0원으로 보는 현금회수 스트레스로 분리한다. 검열률과 검열 잔여원금 비중을 함께 표시하며 어느 한 평균으로 정책 우승자를 정하지 않는다. 예정된 1시간 파일 종료 전에는 최악의 고정 실행 경로만큼 공통 진입을 막지만, 예상보다 일찍 끝난 세션의 검열은 그대로 보존한다.

결정은 `received_ms` 버킷 시작이 아니라 버킷 종료 시각 `(decision_bucket_ms + 1)ms`에 발생한다. 마지막 포함 수신시각이 결정 ns보다 이른지를 검사하고 주문 지연도 이 유효 결정시각부터 계산한다.

개별 후보는 서로 겹치는 반사실 주문이므로 손익 합계를 계좌 수익이나 회전율로 해석하면 안 된다. 결과는 손실이어도 정상 저장되며 항상 `RESEARCH_ONLY`, `orders_enabled=false`다. `COMPLETE`는 계산 완료일 뿐 알파 승인이나 미래 사전등록을 뜻하지 않는다.

```powershell
python -m track_c_multivenue --session D:\path\to\finalized-session
python -m track_c_multivenue --session D:\path\to\session-1 --session D:\path\to\session-2 --output D:\repos\trading-room-state\track-c-multivenue\study-001
python -m track_c_multivenue --register-output D:\repos\trading-room-state\track-c-multivenue\registrations\future-001.json --start-ms 1789000000000 --end-ms 1789600000000
python -m track_c_multivenue --session D:\path\to\future-session --output D:\repos\trading-room-state\track-c-multivenue\future-001 --registration D:\repos\trading-room-state\track-c-multivenue\registrations\future-001.json
```

평가 실행 직전 영수증은 실행 입력의 동결만 증명한다. `REGISTERED_BEFORE_WINDOW`는 소스·계약·기간·주 지표·검열·제외 규칙을 시작시각 전에 별도 파일로 만들고, 그 기간 안에 완전히 들어온 세션만 허용한다. 이 로컬 파일은 외부 타임스탬프 공증이나 입력 세션 누락 방지까지 증명하지 않으므로 그 한계도 보고서에 남긴다.

출력은 레포 밖의 `TRADING_ROOM_HOME` 또는 형제 `trading-room-state/`에만 만든다. 과거 Coinone 단독 테이프에 외부가격을 사후 보간하지 않으며, 기존 C-BTC 모델·평가·장부와 결과 식별자를 공유하지 않는다.
