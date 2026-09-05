"""Factorial policy comparison with frozen costs, contracts and optional phase tape.

python -m quant.compare TAPE --config CONFIG --seed SEED --output NEW_DIRECTORY
"""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import ExitStack
import datetime as dt
import hashlib
import json
from pathlib import Path
import statistics

from .config import Config, canonical, source_hashes, file_hash
from .data import Reader, Funding, epoch_ms
from .markouts import Markouts
from .registry import Registry
from .scalp import ResearchEngine
from .validation import summarize
from .venues import research_config, BITHUMB_END


class PhaseSchedule:
    """Only completed scans, never the scan start or a later inferred label.

    hunt-history timestamps are local completion times rounded down to seconds.
    Add one second to avoid using a scan before its actual completion.
    Rows gate entries by phase ONLY, not hunt qualification/book ownership.
    """
    def __init__(self, raw):
        self.times, self.rows, self.quality = [], [], Counter()
        for line in raw.splitlines():
            try:
                d = json.loads(line)
                stamp = dt.datetime.fromisoformat(d["t"])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=dt.timezone(dt.timedelta(hours=9)))
                t = int(stamp.timestamp() * 1000) + 1000
                if self.times and t <= self.times[-1]:
                    raise ValueError("unordered/duplicate completed scan")
                rows = {}
                for r in d["rows"]:
                    ai = r.get("side") if r.get("phase") in {"markup", "markdown"} else None
                    phase_det = r.get("phase_det") if d.get("hunt", {}).get("ai_read") else r.get("phase")
                    det = r.get("side_det") if d.get("hunt", {}).get("ai_read") else r.get("side")
                    rows[r["symbol"]] = (ai, det if phase_det in {"markup", "markdown"} else None)
                self.times.append(t)
                self.rows.append(rows)
            except (ValueError, KeyError, TypeError):
                self.quality["invalid_scans"] += 1

    def side(self, symbol, t, deterministic, max_age_s):
        i = bisect_right(self.times, t) - 1
        if i < 0 or t - self.times[i] > max_age_s * 1000:
            return None
        return self.rows[i].get(symbol, (None, None))[bool(deterministic)]


def variants(base, phases=False):
    configs = {}
    for regime in (["none", "structure", "recorded", "recorded_det"] if phases else ["none", "structure"]):
        for policy in ("reference", "scalp_exit", "scalp"):
            d = base.data
            name = f"{policy}_{regime}"
            d["name"] = name
            d["research"].update(policy=policy, regime=regime)
            cfg = Config.create(d)
            configs[name] = cfg
            configs[name + "_stress"] = cfg.stressed()
            if d["research"]["venue"] == "bithumb":
                for label, schedule in (("calendar", [[0, 0, 0], [BITHUMB_END, .0004, .0004]]), ("no_coupon", [[0, .0025, .0025]])):
                    extra = cfg.data
                    extra["name"] += "_" + label
                    extra["research"]["fee_schedule"] = schedule
                    configs[name + "_" + label] = Config.create(extra)
    return configs


def load_seed(path, first_t, engines):
    if not path:
        return dict(source=None, symbols=[])
    raw = Path(path).read_bytes()
    seed = json.loads(raw)
    for sym, value in seed.items():
        if sym not in engines[0].features:
            raise ValueError("seed symbol outside configured basket")
        if value["available_ms"] > first_t:
            raise ValueError("seed was unavailable at replay start")
        for key, duration in (("c1", 60000), ("c15", 900000)):
            rows = value[key]
            if any(r["ts"] + duration > value["available_ms"] for r in rows) or any(a["ts"] >= b["ts"] for a, b in zip(rows, rows[1:])):
                raise ValueError("future/unordered seed candle")
            if any(not all(isinstance(r[k], (int, float)) for k in ("ts", "o", "h", "l", "c", "v")) or not 0 < r["l"] <= min(r["o"], r["c"]) <= max(r["o"], r["c"]) <= r["h"] for r in rows):
                raise ValueError("invalid seed OHLC")
        engines[0].features[sym].seed_candles(value["c1"], value["c15"])
    return dict(source=str(Path(path).resolve()), sha256=hashlib.sha256(raw).hexdigest(), symbols=sorted(seed))


def entry_reading(params, f, signals, side):
    """Quality diagnostics for a flat entry; not an order or a position gate.

    Read the same current feature frame as Strategy. A looser frozen config
    must not silently retain the three-confirmation markout population.
    """
    name = "DIP_SLOWING" if side == "long" else "POP_STALLING"
    rows = [r for r in signals or [] if r.get("sig") == name and not r.get("shadow")]
    raw = bool(rows)
    velocity = any(r.get("src") == "v" for r in rows)
    bs = f.get("bs10")
    flow = bs is not None and (1 if side == "long" else -1) * (bs - .5) > 0
    decay = bool(f.get("sell_decay" if side == "long" else "buy_decay"))
    qualified = raw and (not params.get("entry_v") or velocity) and (not params.get("entry_flow") or flow) and (not params.get("entry_decay") or decay)
    return dict(raw=raw, velocity=velocity, velocity_flow=velocity and flow,
                triple=velocity and flow and decay, configured_quality=qualified)


def report(result, output):
    lines = ["# 단기 청산·국면 대조 실험", "", "판정: **HOLD — 개발 자료, 수익 우위 미입증**.", "",
             f"시장: {result['venue']} / {result['market']}. 금액 단위: {result['currency']}. 동일 입력·한 유닛·풀 1개·위험 상한, 정책별 독립 가상 자본.", "",
             "| 정책 | 완결 | 승률 | 실현 순손익 | 청산 추정 포함 | 미완결 | 평균 보유 초 | 수수료 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, run in result["runs"].items():
        s = run["summary"]
        win = f"{100 * s['win_rate']:.1f}%" if s["win_rate"] is not None else "—"
        hold = f"{run['mean_hold_s']:.1f}" if run["mean_hold_s"] is not None else "—"
        liquidation = f"{run['liquidated_estimate_net']:.6f}" if run['liquidated_estimate_net'] is not None else "— 깊이/신선도 부족"
        lines.append(f"| {name} | {len(run['campaigns'])} | {win} | {run['realized_net']:.6f} | {liquidation} | {len(run['unfinished'])} | {hold} | {s['fees']:.6f} |")
    lines += ["", "`reference`: 추출한 B의 진입 대기/청산. `scalp_exit`: 90초 진입 대기 유지, 청산만 교체. `scalp`: 진입 대기도 5초로 단축. 새 청산은 60초 보유 한도·전제 무효화·반대 속도 정체 청산.",
              "`none`: 외부 국면 필터 없음. `structure`: 마감 15분·1시간 구조 방향 동의. `recorded/recorded_det`: 완료 스캔의 AI/결정론 방향만 사용.",
              "B 헌터의 자격·보유 유지·퇴출·급등 목표 매도 전체를 재현한 결과가 아니다. 국내 큐는 실거래 보정 전의 FIFO 가설이다.",
              "`stress`: 요율 2배 또는 행사 종료 가정 중 높은 값, 실행비용 추가 25bp, 지연 2배. 빗썸 기본=종료 후 쿠폰 0.04%, calendar=종료 시각 적용, no_coupon=0.25%.", "",
              "## 진입 이후의 실행 가능 가격", "", "값은 bp. 괄호 안은 관측 수. 신호는 지연 뒤 시장가 진입 진단, fill은 가상 지정가 부분체결이다. 지평 뒤 청산 지연도 포함한다.", "",
              "| 표본 | 1초 | 3초 | 5초 | 10초 | 30초 | 60초 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for group, horizons in result["markouts"]["groups"].items():
        cells = [f"{r['mean_bp']:.3f} ({r['n']})" if r["mean_bp"] is not None else "—" for r in horizons.values()]
        lines.append("| " + group + " | " + " | ".join(cells) + " |")
    lines += ["", "## 첫 진입 조건별 신호 수", "",
              "실제 설정: `" + canonical(result["entry_signal_definition"]) + "`. 신호 진단은 재고·풀·전략의 나머지 조건을 적용하기 전이며 주문 수가 아니다.", "",
              "| 종목/방향 | 원시 정체 | 속도 정체 | 속도+흐름 | 세 조건 | 설정 통과 | 유동성/국면 자격도 통과 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for book, counts in result["entry_funnel"].items():
        lines.append("| " + book + " | " + " | ".join(str(counts.get(k, 0)) for k in ("raw", "velocity", "velocity_flow", "triple", "configured_quality", "configured_eligible")) + " |")
    lines += ["", "## 보류·해석", ""] + ["- " + note for note in result["limitations"]]
    Path(output).write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    base = Config.read(args.config)
    if base.data["version"] == 1:
        base = research_config(base)
    base_data = base.data
    phases = None
    sources = dict(code=source_hashes())
    source_dir = output / "source"
    root = Path(__file__).resolve().parents[1]
    for name, expected in sources["code"].items():
        data = (root / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("source changed during snapshot")
        target = source_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if args.scans:
        raw = Path(args.scans).read_bytes()
        (output / "scans.jsonl").write_bytes(raw)
        sources["scans"] = dict(path=str(Path(args.scans).resolve()), sha256=hashlib.sha256(raw).hexdigest())
        phases = PhaseSchedule(raw)
    configs = variants(base, phases is not None)
    if args.arms:
        unknown = set(args.arms) - set(configs)
        if unknown:
            raise ValueError(f"unknown arms: {sorted(unknown)}")
        # The ungated reference provides the common signal/control observations.
        wanted = set(args.arms) | {"reference_none"}
        configs = {k: v for k, v in configs.items() if k in wanted}
    sources["matrix"] = {k: v.data for k, v in configs.items()}
    if args.seed:
        sources["seed"] = dict(path=str(Path(args.seed).resolve()), sha256=file_hash(args.seed))
    funding = Funding(args.funding)
    sources["funding"] = funding.source
    with ExitStack() as stack:
        registry = Registry(args.registry)
        stack.callback(registry.close)
        trial = registry.register(base, sources)
        registry.claim(trial, base, sources["code"])
        manifest = dict(trial=trial, sources=sources, mode="development", config=base.data)
        (output / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
        try:
            engines = []
            for name, cfg in configs.items():
                ledger = stack.enter_context((output / f"events-{name}.jsonl").open("x", encoding="utf-8"))
                engines.append(ResearchEngine(cfg, name=name, observe=name == "reference_none", sink=lambda row, f=ledger: f.write(canonical(row) + "\n")))
            origin = engines[0]
            for engine in engines:
                engine.features = origin.features
                engine.phase_schedule = phases
            reader = Reader(base.data["books"], track_coverage=True)
            markouts = Markouts(base.data["validation"]["seed"])
            control_minute = {}
            entry_funnel = defaultdict(Counter)
            funding_i = 0
            seeded = False
            for event in reader.files(args.tapes):
                if not seeded:
                    manifest["seed"] = load_seed(args.seed, event.t, engines)
                    seeded = True
                while funding_i < len(funding.rows) and funding.rows[funding_i]["t"] <= event.t:
                    row = funding.rows[funding_i]
                    if row["t"] >= (reader.start or event.t):
                        for engine in engines:
                            if not engine.spot:
                                engine.funding(row)
                    funding_i += 1
                markouts.quote(event)
                signals = None
                prior_feature_t = origin.last_feature.get(event.symbol)
                for engine in engines:
                    before = len(engine.events)
                    if engine is origin:
                        signals = engine.process(event)
                    else:
                        engine.process(event, feature_update=signals)
                    for row in engine.events[before:]:
                        if row["kind"] == "FILL" and row["role"] == "buy":
                            side = row["book"].rsplit(":", 1)[1]
                            markouts.add("fill:" + engine.name, event.symbol, side, row["t"], row["qty"], engine,
                                         price=row["px"], entry_fee=row["fee"] / (row["qty"] * row["px"]))
                    # Ledgers are persisted; only the new event batch is needed here.
                    engine.events.clear()
                f = origin.features[event.symbol].f
                if f.get("atr") and f.get("t") != prior_feature_t and origin.fresh(event.symbol) and event.t / 1000 - f.get("t", 0) <= 2:
                    for side in base_data["books"][event.symbol]["sides"]:
                        b = origin.books[f"{event.symbol}:{side}"]
                        qty = b.strategy.p["unit_qty"]
                        reading = entry_reading(b.strategy.p, f, signals, side)
                        entry_funnel[b.key].update(k for k, value in reading.items() if value)
                        if reading["configured_quality"]:
                            if b.strategy.allowed(f) is None:
                                entry_funnel[b.key]["configured_eligible"] += 1
                            markouts.add("signal", event.symbol, side, event.t, qty, origin)
                        key = (event.symbol, side)
                        if control_minute.get(key) != event.t // 60000:
                            control_minute[key] = event.t // 60000
                            markouts.add("control", event.symbol, side, event.t, qty, origin)
                origin.observations.clear()
                if reader.quality["messages"] % 250000 == 0:
                    print(canonical(dict(messages=reader.quality["messages"], t=event.t, completed={e.name: len(e.campaigns) for e in engines})), flush=True)
            if not seeded:
                raise ValueError("no valid market events")
            runs = {}
            for engine in engines:
                result = engine.result()
                summary = summarize(result, base.data["validation"])
                summary["expectancy_quote"] = summary.pop("expectancy_usdt")
                result.update(summary=summary, mean_hold_s=statistics.mean((c["t1"] - c["t0"]) / 1000 for c in result["campaigns"]) if result["campaigns"] else None)
                runs[engine.name] = result
            spot = base.data["research"]["market"] == "spot"
            result = dict(trial=trial, status="HOLD", venue=base.data["research"]["venue"], market=base.data["research"]["market"], currency="KRW" if spot else "USDT",
                          start=reader.start, end=reader.end, data_quality=dict(reader.quality), phase_quality=dict(phases.quality) if phases else None,
                          funding_complete=True if spot else funding.covers(reader.start, reader.end, base.data["books"]),
                          entry_signal_definition={k: base_data["strategy"][k] for k in ("entry_v", "entry_flow", "entry_decay")},
                          entry_funnel={k: dict(v) for k, v in sorted(entry_funnel.items())},
                          runs=runs, markouts=markouts.result(), limitations=[
                              "개발 실험이다. 동일 창을 보고 임계값을 고른 뒤 OOS로 재명명하지 않는다.",
                              "청산·국면 대조는 동일한 기회와 실행 가정에서 수행한다. 정책에 따라 풀 점유시간과 후속 체결 기회는 달라진다.",
                              "기존 B 전체 복원이 아니다. recorded 비교도 스캔 완료 후 방향 필터만 적용하며 헌터의 책 유지·퇴출은 포함하지 않는다.",
                              "국내 maker 큐는 실거래 미보정 가설이다. 높은/낮은 체결률이 P&L의 상한/하한이라는 뜻은 아니다.",
                              "signal 진단은 설정된 품질 조건만 적용한다. 유동성/국면/풀/재고 조건 및 무작위 거부·미확인 소량 진입과는 별개다. entry_funnel의 실행 자격은 reference_none의 체결 수 하한까지다.",
                              "정책별 미완결·최소 주문 미만 잔고·깊이 부족·호가 공백은 결과에 남는다. 60초는 요청 시한이며 체결 보장이 아니다.",
                              "20일·100캠페인·독립 에피소드·미사용 전방 기간이 필요하다. 이 비교 명령은 승격 기능을 제공하지 않는다."])
            manifest["tapes"] = reader.sources
            (output / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
            (output / "results.json").write_text(canonical(result), encoding="utf-8")
            report(result, output / "report.md")
            registry.finish(trial, dict(status="HOLD", output=str(output.resolve()), data_quality=dict(reader.quality)))
            print(canonical(dict(output=str(output.resolve()), status="HOLD", campaigns={k: len(v["campaigns"]) for k, v in runs.items()})), flush=True)
        except BaseException as e:
            registry.finish(trial, dict(error=type(e).__name__, message=str(e)), failed=True)
            raise


def add_arguments(parser):
    parser.add_argument("tapes", nargs="+")
    parser.add_argument("--config", default="quant/configs/reference.json")
    parser.add_argument("--seed")
    parser.add_argument("--scans", help="completed hunt-history scans; naive timestamps explicitly interpreted as KST")
    parser.add_argument("--funding")
    parser.add_argument("--arms", nargs="+", help="explicit subset recorded in manifest; reference_none is always included")
    parser.add_argument("--registry", default="data/quant/trials.sqlite")
    parser.add_argument("--output", required=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
