# Track C — Coinone / AWS 현물 스캘핑

C의 계약은 [CONCEPT-C.md](../bot/CONCEPT-C.md)다. 기존 `bot.signal.Features`의 감속 신호를 재사용하고, 국내 현물용 스캐너·수량 산식·주문 관리·장부를 분리했다. A/B는 시장가 정리 후 일시정지 상태를 유지한다. C는 A/B의 `STOP`, `PAUSE`, `params.json`을 변경하지 않는다.

## 실제 상태 — 2026-09-05

**C는 AWS에서 실거래 모드로 실행 중이다.** 서버는 서울 `alphaverdict-probe` (`i-0db0329527b7b3533`, Elastic IP `52.78.144.101`), 서비스는 `trading-room-c.service`다. AWS CLI로 사용자 지시에 따라 재기동했고, 기존 S1 매매/알림은 이미 inactive/disabled였음을 확인했다. A/B와 S1을 자동 재개하지 않는다.

17:40:44 KST에 전환했다. 전환 직전 원화 **594,574.7182원**, 기존 코인/미체결 주문 0, 조회한 8종목 maker/taker 요율 0을 확인했다. 이 금액은 그 시점의 관측값이며 고정 운용 한도가 아니다. **최신 사용자 계약은 잔액 전체 복리**다. 이전 30만 원 별도 배정·S1 보존액·자금 확인 대기는 폐기됐다.

현재 release는 `/home/ubuntu/trading-room-c/releases/20260905-173907-dcb134d2e567`이며 Python venv와 데이터는 `/home/ubuntu/trading-room-c`에 분리했다. 서버 설정은 `mode: live`, `funding_confirmed: true`, `capital_mode: account_equity`다. 체크인한 config는 재사용 시 자동 실주문을 피하기 위한 observe 기본값이다. 실제 배포/전환 증거는 `logs/track-c/deployment-latest.json`, `activation-20260905.json`이다.

검증: 로컬 전체 399 tests PASS, 서버 C 40 tests PASS. 17:46 재확인에서 실거래 모드 331초 연속 가동, 8종목 구독·3,887개 메시지, 재시작/WS/API 오류/HALT 0을 확인했다. 실제 원화·내부 현금·운용자본이 모두 594,574.7182원이며 C 주문/체결/재고는 0이다(`logs/track-c/health-latest.json`). 기존 v 신호는 300초 워밍업을 요구하며 조건이 충족돼야 진입한다. 기동 완료와 실제 체결/수익성 검증은 별개다. 계정 수수료는 종목별로 재조회한다. 서버 REST 왕복 53~55ms는 단발 측정으로, 주문 체결 지연을 뜻하지 않는다.

## 전략과 수량

BTC·ETH와 코인원 24시간 거래대금 상위 후보를 합쳐 최대 8종목을 관찰한다. 실제 첫 진입은 v 감속 신호와 최근 10초 매수 체결 우세가 함께 있는 경우다. decay는 필수 조건에서 뺐다. 강화 전 0/0/0과 Hunter B의 1/1/1도 같은 신호에 기록한다. 이 비교는 아직 수익 우위의 증명이 아니다.

한 번에 한 종목을 매수하며 레버리지·숏·추가 매수는 없다. 최우선 매수호가에 post-only로 내고 추격하지 않는다. 기본 미체결 유효기간은 4초, 첫 체결부터 청산 요청까지는 최대 32초다. 반등 정체·전제 무효화·데이터 장애가 발생하면 더 일찍 청산한다. API 지연·미체결 때문에 청산 완료는 32초를 넘을 수 있다. 주문 직전 계정 조회가 끝난 뒤 신호의 유효기간을 다시 확인하고 최신 깊이로 수량을 계산한다.

운용자본 W는 **계좌 원화 잔액 전체 + C 보유수량의 매수호가 평가액**이다. flat과 새 진입 직전에 실제 원화를 동기화하고, 보유 중에는 예약 현금을 포함한 내부 현금에 체결 대금·보유 평가액을 반영한다. 실제 잔액에 이미 반영된 실현손익을 다시 더하지 않는다. 입출금에 따른 차이는 외부 자본 이동으로 기록하며 매매 손익/승률을 바꾸지 않는다. 주문당 원화 상한을 따로 받지 않는다.

```
수량 = 거래소 단위로 내림(
  min(계좌 가용현금 × 95% / 진입가격,
      min(W × 0.25%, 일일 남은 손실예산) / 단위당 손절손실,
      근접 양쪽 호가 잔량의 작은 쪽 × 10%,
      최근 10초 체결량 × 20%,
      거래소 주문 한도)
)
```

손절 거리는 2틱·2스프레드·0.75 ATR1m·3σ1초와 신호 저점의 무효화 거리를 반영한다. 예약 지정가의 체결 여유와 수수료도 위험 산식에 넣는다. 거래당 명목 위험은 현재 W의 0.25%다. 일일 잔여 예산은 `max(0, 현재 W×1.5% + 당일 실현손익 + min(0, 미실현손익))`이다. 전환 잔고 기준으로 거래당 약 1,486원, 당일 손실 전 예산 약 8,919원이며 이후 잔고에 따라 변한다. 현금 5% 여유는 단일 주문의 소진 방지 조건이고 W는 잔액 전체다. 당일 장부는 UTC(한국 시각 오전 9시)에 갱신하며 입금만으로 손실 기록/HALT를 초기화하지 않는다. 최소 주문액을 맞추려고 계산된 수량을 올리지 않는다. 이 값들은 초기 운용 가설이며 보장된 손실 상한이나 최적값이 아니다.

현재 실제 진입은 계정별 maker/taker가 모두 0일 때만 허용한다. 비영 수수료의 통화별 체결 정합은 검증 전이므로 요율이 바뀌면 신규 진입을 거부한다.

## 주문·장부 복구

- `execution.py`: tc- 식별자의 매수 LIMIT/post-only, 매도 MARKET, 매도 STOP_LIMIT만 노출한다. 외부 주문 일괄 취소·출금·이체는 없다.
- `oms.py`: 전송 전에 의도를 저장한다. 응답 유실·중복 ID·조회 실패는 같은 식별자로 조회하고 새 주문을 만들지 않는다. 누적 체결량/대금의 차이만 장부에 적용한다.
- 매수 잔량 취소를 확인한 뒤 C 재고에 거래소 예약 지정가를 건다. 정상 청산은 보호 주문 취소와 마지막 체결량을 조회한 뒤 남은 C 수량만 시장가로 판다. 취소 응답만으로 잔량을 확정하지 않는다.
- `store.py`: SQLite 장부와 이벤트를 함께 커밋하고 별도 배타 잠금으로 중복 실행을 막는다. 재기동하면 저장한 캠페인·주문부터 조회한다. 다른 입출금·S1 손익을 C 수익으로 세지 않는다.
- 최소 주문액 아래 부분체결은 자동 매도/보호가 불가능할 수 있어 재고·원가를 남기고 HALT한다. 매수 취소 확인 전 보호 공백, 예약 지정가 관통·미체결, 서버/API 장애의 위험은 남는다.

## 운영과 배포

Python 3.11 이상과 `websockets==17.1`을 사용한다. C 설정은 `/home/ubuntu/trading-room-c/config.json`, 상태·장부는 독립 `data/status.json`, `data/ledger.sqlite`다. 설정은 시작 시 읽는다. 공개 데이터는 시간별 gzip이며 512MiB 도달 또는 디스크 여유 1GiB 미만이면 새 진입을 막고 기존 재고 관리는 계속한다.

`data/PAUSE`는 새 진입을 중단한다. `data/STOP`이나 SIGTERM은 C 미체결 정리·잔량 청산을 시도한 뒤 종료한다. systemd 종료 여유는 120초다. 거래소/네트워크 장애에서 flat 완료를 보장하지 않는다. STOP 파일은 명시적으로 해제하기 전까지 남는다.

```powershell
# 로컬: 주문 없는 검증
python -m unittest track_c.test_coinone track_c.test_runtime bot.test_lifecycle
python -m track_c.deploy verify

# 서버가 실행 중이고 정지 의도가 해소된 뒤: 관측 전용 배포
python -m track_c.deploy install-observe
```

`install-observe`는 AWS CLI로 지정 서버 확인, 코드 전송·해시 검증·원격 테스트 후 관측 서비스만 기동한다. **현재 live 설정이나 미해결 노출은 이 명령으로 덮어쓰지 않는다.** 다음 실거래 코드 교체는 stage 후 새 flat 창을 확인하고 C를 정상 종료한 뒤, 기존 live 설정/장부를 보존하여 unit의 release 경로를 바꿔 재시작한다. EC2 시작·S1 수정 명령은 도구에 포함하지 않는다. 새 START, WS 구독/워밍업, 계정·장부 정합과 재시작 횟수를 확인한다.

실거래 전환은 완료했다. 다음 평가는 실제 체결의 비용 포함 순손익, 체결/취소율, 1·5·10·30초 후 가격, 최대 역행과 손실 꼬리다. 장기 페이퍼 대기 규칙을 새 승인 절차로 추가하지 않는다. 키·서버·잔액 전체 복리·알고리즘 주문 수량은 확정된 지시다.

## 키와 연결 점검

로컬 `D:/repos/arbitrage/.env`의 `coinone-api-key-aws` / `coinone-secret-key-aws`가 서버에 이미 있던 `/home/ubuntu/arbitrage/.env`의 기본 별칭과 일치함을 값 출력 없이 확인했다. 키 파일은 읽기만 했고 전송하지 않았다. 코드 패키지에는 `.env`, PEM, Bitget 키, Slack webhook, 기존 계좌 장부를 넣지 않는다. 인증 리다이렉트와 원본 HTTP 오류 출력을 차단한다.

`preflight`는 주문 기능이 없는 별도 조회 명령이다. 가상 측정 금액은 실제 주문 한도가 아니다. 비용은 같은 호가에서 즉시 왕복할 때의 깊이·수수료 추정이며 대기열·역선택·가격 이동을 제외한다. ACCOUNT_READABLE은 실행 준비 완료를 뜻하지 않는다.

```bash
# AWS 서버에서 키 파일을 읽기만 하는 계정 점검
python -m track_c.preflight --env /home/ubuntu/arbitrage/.env --symbols BTC ETH SOL --order-krw 300000 --output /home/ubuntu/trading-room-c/data/account-check-new.json
```

## C 전용 Slack 알림 — 2026-09-05 18:03 KST 연결 완료

AWS `trading-room-c-notify.service`가 매매 엔진과 독립 실행한다. `bot.notify`에서 `track="C"`를 명시하면 C SQLite 장부와 status만 읽어 원화 계기판을 붙인다. C 데이터가 없으면 확인 불가로 표시한다. 기존 A/B의 USDT 잔고·체결·승률을 대신 사용하지 않는다.

계기판은 이벤트 상세 아래에 **잔고 | 가용 | 누적 손익 | 엔진 수익률 | 캠페인 횟수 | 승률** 순서로 붙는다. 당일은 KST다. 누적 손익은 C `FILL.pnl`의 수수료 포함 합계이며 `CLOSE.net`을 중복 합산하지 않는다. 첫 매수 부분체결에서 캠페인 한 번을 세고, 당일 시작해 종료된 캠페인만 승률 분모에 넣는다. 원화 잔액 전체와 C 보유 평가액이 복리 운용자본이며 입출금·잔액 증감은 매매 손익이 아니다. 엔진 자체의 일일 위험 제한은 기존 UTC 경계를 유지한다.

진입·부분청산·전량청산·손절·HALT·주문 불확실/거절·API 이상·WS 장애/복구·실제 종목 변경·외부 자본 변동 및 60분 현황을 전송한다. 조용한 개별 종목의 호가 지연을 전체 WS 장애로 오인하지 않는다. 최초 시작은 장부 끝에서 과거 문맥만 복원하고, 이후에는 저장한 처리 위치에서 이어간다. 같은 주문의 미발송 부분체결을 합치고, 같은 CLOSE의 중복 및 이중 worker를 막는다. 미발송 건은 재시도한다. **Slack이 이미 받았으나 응답이나 성공 저장이 유실된 경우 재시도 알림이 중복될 수 있다.** 주문 재제출과는 관계없는 알림 전송의 한계다.

- 코드 release: `/home/ubuntu/trading-room-c/notifications/releases/20260905-180301-458f7eeab3ca`.
- 별도 알림 상태: `/home/ubuntu/trading-room-c/data/notifications/status.json` 및 `relay.sqlite`. 거래 장부는 SQLite `mode=ro`로 열고, systemd 쓰기 권한은 `data/notifications`로 제한한다.
- 기존 서버 `.env`의 동일 Slack webhook을 읽는다. 비밀값을 복사하거나 배포 패키지에 넣지 않는다.
- 정상 기준: 서비스 active/enabled, 상태 보고 2분 이내, `last_failure`/`source_error` 없음, 전송 후 `last_sent.http_status=200`, `ack=ok`. 체결이 없으면 다음 정기 전송은 60분 뒤이므로 오래된 `last_sent`만으로 장애라고 판단하지 않는다.
- 실패는 위 상태와 `journalctl -u trading-room-c-notify.service`에서 확인한다. 새로운 감독 세션은 worker를 중복 실행하지 않는다. 알림을 복구할 때 C 매매 서비스를 재시작하지 않는다.

```powershell
# 로컬에서 AWS CLI 대상 확인 + 원격 알림/매매 상태 조회 (주문 없음)
python -m track_c.deploy_notify status

# 알림 코드 수정 때만 별도 패키지·서버 테스트·서비스 배포
python -m track_c.deploy_notify install
```

검증: 로컬 전체 **419 tests PASS**, 서버 알림 **24 tests PASS**. 18:03 KST 첫 원화 계기판 전송에 **Slack HTTP200 / ok**, notifier 재시작 후 처리 위치 복원과 과거 재전송 **0**을 확인했다. C 매매 PID1621·재시작0을 유지했다. 당시 잔고/가용 594,574.72원, 누적손익0원, 캠페인0, 포지션/미체결0이었다. 원격 상태 증거는 `logs/track-c/notification-health-latest.json`, 재시작 검증은 `notification-restart-verification.json`이다. 실제 체결 카드의 실전 표본은 아직 없으며 합성 장부 테스트로 검증했다.

## 공식 근거

- [인증/IP 등록](https://docs.coinone.co.kr/docs/about-public-api), [개별 수수료](https://docs.coinone.co.kr/reference/find-trade-fee-by-pair).
- [주문](https://docs.coinone.co.kr/reference/place-order), [주문 조회](https://docs.coinone.co.kr/reference/order-detail), [취소](https://docs.coinone.co.kr/reference/cancel-order).
- [호가 WS](https://docs.coinone.co.kr/reference/public-websocket-orderbook), [체결 WS](https://docs.coinone.co.kr/reference/public-websocket-trade).
