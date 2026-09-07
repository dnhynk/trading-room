# Track C 다종목 연구 편입부

이 패키지는 A-2가 녹화한 Coinone·Upbit·Bithumb 원본 세션을 C의 시장 상태와 공개 큐 재생으로 비교하는 **오프라인 연구 전용 경계**다. 인증·주문 클라이언트와 실거래 모드는 없다. 기존 `track_c/` 밖에 둔 이유는 C-BTC 고정 모델이 `track_c` 아래의 모든 Python 소스를 해시하기 때문이다.

원본 gzip과 manifest를 덮어쓰지 않는다. 각 파일의 SHA-256을 검증한 뒤 한 수집 프로세스의 `sequence` 순서로 읽는다. `received_ms`나 거래소별 우선순위로 재정렬하지 않는다. 시간별 세션은 각각 모든 거래소가 재연결된 구간이므로 경계에서 열린 반사실 주문을 검열하고 시장·참조 상태를 초기화한다. 세션 경계를 연속 시장이나 독립 표본으로 가장하지 않는다.

비교 정책은 고정되어 있다.

| 정책 | 사전에 알려진 진입 전제 |
|---|---|
| P0 | 새 A-2 `DIP_SLOWING` |
| P1 | P0 + C 외부 할인 적격성 |
| P2 | 새 C 현지 매도 에피소드 + 외부 할인 적격성 |
| P3 | P2의 같은 결정 시점까지 A-2 감속이 관측되어 TTL 안에 있음 |

P0는 A-2의 감속 **진입 전제**를 동결한 비교군이지, 과거 A-2의 사다리·수량·청산을 다시 실행하는 정책이 아니다. 네 정책은 진입 정보의 차이만 보도록 동일한 `0:minimum` 후보, C의 동일한 수수료·지연·깊이·청산 가정을 사용한다. 모든 트리거의 합집합과 각 정책의 비선택 사유도 남긴다. 개별 후보는 서로 겹치는 반사실 주문이므로 손익 합계를 계좌 수익이나 회전율로 해석하면 안 된다. 결과는 손실이어도 정상 저장되며 항상 `RESEARCH_ONLY`, `orders_enabled=false`다.

```powershell
python -m track_c_multivenue --session D:\path\to\finalized-session
python -m track_c_multivenue --session D:\path\to\session-1 --session D:\path\to\session-2 --output D:\repos\trading-room-state\track-c-multivenue\study-001
```

출력은 레포 밖의 `TRADING_ROOM_HOME` 또는 형제 `trading-room-state/`에만 만든다. 과거 Coinone 단독 테이프에 외부가격을 사후 보간하지 않으며, 기존 C-BTC 모델·평가·장부와 결과 식별자를 공유하지 않는다.
