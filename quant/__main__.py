"""python -m quant: isolated replay, audit, registration, and recorder-fed paper."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import time

from .config import Config, canonical, file_hash, source_hashes
from .coverage import attribute_episodes, load_sidecar, universe_coverage
from .data import Funding, Reader, epoch_ms
from .engine import Engine
from .registry import Registry, now_ms
from .report import audit_report, write_report
from .validation import entry_study, gates, summarize, walk_forward


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def replay(args):
    cfg = Config.read(args.config)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    code = source_hashes()
    registry = Registry(args.registry)
    trial = None
    try:
        paths = [Path(p).resolve() for p in args.tapes]
        inputs = [dict(path=str(p), sha256=file_hash(p), bytes=p.stat().st_size) for p in paths]
        funding = Funding(args.funding)
        universe, universe_source = load_sidecar(args.universe)
        episodes, episode_source = load_sidecar(args.episodes)
        if args.trial:
            trial = args.trial
        else:
            trial = registry.register(cfg, dict(code=code, tapes=inputs, funding=funding.source, universe=universe_source, episodes=episode_source))
        spec = registry.claim(trial, cfg, code)
        prospective = bool(spec["prospective"])
        window = spec.get("window")
        start = epoch_ms(window["start"]) if window else -1
        end = epoch_ms(window["end"]) if window else 2**63 - 1
        if prospective and now_ms() < end:
            raise ValueError("prospective window has not ended; do not inspect it repeatedly")
        manifest = dict(trial_id=trial, config=cfg.data, code=code, tapes=inputs, funding=funding.source, universe=universe_source, episodes=episode_source, specification=spec)
        save(output / "manifest.json", manifest)
        reader = Reader(cfg.data["books"], track_coverage=universe is not None)
        with ExitStack() as stack:
            engines = []
            for name, config in (("reference", cfg), ("cost_stress", cfg.stressed())):
                stream = stack.enter_context((output / f"events-{name}.jsonl").open("x", encoding="utf-8"))
                sink = lambda row, stream=stream: stream.write(canonical(row) + "\n")
                engines.append(Engine(config, name, observe=name == "reference", sink=sink))
            # Identical feature path for both cost policies; no second feature computation.
            engines[1].features = engines[0].features
            funding_index, count = 0, 0
            for event in reader.files(paths):
                if event.t >= end:
                    continue
                while funding_index < len(funding.rows) and funding.rows[funding_index]["t"] <= event.t:
                    row = funding.rows[funding_index]
                    for engine in engines:
                        if row["t"] >= engine.now and (engine.start is not None or row["t"] == event.t):
                            engine.funding(row)
                    funding_index += 1
                for engine in engines:
                    engine.trading = event.t >= start
                signals = engines[0].process(event)
                engines[1].process(event, signals)
                count += 1
                if count % 200000 == 0:
                    print(f"replayed {count:,} messages", flush=True)
            if reader.start is None or not engines[0].observations:
                raise ValueError("no usable quote/feature observations")
            if prospective and (reader.start > start or reader.end < end - 1000):
                raise ValueError("prospective window not fully covered")
            if source_hashes() != code:
                raise ValueError("source changed during evaluation")
            if reader.sources != inputs:
                raise ValueError("input hashes changed before replay")
            policy = cfg.data["validation"]
            universe_evidence = universe_coverage(universe, reader.quotes, max(start, engines[0].start), engines[0].now, cfg.data["books"], cfg.data["execution"]["quote_max_age_ms"])
            result = dict(trial_id=trial, data_quality=dict(reader.quality),
                          funding_complete=funding.covers(engines[0].start, engines[0].now, cfg.data["books"]),
                          universe_complete=universe_evidence["complete"], universe_evidence=universe_evidence,
                          runs={engine.name: engine.result() for engine in engines},
                          entry_study=entry_study(engines[0].observations, cfg.data["execution"], policy))
            result["episode_evidence"] = attribute_episodes(episodes, result["runs"], policy)
            result["independent_episodes_complete"] = result["episode_evidence"]["complete"]
            for run in result["runs"].values():
                run["summary"] = summarize(run, policy)
            result["gate"] = gates(result, cfg, prospective)
            write_report(result, cfg, output)
            registry.finish(trial, dict(output=str(output.resolve()), results_sha256=file_hash(output / "results.json"), gate=result["gate"]))
            print(canonical(dict(output=str(output.resolve()), gate=result["gate"], net={k: v["liquidated_estimate_net"] for k, v in result["runs"].items()})))
    except BaseException as exc:
        if trial:
            try:
                registry.finish(trial, dict(error_type=type(exc).__name__, output=str(output.resolve())), failed=True)
            except ValueError:
                pass
        raise
    finally:
        registry.close()


def paper(args):
    cfg = Config.read(args.config)
    path, output = Path(args.tape), Path(args.output)
    if path.suffix != ".jsonl" or args.seconds <= 0:
        raise ValueError("paper requires a growing uncompressed recorder .jsonl and positive duration")
    output.mkdir(parents=True, exist_ok=False)
    save(output / "manifest.json", dict(config=cfg.data, code=source_hashes(), mode="paper", tape=str(path.resolve()),
                                        note="A new simulated account; replay pre-roll warms features only. No exchange connection."))
    reader = Reader(cfg.data["books"])
    start = now_ms()
    deadline = time.monotonic() + args.seconds
    with path.open("r", encoding="utf-8") as tape, (output / "events.jsonl").open("x", encoding="utf-8") as ledger:
        identity = (path.stat().st_dev, path.stat().st_ino)
        engine = Engine(cfg, observe=False, sink=lambda row: ledger.write(canonical(row) + "\n"))
        while time.monotonic() < deadline:
            stat = path.stat()
            if (stat.st_dev, stat.st_ino) != identity or stat.st_size < tape.tell():
                raise ValueError("paper tape replaced/truncated; start a new explicit session")
            offset = tape.tell()
            line = tape.readline()
            if not line.endswith("\n"):
                tape.seek(offset)
                ledger.flush()
                time.sleep(.2)
                continue
            event = reader.parse(line)
            if event:
                engine.trading = event.t >= start and 0 <= now_ms() - event.t <= 5000
                engine.process(event)
        result = engine.result()
        result.update(mode="paper", funding_complete=False, data_quality=dict(reader.quality),
                      note="Open positions/reservations are disclosed. This session does not resume a previous simulated account.")
        save(output / "paper.json", result)
    print(canonical(dict(output=str(output.resolve()), realized=result["realized_net"], unfinished=len(result["unfinished"]))))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    from .compare import add_arguments as compare_arguments
    compare_arguments(sub.add_parser("compare"))
    p = sub.add_parser("replay")
    p.add_argument("tapes", nargs="+")
    p.add_argument("--config", default="quant/configs/reference.json")
    p.add_argument("--output", required=True)
    p.add_argument("--registry", default="data/quant/trials.sqlite")
    p.add_argument("--funding")
    p.add_argument("--trial")
    p.add_argument("--universe", help="expected point-in-time universe/coverage sidecar")
    p.add_argument("--episodes", help="externally attributed complete episode intervals")
    p = sub.add_parser("paper")
    p.add_argument("--config", default="quant/configs/reference.json")
    p.add_argument("--tape", required=True)
    p.add_argument("--seconds", type=int, default=3600)
    p.add_argument("--output", required=True)
    p = sub.add_parser("register")
    p.add_argument("--config", default="quant/configs/reference.json")
    p.add_argument("--registry", default="data/quant/trials.sqlite")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p = sub.add_parser("trials")
    p.add_argument("--registry", default="data/quant/trials.sqlite")
    p = sub.add_parser("folds")
    p.add_argument("--episodes", required=True)
    p.add_argument("--train-end", required=True)
    p.add_argument("--validation-start", required=True)
    p.add_argument("--validation-end", required=True)
    p.add_argument("--embargo-s", type=int, default=900)
    p.add_argument("--output", required=True)
    p = sub.add_parser("audit")
    p.add_argument("--events", default="logs/events.jsonl")
    p.add_argument("--scans", default="logs/hunt-history.jsonl")
    p.add_argument("--since", default="2026-09-03 00:00:00")
    p.add_argument("--until", required=True)
    p.add_argument("--output", required=True)
    args = ap.parse_args()
    if args.command == "compare":
        from .compare import run
        run(args)
    elif args.command == "replay":
        replay(args)
    elif args.command == "paper":
        paper(args)
    elif args.command in {"register", "trials"}:
        registry = Registry(args.registry)
        try:
            if args.command == "trials":
                print(canonical(registry.list()))
            else:
                cfg = Config.read(args.config)
                if cfg.data["version"] != 1:
                    raise ValueError("schema 2 is development comparison only; prospective promotion is not implemented")
                print(registry.register(cfg, dict(code=source_hashes()), dict(start=args.start, end=args.end), prospective=True))
        finally:
            registry.close()
    elif args.command == "folds":
        episodes = json.loads(Path(args.episodes).read_text(encoding="utf-8"))
        for e in episodes:
            e["start"], e["end"] = epoch_ms(e["start"]), epoch_ms(e["end"])
        save(args.output, walk_forward(episodes, epoch_ms(args.train_end), epoch_ms(args.validation_start), epoch_ms(args.validation_end), args.embargo_s * 1000))
    else:
        from bot.research_b import records, scorecard
        events, bad_events = records(args.events, args.until)
        scans, bad_scans = records(args.scans, args.until)
        result = scorecard(events, scans, args.since, args.until)
        result["source_quality"] = dict(event_bad_lines=bad_events, scan_bad_lines=bad_scans)
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=False)
        save(output / "audit.json", result)
        audit_report(result, output / "audit.md")
        print(canonical(result["accounting"]))


if __name__ == "__main__":
    main()
