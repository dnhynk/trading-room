# Trading Room

> A Python system for observing, replaying, and executing multiple crypto trading strategies across exchanges, with every track paused or observe-only by default.

Bitget·Coinone에서 성격이 다른 매매 전략 여러 개(트랙)를 한 레포에서 관측·재생·검증·실행하는 Python 프로젝트다. 전략 아이디어보다 **주문이 잘못 나가지 않게 하는 실행 경계와, 재생 결과를 실체결 근거로 오해하지 않게 하는 검증 경계**에 무게를 둔다.

## 해결하려는 문제

자동매매 코드에서 손실은 전략 판단보다 실행과 검증의 빈틈에서 자주 생긴다. 이 레포는 다음 문제를 코드로 다룬다.

- 응답을 잃은 주문을 다시 보내 중복 체결이 생기는 문제
- 계산하는 동안 시장이 바뀌었는데 오래된 결정으로 주문하는 문제
- 설정·키·네트워크가 준비되지 않은 상태에서 실거래가 시작되는 문제
- 소스나 입력 데이터가 바뀌었는데 이전 모델·평가 결과를 그대로 재사용하는 문제
- 공개 호가로 만든 재생 결과를 실제 체결 성과처럼 해석하는 문제

## 구조

```text
config/tracks.json ── 트랙별 운영 상태 (paused / observe / execution·live 비활성)
│
├─ track_a/, track_b/ ─┐  Bitget 선물: A 순환매, B 캠페인
├─ common/ ────────────┘  A/B 공유 실행 엔진·위험 관리·거래소 연결·알림
├─ track_a_2/             Coinone 현물 롱온리 실행기 + 공개 시세 녹화·재생
│    └─ 녹화 세션 ──▶ track_c_multivenue/   Coinone·Upbit·Bithumb 오프라인 비교 연구
├─ track_c/               Coinone BTC 유동성 회복 가치 추정·관측·재생·검증
├─ track_special/         Bitget ARXUSDT 무기한 선물 long-only 연구 (실거래 경로 없음)
└─ tests/                 트랙별 회귀 테스트 (합성 입력·모의 거래소)
```

| 트랙 | 역할 |
|---|---|
| [A — 순환매](track_a/README.md) | Bitget 바구니 선정과 감속·정체 기반 순환매 |
| [A-2 — 코인원 순환매](track_a_2/README.md) | Coinone 현물 롱온리 순환매 실거래 실행기(기본 정지) |
| [B — 캠페인](track_b/README.md) | Bitget 종목 탐색과 독립 캠페인, 자본 풀 관리 |
| [C — 유동성 회복](track_c/README.md) | Coinone 시장 상태·주문 조건별 회수 가치 추정과 실체결 관측 |
| [C 다종목 연구](track_c_multivenue/README.md) | A-2 녹화 세션을 C의 조건으로 비교하는 오프라인 연구(주문 기능 없음) |
| [Special — ARX 선물 캠페인](track_special/README.md) | Bitget ARXUSDT 선형 무기한 long-only observe/paper/replay 연구(실거래 차단) |

## 설계 포인트

- **실거래는 여러 잠금을 모두 풀어야 시작된다.** `python -m track_a_2.live`는 트랙 상태·실행 플래그 등 설정 게이트를 자격 증명을 읽기 전에 검사하고, 송신 IP를 확인한 뒤 다시 검사한다. Special 트랙의 `LiveTransport`는 서명·HTTP 쓰기 경로가 없는 의도적 차단 구현이며, `validate-live`는 남은 차단 사유를 나열하고 종료 코드 2를 반환한다.
- **주문 의도를 먼저 기록하고, 식별자로 멱등성을 지킨다.** A-2는 `ta2-` 접두 클라이언트 주문 ID를 만들어 `ORDER_INTENT`를 SQLite(`synchronous=FULL`)에 저장한 뒤에 거래소로 전송한다. 응답을 잃은 주문은 같은 식별자로 조회하고 다시 주문하지 않는다.
- **전송 직전에 결정을 다시 검증한다.** C는 후보 계산 후와 주문 전송 직전에 결정 나이·호가 시각·계좌·소유권을 확인한다. 시한을 넘긴 결정은 `transmitted=false`로 폐기해 거래소 거부와 구분한다.
- **송신 IP를 확인한다.** C와 A-2는 실행 전에 외부에서 본 송신 IP가 설정의 `expected_egress_ip`와 같은지 확인하고, 다르면 시작하지 않는다.
- **모델·평가를 소스 해시에 묶는다.** C의 재생·평가 산출물은 `track_c/`·`common/` 소스와 입력 파일의 SHA-256을 식별자로 기록하고, 소스나 입력이 달라지면 기존 결과를 거부한다. 다종목 연구도 원본 세션 파일의 SHA-256을 검증한 뒤 읽는다.
- **미확정 주문까지 위험에 포함한다.** Special 트랙의 위험 엔진은 기존 포지션과 전송 중·미체결 예약을 합산해 손실·명목금액·청산 완충 한도를 검사한다.
- **재생과 실체결을 구분한다.** 재생의 지연·체결 조건은 고정 시나리오 가정으로 명시하고, 관측 중단·부분체결·잔량은 검열 표본으로 남긴다.

## 기술 스택

- Python 3.12 이상, `asyncio`
- 외부 런타임 의존성: `websockets`, `python-dotenv` (HTTP는 표준 라이브러리 `urllib`)
- 상태·장부 저장: SQLite
- 테스트: `unittest` / 정적 검사: `mypy --strict` (`track_special/arx_campaign` 범위)
- 선택: `matplotlib` (차트 도구)
- 거래소: Bitget(USDT 선물), Coinone(KRW 현물), 비교 수집용 Upbit·Bithumb 공개 시세

## 현재 상태

공개 설정 기준으로 실거래 중인 트랙은 없다.

| 트랙 | 공개 설정 상태 |
|---|---|
| A, B | `paused`, dry |
| A-2 | `paused`, `execution_enabled=false`, 종목 없음. 실행기는 구현돼 있으나 독립 알파는 동결 |
| C | `observe`. 예측·수익성 개선은 입증되지 않음 |
| C 다종목 연구 | 오프라인 연구 전용, `orders_enabled=false` |
| Special ARX | `observe`, `live_enabled=false`. 모의 매집·재생만 가능, 실거래 어댑터 없음 |

단위 테스트는 합성 입력과 모의 거래소를 사용한다. 실제 체결확률·지연의 현실 보정, 하루 거래횟수의 충분성, 수익성 개선은 이 레포의 테스트로 입증되지 않으며 별도 자료와 미사용 평가 구간으로 확인해야 한다. 자동 재학습·증액·실거래 전환 기능은 없다.

## 실행 방법

Python 3.12 이상을 사용한다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -s tests -t .
```

자격 증명 없이 실행할 수 있는 확인·연구 명령:

```powershell
python -m track_a_2.live --check        # 설정·잠금 상태 점검 (네트워크·자격 증명 미사용)
python -m track_a_2.status
python -m track_a_2.observe --coin BTC --coin ETH --seconds 3600
python -m track_a_2.replay --help
python -m track_special --help
python -m track_special validate-live   # 실거래 차단 사유 목록, 종료 코드 2
python -m track_c.replay.engine --help
python -m track_c.replay.evidence --help
python -m track_c.ops.live_evidence --help
python -m track_c_multivenue --help
```

차트 도구는 `python -m pip install -e ".[charts]"`로 선택 설치한다.

## 개발자 참고

- 트랙별 전략 계약·설정·명령은 각 트랙 README에 있다. C의 연구 원문은 [track_c/research.md](track_c/research.md), Special 트랙의 설계 문서는 [track_special/docs/](track_special/docs/)에 있다.
- 작업 규약은 [AGENTS.md](AGENTS.md)에 있다.
- `config/tracks.json`과 각 트랙의 설정은 배포 예시이며 실제 계좌 상태를 나타내지 않는다. 키·서버 주소·장부·녹화·학습 모델·운영 평가 등록과 비공개 Git 이력은 포함하지 않는다. 라이브 실행에는 별도의 자격 증명, 계정·네트워크 확인, 소스와 일치하는 고정 모델 및 명시적인 운영 설정이 필요하다.
- 로그·녹화·모델·실험 출력은 기본적으로 레포의 형제 디렉터리 `trading-room-state/`에 둔다. A/B와 로컬 도구의 경로는 `TRADING_ROOM_HOME`으로 바꾸며, C는 설정의 `data_directory`와 `c4_model_path`를 사용한다.
