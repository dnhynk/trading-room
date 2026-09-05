"""Read-only Track B campaign scorecard. No exchange access, tuning, or params writes.

python -m bot.research_b --until "2026-09-05 12:00:00" --output logs/research-b-20260905.json
Times supplied to --since/--until are engine-local (KST on this host).
--day YYYYMMDD selects entries in that UTC day for the nightly report.
"""
import argparse
import bisect
import datetime as dt
import hashlib
import json
import math
import os
import random
import statistics as stats
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE = ("unit_frac", "cap_frac", "max_units", "cap_min_atr", "cap_per_unit", "entry_v", "entry_flow", "entry_decay", "entry_mult", "add_mult")


def records(path, until=None):
    rows, bad = [], []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            try: e = json.loads(line)
            except (ValueError, TypeError):
                bad.append(n); continue
            if not until or e.get("t", "") <= until: rows.append(e)
    return rows, bad


def binomial_upper(k, n, alpha=0.05):
    """One-sided exact Clopper-Pearson upper bound (IID Bernoulli assumption).

    Invert P_p[X <= k] = alpha; log-sum-exp avoids underflow in the tail.
    """
    if not 0 <= k <= n or not 0 < alpha < 1: raise ValueError("invalid binomial sample")
    if n == 0 or k == n: return 1.0
    if k == 0: return -math.expm1(math.log(alpha) / n)
    lo, hi = 0.0, 1.0
    coeff = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) for i in range(k + 1)]
    for _ in range(70):
        p = (lo + hi) / 2
        terms = [c + i * math.log(p) + (n - i) * math.log1p(-p) for i, c in enumerate(coeff)]
        top = max(terms)
        cdf = math.exp(top) * sum(math.exp(x - top) for x in terms)
        if cdf > alpha: lo = p
        else: hi = p
    return (lo + hi) / 2


def break_even_stop(nonstop_returns, loss):
    """p* = a / (a - log(1-L)); a = E[log(1+R) | no stop].

    Losing non-stop exits remain in a. L is a scenario, not an observed bound.
    """
    if not nonstop_returns or not 0 < loss < 1 or any(x <= -1 for x in nonstop_returns): return None
    a = stats.mean(math.log1p(x) for x in nonstop_returns)
    return a / (a - math.log1p(-loss)) if a > 0 else 0.0


def reconstruct(events, scans, since="2026-09-03 00:00:00", until=None):
    scans = sorted(scans, key=lambda r: r["t"])
    scan_times = [s["t"] for s in scans]
    modes, hunted, sizing, signal, metadata = {}, set(), {}, {}, {}
    active, done, incomplete = {}, [], []
    orphan, mismatches = 0, 0
    for e in events:
        kind, t, sym = e.get("ev"), e.get("t", ""), e.get("symbol")
        if until and t > until: continue
        if kind == "HUNT_ADD" and sym: hunted.add(sym)
        if kind == "START" and sym:
            modes[sym] = e.get("mode")
            if e.get("track") == "B": hunted.add(sym)
            elif e.get("track") == "A": hunted.discard(sym)
            for side, b in (e.get("books") or {}).items():
                c = active.get((sym, side))
                if c and abs(sum(l[0] for l in b.get("lots") or []) - c["remaining"]) > 1e-6:
                    c["issues"].append("restart position mismatch")
                    incomplete.append(active.pop((sym, side)))
            continue
        side = e.get("side") or "long"
        key = (sym, side)
        if kind == "SIZING": sizing.setdefault(key, {}).update(e)
        if kind == "SIGNAL": signal[sym] = e
        if kind == "CAMPAIGN_OPEN":
            metadata[key] = e; hunted.add(sym)
        if kind not in ("FILL", "STOP_HIT") or modes.get(sym) != "live" or sym not in hunted: continue
        buy = kind == "FILL" and e.get("role") == "buy"
        c = active.get(key)
        if buy and c is None:
            ix = bisect.bisect_right(scan_times, t) - 1
            scan = scans[ix] if ix >= 0 else {}
            row = next((r for r in scan.get("rows", []) if r["symbol"] == sym), {})
            meta = metadata.pop(key, {})
            sz = {**sizing.get(key, {}), **(meta.get("sizing") or {})}
            profile = meta.get("profile") or (scan.get("hunt") or {}).get("strat") or {}
            config = {k: meta.get(k) for k in ("profile", "sig", "build", "fees")} if meta else None
            config_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest() if config and meta.get("build") else None
            wallet = meta.get("wallet") if meta else sz.get("wallet")
            sig = signal.get(sym, {})
            atr = meta.get("atr") if meta else sz.get("atr") or sig.get("atr")
            c = dict(symbol=sym, side=side, t0=t, t1=None, remaining=0.0, net=0.0, fees=0.0,
                     bought_notional=0.0, exit_notional=0.0, taker_notional=0.0, stop=False,
                     orders=[], wallet=wallet, cap=sz.get("cap_usdt"), unit=sz.get("unit_qty"),
                     atr_pct=atr / e["px"] * 100 if atr else None, scan_atr_pct=row.get("atr_pct"),
                     profile={k: profile.get(k) for k in PROFILE}, profile_source="entry" if meta else "preceding_scan",
                     configuration=config, config_id=config_id,
                     phase=row.get("phase"), phase_det=row.get("phase_det"), scan_t=scan.get("t"),
                     issues=[])
            active[key] = c
        if c is None:
            if t >= since: orphan += 1
            continue
        q, px = float(e["qty"]), float(e["px"])
        c["net"] += float(e["pnl"])
        if buy:
            c["remaining"] += q; c["bought_notional"] += q * px
            if e.get("oid") not in c["orders"]: c["orders"].append(e.get("oid"))
        else:
            c["remaining"] -= q; c["exit_notional"] += q * px
            if kind == "STOP_HIT" or e.get("scope") == "taker": c["taker_notional"] += q * px
        if kind == "STOP_HIT":
            c["stop"] = True
            sign = 1 if side == "long" else -1
            fee = sign * (px - float(e.get("avg") or px)) * q - float(e["pnl"])
        else: fee = float(e.get("fee") or 0.0)
        c["fees"] += fee
        if "pos_qty" in e and abs(c["remaining"] - e["pos_qty"]) > 1e-5:
            c["issues"].append("fill quantity mismatch"); mismatches += 1
            c["remaining"] = float(e["pos_qty"])
        if c["remaining"] <= 1e-6:
            c["t1"] = t
            if c["remaining"] < -1e-5: c["issues"].append("close exceeds position")
            if c["wallet"] and c["wallet"] > 0: c["return"] = c["net"] / c["wallet"]
            else: c["return"] = None; c["issues"].append("entry wallet unavailable")
            c["hold_s"] = (dt.datetime.fromisoformat(t) - dt.datetime.fromisoformat(c["t0"])).total_seconds()
            if c["t0"] >= since: done.append(c)
            del active[key]
    unfinished = [c for c in list(active.values()) + incomplete if c["t0"] >= since]
    return sorted(done, key=lambda c: (c["t0"], c["symbol"])), unfinished, dict(orphan_closes=orphan, quantity_mismatches=mismatches,
                        incomplete_campaigns=sum(bool(c["issues"]) for c in unfinished), open_campaigns=sum(not c["issues"] for c in unfinished))


def grouped_interval(campaigns, group="day", draws=2000):
    """Descriptive cluster bootstrap; a day is resampled whole across symbols."""
    blocks = defaultdict(list)
    for c in campaigns:
        if c["return"] is None or c["issues"]: continue
        key = c["t0"][:10] if group == "day" else (c["symbol"], c["t0"][:10])
        blocks[key].append(c["return"])
    values = list(blocks.values())
    if len(values) < 3: return dict(blocks=len(values), mean_ci95=None, reason="fewer than three blocks")
    rng = random.Random(20260905)
    means = []
    for _ in range(draws):
        selected = [x for _ in values for x in rng.choice(values)]
        means.append(stats.mean(selected))
    means.sort()
    return dict(blocks=len(values), mean_ci95=[means[int(draws * .025)], means[int(draws * .975)]])


def summary(cs):
    clean = [c for c in cs if not c["issues"] and c["return"] is not None]
    rets = [c["return"] for c in clean]
    n, stops = len(clean), sum(c["stop"] for c in clean)
    fee_stress = [c["return"] - c["fees"] / c["wallet"] for c in clean]
    stress = {str(bp): stats.mean(c["return"] - c["taker_notional"] * bp / 10000 / c["wallet"] for c in clean) if clean else None
              for bp in (5, 10, 25)}
    nonstops = [c["return"] for c in clean if not c["stop"]]
    mixed = len({cohort_key(c) for c in clean}) > 1
    exact = bool(clean) and all(c.get("config_id") for c in clean)
    pstar = {str(loss): break_even_stop(nonstops, loss) for loss in (.05, .075, .10)} if exact and not mixed else None
    return dict(campaigns=len(cs), usable=n, mixed_profiles=mixed, exact_entry_configuration=exact, stops=stops, net_usdt=sum(c["net"] for c in cs),
                win_rate=sum(c["net"] > 0 for c in clean) / n if n else None,
                stop_rate=stops / n if n else None, stop_upper95_iid=binomial_upper(stops, n),
                mean_return=stats.mean(rets) if rets else None,
                mean_log_return=stats.mean(math.log1p(r) for r in rets) if rets and min(rets) > -1 else None,
                worst_return=min(rets) if rets else None,
                worst_decile_mean=stats.mean(sorted(rets)[:max(1, math.ceil(n / 10))]) if rets else None,
                median_hold_s=stats.median(c["hold_s"] for c in cs) if cs else None,
                fees_usdt=sum(c["fees"] for c in cs), double_fee_mean_return=stats.mean(fee_stress) if fee_stress else None,
                extra_taker_bps_mean_return=stress, scenario_break_even_stop_rate=pstar,
                day_bootstrap=grouped_interval(clean), symbol_day_bootstrap=grouped_interval(clean, "symbol_day"))


def cohort_key(c):
    return json.dumps(dict(profile=c["profile"], config_id=c.get("config_id")), sort_keys=True)


def scorecard(events, scans, since, until):
    done, unfinished, quality = reconstruct(events, scans, since, until)
    tables = {}
    for label, group in (("side", lambda c: c["side"]), ("day", lambda c: c["t0"][:10]),
                         ("symbol", lambda c: c["symbol"]),
                         ("profile", cohort_key),
                         ("atr", lambda c: "missing" if c["scan_atr_pct"] is None else "<0.50" if c["scan_atr_pct"] < .5 else ">=0.50"),
                         ("phase_agreement", lambda c: "missing" if not c["phase_det"] else "agree" if c["phase"] == c["phase_det"] else "disagree")):
        groups = defaultdict(list)
        for c in done: groups[group(c)].append(c)
        tables[label] = {k: summary(v) for k, v in sorted(groups.items())}
    accounting = dict(completed_net_usdt=sum(c["net"] for c in done), unresolved_recorded_net_usdt=sum(c["net"] for c in unfinished),
                      known_campaign_fill_net_usdt=sum(c["net"] for c in done + unfinished),
                      note="Only recorded fills of entries in this window; not full account P&L. Unresolved campaigns are NOT wins or zero-loss trades.")
    return dict(since=since, until=until, summary=summary(done), tables=tables, quality=quality, accounting=accounting,
                unfinished=unfinished, campaigns=done,
                limitations=["Development sample, already observed; no out-of-sample profit claim.",
                             "Historical profile is the preceding completed scan, not an exact entry snapshot.",
                             "Entry wallet is the last SIZING observation until CAMPAIGN_OPEN exists; transfers in between can distort returns.",
                             "Binomial upper bound assumes IID campaigns; clustered pump episodes violate this assumption.",
                             "Bootstrap is descriptive with few days; it cannot invent unobserved crash losses.",
                             "Completed-only statistics can have informative censoring; incomplete/open campaigns are disclosed separately and block promotion.",
                             "Fees included, rebates excluded; funding is not in the engine FILL ledger.",
                             "Extra taker-cost stress keeps fills fixed; policy counterfactuals require engine replay."])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", default=os.path.join(ROOT, "logs", "events.jsonl"))
    ap.add_argument("--scans", default=os.path.join(ROOT, "logs", "hunt-history.jsonl"))
    ap.add_argument("--since", default="2026-09-03 00:00:00")
    ap.add_argument("--until", default=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    ap.add_argument("--day")
    ap.add_argument("--output")
    args = ap.parse_args()
    if args.day:
        start = dt.datetime.strptime(args.day, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
        args.since = start.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        args.until = (start + dt.timedelta(days=1, seconds=-1)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    events, bad_e = records(args.events, args.until)
    scans, bad_s = records(args.scans, args.until)
    result = scorecard(events, scans, args.since, args.until)
    result["quality"].update(event_bad_lines=bad_e, scan_bad_lines=bad_s, event_rows=len(events), scan_rows=len(scans),
                             bad_lines_scope="whole files at read time; undated fragments may be outside the selected time window")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh: json.dump(result, fh, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({k: result[k] for k in ("since", "until", "accounting", "quality", "summary")}, ensure_ascii=False, indent=2, allow_nan=False))
    print("Research only: profiles, clustered episodes, missing funding and unobserved tails prevent a profit guarantee.")


if __name__ == "__main__": main()
