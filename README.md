# Trading Room

A, A-2, B, C 매매 전략의 시장 관측, 주문 실행, 재생과 검증을 관리하는 Python 프로젝트다.

| 트랙 | 역할 |
|---|---|
| [A — 순환매](track_a/README.md) | Bitget 바구니 선정과 감속·정체 기반 순환매 |
| [A-2 — 코인원 순환매](track_a_2/README.md) | Coinone 현물 롱온리 순환매 실거래 실행기(기본 정지) |
| [B — 캠페인](track_b/README.md) | Bitget 종목 탐색과 독립 캠페인, 자본 풀 관리 |
| [C — 유동성 회복](track_c/README.md) | Coinone 시장 상태·주문 조건별 회수 가치 추정과 실체결 관측 |

`common/`은 A/B 공유 실행·위험 관리·거래소 연결과 알림, `tests/`는 트랙별 회귀 테스트, `config/`는 예시 설정을 담는다. C의 연구 원문은 [track_c/research.md](track_c/research.md)에 있다.

Python 3.12 이상을 사용한다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -s tests -t .
python -m track_a_2.live --check
python -m track_a_2.status
python -m track_a_2.observe --coin BTC --coin ETH --seconds 3600
python -m track_a_2.replay --help
```

차트 도구는 `python -m pip install -e ".[charts]"`로 선택 설치한다. 재생·검증 도구의 입력은 다음 명령으로 확인한다.

```powershell
python -m track_c.replay.engine --help
python -m track_c.replay.evidence --help
python -m track_c.ops.live_evidence --help
```

공개 설정의 기본값은 **A/B 정지·dry, C observe**다. `config/tracks.json`과 각 트랙의 설정은 배포 예시이며 실제 계좌 상태를 나타내지 않는다. 키·서버 주소·장부·녹화·학습 모델·운영 평가 등록과 비공개 Git 이력은 포함하지 않는다. 라이브 실행에는 별도의 자격 증명, 계정·네트워크 확인, 소스와 일치하는 고정 모델 및 명시적인 운영 설정이 필요하다.

로그·녹화·모델·실험 출력은 기본적으로 레포의 형제 디렉터리 `trading-room-state/`에 둔다. A/B와 로컬 도구의 경로는 `TRADING_ROOM_HOME`으로 바꾸며, C는 설정의 `data_directory`와 `c4_model_path`를 사용한다. 일회성 스크립트는 OS 임시 디렉터리에서 실행하고 반복 도구와 회귀 테스트만 소스에 남긴다.

단위 테스트는 합성 입력과 모의 거래소를 사용한다. 실제 체결확률·지연의 현실 보정, 하루 거래횟수의 충분성, 수익성 개선은 별도 자료와 미사용 평가 구간으로 확인해야 한다. 자동 재학습·증액·실거래 전환은 제공하지 않는다.
