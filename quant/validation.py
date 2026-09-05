"""Descriptive statistics and explicit promotion gates; no parameter search."""
from bisect import bisect_left
from collections import defaultdict
import math
import random
import statistics as stats


def percentile(values, p):
    xs = sorted(values)
    if not xs:
        return None
    z = (len(xs) - 1) * p
    i = int(z)
    return xs[i] + (xs[min(i + 1, len(xs) - 1)] - xs[i]) * (z - i)


def block_interval(blocks, seed, samples):
    """Resample whole UTC days, including all symbols. Few days yield no CI."""
    values = list(blocks.values())
    if len(values) < 3:
        return dict(blocks=len(values), lower=None, upper=None)
    rng = random.Random(seed)
    means = [stats.mean(rng.choices(values, k=len(values))) for _ in range(samples)]
    return dict(blocks=len(values), lower=percentile(means, .025), upper=percentile(means, .975))


def summarize(run, policy):
    cs = run["campaigns"]
    winners, losers = [c["net"] for c in cs if c["net"] > 0], [c["net"] for c in cs if c["net"] < 0]
    days = defaultdict(float)
    for c in cs:
        days[str(c["t0"] // 86400000)] += c["net"]
    wins, losses = sum(winners), -sum(losers)
    # Campaign returns have differing denominators. Do not call their mean CAGR.
    returns = [c["net"] / c["wallet"] for c in cs if c["wallet"] > 0]
    return dict(campaigns=len(cs), wins=len(winners), losses=len(losers), flat=len(cs) - len(winners) - len(losers),
                win_rate=len(winners) / len(cs) if cs else None,
                expectancy_usdt=stats.mean(c["net"] for c in cs) if cs else None,
                average_win=stats.mean(winners) if winners else None,
                average_loss=stats.mean(losers) if losers else None,
                payoff_ratio=stats.mean(winners) / -stats.mean(losers) if winners and losers else None,
                profit_factor=wins / losses if losses else None,
                profit_factor_unbounded=bool(wins and not losses),
                fees=sum(c["fees"] for c in cs + run["unfinished"]),
                funding=sum(c["funding"] for c in cs + run["unfinished"]),
                worst_campaign=min((c["net"] for c in cs), default=None),
                expected_shortfall_10=stats.mean(sorted(returns)[:max(1, math.ceil(len(returns) * .1))]) if returns else None,
                log_growth=math.log(run["marked_equity"] / run["initial_equity"]) if run["marked_equity"] > 0 else None,
                day_net_interval=block_interval(days, policy["seed"], policy["bootstrap_samples"]))


def entry_study(observations, execution, policy):
    """Fixed-horizon, executable-touch markouts with a same-day/time-side control.

    Signals and controls enter on the first observation AFTER latency and exit
    after the actual clock horizon. Neither sample assumes a maker fill. This
    diagnoses timing conditional on recorded coverage, not a traded portfolio.
    Select non-overlapping anchors greedily without looking at the return.
    """
    groups = defaultdict(list)
    for row in observations:
        groups[row["symbol"]].append(row)
    horizon = policy["horizon_s"] * 1000
    latency = execution["latency_ms"]
    cells = defaultdict(lambda: {"signals": [], "controls": []})
    censored = 0
    used_days = defaultdict(list)
    for sym, rows in groups.items():
        times = [r["t"] for r in rows]
        gaps = [0]
        for a, b in zip(times, times[1:]):
            gaps.append(gaps[-1] + (b - a > 5000))
        last_signal = {"long": -math.inf, "short": -math.inf}
        last_control = {"long": -math.inf, "short": -math.inf}
        for r in rows:
            i = bisect_left(times, r["t"] + latency)
            if i >= len(rows):
                censored += len(r["signals"])
                continue
            j = bisect_left(times, rows[i]["t"] + horizon)
            if j >= len(rows) or times[i] - r["t"] - latency > 2000 or times[j] - times[i] - horizon > 2000:
                censored += len(r["signals"])
                continue
            # A disconnected path is not a complete horizon even if endpoints exist.
            if gaps[j] != gaps[i]:
                censored += len(r["signals"])
                continue
            day = r["t"] // 86400000
            if times[j] // 86400000 != day:
                censored += len(r["signals"])
                continue
            for side in ("long", "short"):
                s = 1 if side == "long" else -1
                entry = rows[i]["ask" if s > 0 else "bid"] * (1 + s * execution["slip_bps"] / 10000)
                exit_px = rows[j]["bid" if s > 0 else "ask"] * (1 - s * execution["slip_bps"] / 10000)
                ret = s * (exit_px / entry - 1) - execution["taker"] * (1 + exit_px / entry)
                cell = cells[(day, sym, side)]
                if side in r["signals"] and r["t"] >= last_signal[side]:
                    cell["signals"].append(ret)
                    last_signal[side] = times[j]
                if r["t"] >= last_control[side]:
                    cell["controls"].append(ret)
                    last_control[side] = times[j]
    rng = random.Random(policy["seed"])
    signal_values, control_values, table = [], [], []
    for (day, sym, side), cell in sorted(cells.items()):
        selected, controls = cell["signals"], cell["controls"]
        if not selected or not controls:
            continue
        # Count matching with replacement makes scarcity visible, not a post-hoc filter.
        matched = rng.choices(controls, k=len(selected))
        signal_values += selected
        control_values += matched
        diff = stats.mean(selected) - stats.mean(matched)
        used_days[str(day)].append(diff)
        table.append(dict(day=day, symbol=sym, side=side, signals=len(selected), control_pool=len(controls),
                          signal_mean=stats.mean(selected), random_mean=stats.mean(matched), excess=diff))
    interval = block_interval({day: stats.mean(v) for day, v in used_days.items()}, policy["seed"], policy["bootstrap_samples"])
    return dict(horizon_s=policy["horizon_s"], signals=len(signal_values), censored=censored,
                signal_mean=stats.mean(signal_values) if signal_values else None,
                random_mean=stats.mean(control_values) if control_values else None,
                excess_day_interval=interval, cells=table,
                limitation="Taker-touch timing diagnostic, one seeded matched control; funding excluded. Not policy alpha or a random-entry portfolio test.")


def walk_forward(episodes, train_end, validation_start, validation_end, embargo_ms):
    """Keep whole externally identified episodes, with a purged time boundary.

    Each row requires an episode id and full start/end. Never infer independent
    episodes from adjacent campaigns or split them at calendar midnight.
    """
    if not train_end < validation_start < validation_end or embargo_ms < 0:
        raise ValueError("invalid fold boundaries")
    grouped = defaultdict(list)
    for row in episodes:
        if not row.get("episode") or row["end"] < row["start"]:
            raise ValueError("full episode identity/range required")
        grouped[row["episode"]].append(row)
    train, validation, purged = [], [], []
    for key, rows in grouped.items():
        start, end = min(x["start"] for x in rows), max(x["end"] for x in rows)
        if end < train_end - embargo_ms:
            train.append(key)
        elif start >= validation_start + embargo_ms and end < validation_end:
            validation.append(key)
        else:
            purged.append(key)
    return dict(train=sorted(train), validation=sorted(validation), purged=sorted(purged))


def gates(report, config, prospective=False):
    p = config.data["validation"]
    ref, stress = report["runs"]["reference"], report["runs"]["cost_stress"]
    summary = summarize(ref, p)
    quality = report["data_quality"]
    failures = []
    def require(ok, reason):
        if not ok:
            failures.append(reason)
    require(prospective, "development_or_unregistered_window")
    require(report.get("funding_complete"), "funding_incomplete")
    require(report.get("universe_complete", False), "recorded_universe_is_selection_conditional")
    require(report.get("independent_episodes_complete", False), "independent_episode_attribution_missing")
    episodes = report.get("episode_evidence", {}).get("interval", {})
    require(episodes.get("blocks", 0) >= p["min_days"] and episodes.get("lower") is not None and episodes["lower"] > 0, "episode_block_evidence_insufficient")
    require(not any(quality.get(k, 0) for k in ("invalid_messages", "reception_regressions", "late_channel_messages", "future_exchange_messages", "duplicate_messages")), "input_quality")
    for name, run in (("reference", ref), ("cost_stress", stress)):
        require(not run["unfinished"] and not run["reservations"], f"{name}_unfinished_or_reserved")
        require(not any(run["quality"].values()), f"{name}_execution_coverage")
        require(run["liquidated_estimate_net"] > 0, f"{name}_nonpositive_net")
        require(run["max_drawdown"] <= run["initial_equity"] * p["max_drawdown_frac"], f"{name}_drawdown")
        require(len(run["campaigns"]) >= p["min_campaigns"], f"{name}_sample_size")
    interval = summary["day_net_interval"]
    require(interval["blocks"] >= p["min_days"], "insufficient_day_blocks")
    require(interval["lower"] is not None and interval["lower"] > 0, "net_lower_bound_not_positive")
    excess = report["entry_study"]["excess_day_interval"]
    require(excess["blocks"] >= p["min_days"] and excess["lower"] is not None and excess["lower"] > 0, "timing_vs_random_not_established")
    require(summary["log_growth"] is not None and summary["log_growth"] > 0, "nonpositive_log_growth")
    return dict(status="HOLD" if failures else "PAPER_CANDIDATE", reasons=failures,
                live_authorized=False,
                note="Minimum evidence floors are operational choices, not a statistical guarantee. No live promotion command exists.")
