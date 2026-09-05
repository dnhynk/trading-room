"""Human-readable, self-contained results, including the reasons to withhold."""
import json
from pathlib import Path

from .validation import summarize


def number(value):
    return "미확정" if value is None else f"{value:+.6f}"


def write_report(report, config, directory):
    path = Path(directory)
    with (path / "results.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    rows = ["# Quant pipeline 결과", "", f"판정: **{report['gate']['status']}**. 실거래 전환: 없음.", "",
            f"실험: `{report['trial_id']}`", f"설정: `{config.id}`", "",
            "동일한 녹화·신호·기준 정책·초기 자본에서 비용과 지연만 바꾼 비교다. 기존 live 성적이나 새 알파의 증명이 아니다.", "",
            "| 항목 | 기준 | 비용 스트레스 |", "| --- | ---: | ---: |"]
    runs = [report["runs"][k] for k in ("reference", "cost_stress")]
    summaries = [summarize(run, config.data["validation"]) for run in runs]
    for name, values in (
        ("초기 자본 USDT", [r["initial_equity"] for r in runs]),
        ("수수료·관측 펀딩 포함 실현", [r["realized_net"] for r in runs]),
        ("잔여 포지션 청산 평가 포함", [r["liquidated_estimate_net"] for r in runs]),
        ("최대 낙폭 USDT", [r["max_drawdown"] for r in runs]),
        ("완결 캠페인", [s["campaigns"] for s in summaries]),
        ("승률", [s["win_rate"] for s in summaries]),
        ("평균 순손익 / 캠페인", [s["expectancy_usdt"] for s in summaries]),
        ("평균 이익 / 평균 손실 절댓값", [s["payoff_ratio"] for s in summaries]),
        ("수수료 USDT", [s["fees"] for s in summaries]),
        ("관측 펀딩 USDT", [s["funding"] for s in summaries]),
        ("미완결 캠페인", [len(r["unfinished"]) for r in runs]),
    ):
        rows.append(f"| {name} | {number(values[0])} | {number(values[1])} |")
    study = report["entry_study"]
    rows += ["", "## 진입 신호 분리 측정", "",
             f"고정 {study['horizon_s']}초, 진입·청산 모두 시장가 호가와 비용 가정. 비중/출구 정책 수익과 별개다.",
             f"겹치지 않는 신호 {study['signals']}개, 미래 경로 미완성 신호 {study['censored']}개.",
             f"평균 순가격 수익률: 신호 {number(study['signal_mean'])}, 같은 날·종목·방향 무작위 기준 {number(study['random_mean'])}.",
             "이 비교의 펀딩은 제외되어 있다. 단일 시드의 진단이며 전체 정책을 무작위 진입으로 재생한 실험은 아니다.",
             "", "## 보류 근거", ""]
    rows += [f"- `{reason}`" for reason in report["gate"]["reasons"]] or ["- 후속 페이퍼 비교 후보. 실거래 허가가 아니다."]
    rows += ["", "## 자료와 한계", "",
             f"입력 감사: `{json.dumps(report['data_quality'], ensure_ascii=False, sort_keys=True)}`",
             f"펀딩 범위 확인: `{report['funding_complete']}`. 누락 펀딩을 0으로 확정하지 않았다.",
             "종목들은 녹화된 부분집합이다. 전체 시장의 선정 성과와 생존 편향을 해결한 결과로 해석하지 않는다.",
             "미청산 평가는 최우선 호가와 비용을 쓴 추정이며, 실제 전량 시장가 체결 보증이 아니다.",
             "손절은 지연 이후 관측된 호가 깊이에서 체결한다. 명목 스탑 예산을 실제 최악손실 보증으로 읽지 않는다.",
             "블록 부트스트랩은 관측한 날짜만 재표집하며 아직 관측하지 못한 폭락을 만들어 내지 않는다.",
             "", "원본·설정·코드 해시는 manifest.json, 체결/주문 장부는 events-*.jsonl, 완결/미완결 캠페인 원장은 results.json에 있다.", ""]
    (path / "report.md").write_text("\n".join(rows), encoding="utf-8")


def audit_report(score, path):
    s, a = score["summary"], score["accounting"]
    lines = ["# 기존 Track B 경제적 성과 감사", "", f"관측 창: {score['since']} ~ {score['until']} (엔진 로컬 시각).", "",
             f"완결 캠페인 {s['campaigns']}개, 승률 {number(s['win_rate'])}, 수수료 포함 순손익 {number(a['completed_net_usdt'])} USDT.",
             f"미완결/불일치에서 확인된 엔진 손익 {number(a['unresolved_recorded_net_usdt'])} USDT.",
             f"합계 {number(a['known_campaign_fill_net_usdt'])} USDT. 수동 마무리·펀딩이 제외되어 전체 경제적 성과는 미확정이다.", "",
             "승률의 분모는 완결 캠페인이다. 미완결을 승리나 손실 0으로 취급하지 않는다. 혼합된 위험 프로필의 과거 승률로 현재 사이즈를 올리지 않는다.", "",
             "근거: bot.research_b가 원본 events.jsonl·hunt-history.jsonl에서 재구성한 audit.json.", ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")
