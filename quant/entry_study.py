"""Frozen entry-gate and fixed-basket contrasts on a completed domestic capture.

python -m quant.entry_study --capture DIRECTORY --output NEW_DIRECTORY
Uses public recordings only; it never reads live params or submits orders.
"""
import argparse
from collections import Counter
import json
from pathlib import Path

from .compare import run
from .config import Config, canonical, file_hash


GATES = {"strict": (1, 1, 1), "no_decay": (1, 1, 0), "pre_gate": (0, 0, 0)}
ARMS = ["reference_none", "reference_none_stress", "reference_structure", "reference_structure_stress"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--venues", nargs="+", choices=["coinone", "bithumb", "upbit", "korbit"],
                        default=["coinone", "bithumb", "upbit", "korbit"])
    parser.add_argument("--registry", default="data/quant/trials.sqlite")
    args = parser.parse_args()
    capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    protocol = dict(status="DEVELOPMENT", gates=GATES, arms=ARMS, venues=args.venues,
                    universes=["recorded_basket", "btc_eth"], capture=str(capture),
                    capture_manifest_sha256=file_hash(capture / "manifest.json"),
                    invariants="first-entry quality only; TTL90, reference exits, one unit, risk unchanged, min_trades_10s unchanged",
                    interpretation="selected fixed baskets, not a whole-market dynamic scanner; same old tape is not OOS")
    (output / "protocol.json").write_text(canonical(protocol), encoding="utf-8")
    results = []
    for venue in args.venues:
        source = capture / venue
        original = Config.read(source / "config.json")
        seed = json.loads((source / "seed.json").read_bytes())
        for universe in protocol["universes"]:
            for gate, values in GATES.items():
                d = original.data
                if universe == "btc_eth":
                    d["books"] = {s: b for s, b in d["books"].items() if s in {"BTCKRW", "ETHKRW"}}
                    if len(d["books"]) != 2:
                        raise ValueError("BTC and ETH must both exist in the recording")
                d["name"] = f"{venue}-{universe}-{gate}"
                d["strategy"].update(dict(zip(("entry_v", "entry_flow", "entry_decay"), values)))
                d["strategy"].update(entry_mult=0, entry_random=0)
                if d["strategy"]["max_units"] != 1 or d["signal"]["c1_on"] != 1:
                    raise ValueError("study requires one unit and keeps the candle detector for exits")
                trial = output / d["name"]
                trial.mkdir()
                config_path, seed_path = trial / "config.json", trial / "seed.json"
                config_path.write_text(Config.create(d).text, encoding="utf-8")
                seed_path.write_text(canonical({s: seed[s] for s in d["books"]}), encoding="utf-8")
                run(argparse.Namespace(output=str(trial / "replay"), config=str(config_path),
                                       seed=str(seed_path), scans=None, funding=None, arms=ARMS,
                                       registry=args.registry, tapes=[str(source / "tape.jsonl")]))
                result = json.loads((trial / "replay" / "results.json").read_bytes())
                funnel = Counter()
                for counts in result["entry_funnel"].values():
                    funnel.update(counts)
                for arm, row in result["runs"].items():
                    with (trial / "replay" / f"events-{arm}.jsonl").open(encoding="utf-8") as stream:
                        ledger = [json.loads(line) for line in stream]
                    entry_orders = sum(e["kind"] == "ORDER" and e.get("role") == "buy" for e in ledger)
                    entry_fills = sum(e["kind"] == "FILL" and e.get("role") == "buy" for e in ledger)
                    results.append(dict(venue=venue, universe=universe, gate=gate, arm=arm,
                                        start=result["start"], end=result["end"],
                                        completed=len(row["campaigns"]), unfinished=len(row["unfinished"]),
                                        wins=row["summary"]["wins"], counts=row["counts"],
                                        entry_orders=entry_orders, entry_fills=entry_fills,
                                        realized_net=row["realized_net"], liquidation_net=row["liquidated_estimate_net"],
                                        max_drawdown=row["max_drawdown"], mean_hold_s=row["mean_hold_s"],
                                        funnel=dict(funnel), entry_funnel=result["entry_funnel"],
                                        report=str((trial / "replay" / "report.md").relative_to(output))))
                (output / "summary.json").write_text(canonical(dict(protocol=protocol, results=results)), encoding="utf-8")
    lines = ["# 국내 첫 진입 문턱과 고정 바구니 대조", "", "**HOLD: 개발 녹화 재사용. 수익 우위·실거래 채택을 판정하지 않는다.**", "",
             "strict=속도+흐름+감쇠, no_decay=속도+흐름, pre_gate=세 품질 조건 해제. 이전 B 전체 복원이 아니다.",
             "기존 90초 진입 대기·B 기준 청산·한 유닛·동일 위험·현물 실행 제약을 유지한다. recorded_basket은 녹화한 일부 종목이고 전체 시장 스캐너가 아니다.",
             "신호 수는 공통 reference_none에서의 품질 조건 통과 수이며 국면/체결 수/풀/나머지 전략 조건과 구분한다. 단위는 원.", "",
             "| 거래소 | 바구니 | 진입 조건 | 대조 | 신호 | 진입 주문 | 완결 / 미완결 | 실현 순손익 | 청산 평가 포함 |",
             "|---|---|---|---|---:|---:|---:|---:|---:|"]
    for r in results:
        value = f"{r['liquidation_net']:.2f}" if r["liquidation_net"] is not None else "산출 불가"
        lines.append(f"| {r['venue']} | {r['universe']} | {r['gate']} | {r['arm']} | {r['funnel'].get('configured_quality', 0)} | {r['entry_orders']} | {r['completed']} / {r['unfinished']} | {r['realized_net']:.2f} | {value} |")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(canonical(dict(output=str(output), completed_runs=len(results), status="HOLD")), flush=True)


if __name__ == "__main__":
    main()
