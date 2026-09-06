# Trading Room

현재 **A/B는 정지**, **A-2는 실거래 실행기가 구현됐지만 활성화 전 정지**, **C는 BTC 최소수량 탐색 규칙으로 실체결 수집 중**이다. 운영 상태의 기준은 [config/tracks.json](config/tracks.json)이며 실제 AWS 상태는 아래 명령으로 확인한다.

| 경로 | 역할 |
|---|---|
| [track_a](track_a/README.md) | Bitget 순환매: 바구니 선정, 재생, 분석 |
| [track_a_2](track_a_2/README.md) | Coinone 현물 롱온리 순환매: 독립 포트폴리오 실거래 실행기(기본 정지) |
| [track_b](track_b/README.md) | Bitget 작전코인: 종목 탐색, 캠페인, 검증 |
| [track_c](track_c/README.md) | Coinone 유동성 회복: 시장 상태, 학습, 재생, 실체결 |
| `common/` | A/B 공유 주문 엔진·리스크·거래소 연결, 공통 알림 |
| `tests/` | 트랙별 회귀 테스트와 고정 테스트 입력 |
| `config/` | 운영 트랙 상태와 A/B 공유 계정 설정 |

```powershell
python -m unittest discover -s tests -t .
python -m track_c.ops.status
python -m track_a_2.live --check
python -m track_a_2.status
```

Python 3.12 이상을 사용한다. 의존성은 `pyproject.toml`에 정의하며 `python -m pip install -e .`로 설치한다. 차트 도구가 필요할 때만 `python -m pip install -e ".[charts]"`를 사용한다.

로그·녹화·모델·실험 출력은 기본적으로 **레포의 형제 디렉터리 `trading-room-state/`**에 둔다. A/B와 로컬 도구의 기본 경로는 `TRADING_ROOM_HOME`으로 바꾸며, C 실행기는 설정의 `data_directory`와 `c4_model_path`를 사용한다. 일회성 조사 스크립트는 OS 임시 디렉터리에서 실행하고, 반복 사용하는 도구만 해당 트랙에 넣는다. 테스트는 `tests/`에서 임시 디렉터리를 사용한다. 날짜별 보고서와 실행 영수증을 소스 트리에 추가하지 않는다.

기존 `data/`, `logs/`는 내용 그대로 `../trading-room-state/`로 이동했다. 정리 전 소스·문서·설정과 미커밋 변경은 `../trading-room-archive/20260906-pre-cleanup/`에 해시 목록과 함께 보존했다. 폐기된 `quant/`, C1/C2/C3 실행기, 옛 배포·수리 스크립트는 이 보존본과 Git 기록에서 확인한다.

현재 AWS C는 `c4-live-v2.4` 고정 릴리스에서 `execution_sampling`으로 실행한다. 1틱 이상에서 참조가격 하한보다 싼 가장 가까운 최소수량 후보만 주문하며 모델값은 기록에만 쓴다. 주문 직전 최신 시장으로 같은 가격·수량·손절이 여전히 안전한지 다시 계산한다. 이전 C4 live와 원래 shadow의 모델·장부·평가 창은 별도로 보존한다. `.env`는 비공개 입력이고 루트 `STOP`·`PAUSE`는 A/B의 기존 정지 제어다. 정리나 테스트 때문에 이를 해제하지 않는다.
