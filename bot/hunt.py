"""Pump-coin hunting pipeline — the SIDE pipeline (2026-09-03, user's experiment "until $1000"): one book, one side, the whole wallet,
the side chosen by the coin's lifecycle PHASE (bot/whale.py).
  python -m bot.supervise hunt      (python -m bot.hunt [--once] [--dry])

The basket selector (bot/scan.py + bot/select.py) is untouched and stays the contract; this module inherits its skeleton — scan ->
record every scan (logs/hunt.json, logs/hunt-history.jsonl) -> verdict on `confirm` consecutive scans -> params.json["books"] ->
wind-down -> flat -> drop -> the next coin. Two writers of `books` must never run at once: `hunt.on` = 1 makes this job the owner
(bot.select must be stopped; the job refuses to write while logs/select.pid names a LIVE select job — `pid_alive`, a pid number alone
is not evidence), `hunt.on` = 0 keeps it a report-only scanner.

THEORY (CONCEPT 실험 모드, 세력대항마): an operator runs a coin through phases and the phase decides our side —
  markup   -> LONG book: the cycle buys the shakeouts' deceleration (the sweep-and-reclaim) and trims into the pops;
  climax   -> the long stops adding (wind_down) and leaves at the next stall; nothing opens;
  markdown -> SHORT book: the cycle sells the bounces' deceleration and buys back the drops' stall;
  squeeze  -> the short leaves (late shorts are the operator's next meal); dead -> leave the coin; quiet/unknown -> nothing opens.
Footprints and thresholds: bot/whale.py (stage 1: candles + ticker; stage 2: CVD / OI / funding series and the fingerprint tables).
Common vetoes: 24h volume, ATR(1m) band the 1m rules were tuned in, contract leverage, churn; funding must not tax our side.
A phase exit does not start a cooldown (the same coin flips long -> short on the same scan it goes flat), and neither does the coin
merely going still (ATR floor) — an episode death or a coin that cooled off its own heat does.
Rank among the eligible = 1h two-way path (churn) — the order for the empty slot, never a reason to replace a holding.
Events: HUNT (every scan) in logs/events.jsonl; HUNT_ADD / HUNT_WIND_DOWN / HUNT_DROP / HUNT_BLOCKED also in logs/alerts.jsonl.
State (streaks, cooldowns, the held coin's side / peak volume / climax high / exit reason) in logs/hunt-state.json."""
import json, os, statistics, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import Bitget
from bot.scan import PRODUCT
from bot.signal import wilder_atr
from bot.whale import footprints, phase as whale_phase, longer_history, pre_qualifies, WHALE
from bot.select import log, read_json, write_json, ev, flats_now, recent_engines, CHANNELS
from bot.ws import load_params, PARAMS, load_states

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
HUNT = dict(on=0,                # 1: this job owns params.books (bot.select stopped); 0: report only, never writes params or state
            every_min=10, confirm=2, exit_confirm=1, long_on=1, short_on=1,   # opening risk waits `confirm` scans; leaving is fast (`exit_confirm`, CONCEPT: the
            #                                                                   risk-opening side bears the higher bar). Scan often — the churn we eat is minutes-scale
            quiet_frac=0.5,      # leave when the last-24h two-way path falls under this share of the peak seen while held: the action left THIS coin, chase a hotter one
            require_spot=1,      # a candidate must have a SPOT market (Bitget or Binance): a perp-only pump is a pure liquidation harvest that can vanish in an hour
            #                      (AKE, USELESS: no spot anywhere; 강고양이 picked STO over NOM for its spot liquidity; user approved 2026-09-03)
            blowoff_atr=8.0, blowoff_frac=0.5,   # written onto LONG hunt books: half the position rests at avg + 8 x ATR15 (strat.blowoff_*; first values)
            strat={},            # the track's RISK PROFILE, written whole onto every hunt book (strat keys: unit_frac, cap_frac, daily_loss_frac, notional_frac,
            #                      max_stops_day, lever, cap_min_atr, stop_lock_atr, stop_trail_atr ...). The common params.strat stays the basket's contract — the two tracks never share numbers
            min_vol=1e7,         # 24h quote volume floor (fills and footprint at this wallet)
            universe=1e7,        # the volume floor for pulling daily candles
            min_ratio=4.0,       # 24h volume over the median of the prior 7 UTC days: an episode (a fresh listing reads 99)
            min_twoway=15.0,     # 1h two-way path over the last 24h, %/day (TRUMP 20-36 on its good days, 12 on its dead one)
            min_atr=0.30, max_atr=1.2,   # ATR(1m) % band the 1m rules were tuned in / gappy prints above (USELESS 1.49). The floor is
            #                              where the cliff is (2026-09-04, 1758 rows): the next hour's movement is flat at ~2.5%/h above
            #                              0.30 and falls through it (0.25-0.30 1.68%/h, 0.20-0.25 1.39, 0.15-0.20 1.32). It costs 8% of
            #                              candidate rows (eligible ATR1m p10 = 0.31, p50 = 0.50). Was 0.15 (user: "문턱 좀 가까이 붙여")
            exit_atr_min=0.25,   # a HELD coin whose ATR(1m) falls under this has stopped moving at the scale we trade: wind down (no market
            #                      dump). Deliberately WELL UNDER the entry floor — entry and exit are different questions and the gap is
            #                      hysteresis, not an oversight. At 0.30 (= the entry floor, 2026-09-04 first try) a single scan below it
            #                      ended 69% of campaigns at a 1.7h median and cut the path traversed while held — the fuel a cycle burns —
            #                      from 16.5% to 7.1%. 0.15 restored it (still 12%, fuel 17.6%). Between 0.15 and 0.30 the tape cannot tell:
            #                      NONE of them ever fired on the four books we actually held (lowest ATR1m while held: AKE 0.806, EGLD 0.379,
            #                      UAI 0.356, MUBARAK 0.306), so the choice is how much room to leave under that 0.306 — 0.25 leaves 18%.
            #                      The CEILING stays an entry veto only: a held coin's ATR exploding is the pump itself (RULES).
            min_fund=-0.05,      # funding %/8h floor for a short (negative = shorts pay; T -0.29 would tax a short 0.9%/day)
            max_fund=0.3,        # funding %/8h ceiling for a long (longs crowded and paying)
            min_lever=10,        # the contract must allow at least this leverage (AKE max 10)
            exit_twoway=0.0,     # OFF (2026-09-04): this floor was inverted at its own threshold — rows under 8 moved MORE in the next
            #                      hour (median 1.32%/h, n=136) than the 8-15 rows it kept (0.89%/h, n=231), and twoway24 is not monotone
            #                      anywhere. The two questions it was standing in for are covered better: "the tape is gone" by vol/dead,
            #                      "the coin stopped moving" by exit_atr_min. > 0 turns it back on.
            ai_read=0,           # 1: 국면을 AI 세션(`codex exec`)이 읽고 결정론 판독(`whale.phase`)은 phase_det/side_det 로 그림자가 된다
            #                      (사용자 결정 2026-09-04). 자격 게이트는 AI 가 못 건드린다 — 대체하는 것은 국면과 방향뿐이다.
            #                      실패하면 결정론으로 폴백한다. 판정은 며칠 뒤 `bot.campaigns`: 불일치한 행에서 밀려난 쪽이 옳았나.
            ai_cmd="codex", ai_model="", ai_effort="", ai_timeout_s=240,
            cooldown_h=24, exclude=["BTCUSDT"], record_top=3,
            min_hours=6)         # closed 1H bars a coin needs to be read (a listing a few hours old)
BOOK_KEYS = ("wallet_frac", "sides", "hunt", "wind_down", "exit", "blowoff_atr", "blowoff_frac")   # a hunt book = these + the risk profile (hunt.strat), nothing else

def _pct(a, b): return (a / b - 1) * 100 if b else 0.0

def measures(hours, minutes, bars15, days, ticker, held=None):
    """The shape from candles: whale.footprints (run / off / churn / structure / phase inputs) plus ATR(1m) %."""
    f = footprints(hours, bars15, days, ticker, held)
    f["qv_shape"] = f.pop("qv")                                  # the volume of the venue the shape came from (Binance for a young Bitget listing);
    atr1 = wilder_atr(minutes[-100:])                             # r["qv"] stays Bitget's 24h volume (the toll is paid there)
    f["atr_pct"] = round(atr1 / f["px"] * 100, 3) if atr1 else None
    f["phase"], f["votes"] = whale_phase(f)
    f["side"] = "long" if f["phase"] == "markup" else "short" if f["phase"] == "markdown" else None
    return f

def flags_of(r, hunt):
    """Entry vetoes (a flagged coin is not a candidate whatever its churn): the phase names the side, the rest is the toll."""
    f = []
    if r["qv"] < hunt["min_vol"]: f.append(f"vol{r['qv'] / 1e6:.0f}M")
    if r.get("atr_pct") is None or r["atr_pct"] > hunt["max_atr"] or r["atr_pct"] < hunt["min_atr"]: f.append(f"atr{r.get('atr_pct')}")
    if (r.get("lever_max") or 0) < hunt["min_lever"]: f.append(f"lever{r.get('lever_max')}")
    if (r.get("twoway24") or 0) < hunt["min_twoway"]: f.append(f"twoway{r.get('twoway24')}")
    if hunt.get("require_spot") and not r.get("spot"): f.append("nospot")
    ph = r.get("phase")
    if ph == "markup":
        if not hunt["long_on"]: f.append("long_off")
        if r["fund"] is not None and r["fund"] > hunt["max_fund"]: f.append(f"fund{r['fund']:+.2f}%")
    elif ph == "markdown":
        if not hunt["short_on"]: f.append("short_off")
        if r["fund"] is not None and r["fund"] < hunt["min_fund"]: f.append(f"fund{r['fund']:+.2f}%")
    else: f.append(f"phase:{ph}")
    return f

def exit_flags(r, held, hunt):
    """Why a held coin leaves: the phase turned against its side, the episode died, it stopped moving, funding taxes it, or the tape
    went gappy. The movement question is ATR(1m) — the churn measures are 24h windows that move once an hour (NEXT 17f)."""
    f = []; side = held.get("side") or "short"; ph = r.get("phase")
    if r["qv"] < hunt["min_vol"]: f.append(f"vol{r['qv'] / 1e6:.0f}M")
    if r.get("dead"): f.append("dead")
    atr = r.get("atr_pct")                                          # absent = not evidence (an unread shape keeps the book)
    if hunt.get("exit_atr_min") and atr is not None and atr < hunt["exit_atr_min"]: f.append(f"still{atr}")
    tw = r.get("twoway24") or 0.0; tw_peak = held.get("tw_peak") or 0.0
    if tw < hunt["exit_twoway"]: f.append(f"flat{tw}")                                          # absolute floor: no churn left to trade
    elif tw_peak >= hunt["min_twoway"] and tw < hunt["quiet_frac"] * tw_peak: f.append(f"quiet{tw:.0f}/{tw_peak:.0f}")   # the coin cooled off its own hot: chase
    # 판독기가 우리 편이 아니라고 **적극적으로** 말하면 담기를 멈춘다(`hold:`, wind_down 만) — 새로 열지 않을 국면에서 계속 담는 것은
    # CONCEPT-B "리스크를 여는 쪽이 닫는 쪽보다 높은 기준을 진다" 와 어긋난다(담기도 여는 것이다). `unknown` 은 여기 없다: 말을 못 하는
    # 것은 근거가 아니고(판독의 36%), 그것으로 나가는 변형은 측정에서 기각됐다(NEXT 19a). distribution 은 AI 만 내는 라벨이라 전방 검증이
    # 없으므로 전량 청산까지 가지 않는다 — 승격 조건은 `bot.campaigns` 에서 그 행의 전방 4h 꼬리가 markup(p10 −8.3%)보다 나쁠 때.
    if ph in ("dead", "quiet") or (side == "long" and ph == "distribution"): f.append(f"hold:{ph}")   # 숏에게 distribution 은 우리 편이다(고점이 팔리는 중)
    if side == "long":
        if ph in ("climax", "markdown", "squeeze"): f.append(f"phase:{ph}")
        elif (r.get("off") or 0) >= WHALE["far_off"] and (r.get("off_close") or 0) >= WHALE["far_close"]: f.append(f"far{r.get('off')}")   # far under the top AND its highest close, whatever the structure reads (a bounce that flips
        if r["fund"] is not None and r["fund"] > hunt["max_fund"]: f.append(f"fund{r['fund']:+.2f}%")   # the 15m read to "long" 70% under the top is not a markup — audit 2026-09-03)
    else:
        if ph in ("markup", "squeeze"): f.append(f"phase:{ph}")
        if held.get("climax") and r["px"] > held["climax"]: f.append("newhigh")
        if r["fund"] is not None and r["fund"] < hunt["min_fund"]: f.append(f"fund{r['fund']:+.2f}%")
    return f                                                      # ATR is an entry question only: a held coin's ATR exploding is the pump itself

_SPOTS = {"t": 0.0, "map": {}}
def spot_markets(b, log=log):
    """{symbol: "bitget" | "binance"} for USDT pairs with a live spot market, refreshed hourly. Binance is best-effort (unreachable = ignored)."""
    if time.time() - _SPOTS["t"] < 3600 and _SPOTS["map"]: return _SPOTS["map"]
    m = {}
    try:
        import json as _json, urllib.request
        with urllib.request.urlopen("https://api.binance.com/api/v3/exchangeInfo", timeout=10) as r:
            for s in _json.loads(r.read())["symbols"]:
                if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT": m[s["symbol"]] = "binance"
    except Exception as e: log(f"hunt: binance spot list unavailable ({type(e).__name__})")
    try:
        for s in b.get("/api/v2/spot/public/symbols", auth=False):
            if s.get("status") == "online" and s.get("quoteCoin") == "USDT": m[s["symbol"]] = "bitget"
    except Exception as e: log(f"hunt: bitget spot list unavailable ({type(e).__name__})")
    if m: _SPOTS.update(t=time.time(), map=m)
    return m

def market(b, sym="BTCUSDT", log=log):
    """The tape every alt moves with, RECORDED ONLY (CONCEPT 트랙 B, 2026-09-04): a cascade's deceleration is somebody else's
    liquidation, not the operator's shakeout, and `phase` cannot tell them apart — so every scan writes the market's move into
    logs/hunt-history.jsonl and NOTHING reads it. The threshold waits for the nightly cross-section (NEXT 17b) to answer whether
    the market's drawdown predicts THIS coin's tail. Moves are vs the close of N closed 1H bars ago; dd4 = under the 4h high."""
    try: hours = b.candles(sym, "1H", limit=30)[:-1]; m15 = b.candles(sym, "15m", limit=20)[:-1]
    except Exception as e: log(f"hunt: {sym} candles failed ({type(e).__name__})"); return None
    if len(hours) < 4 or len(m15) < 2: return None
    px = m15[-1]["c"]
    return dict(sym=sym, px=px, m15=round(_pct(px, m15[-2]["c"]), 2), h1=round(_pct(px, hours[-1]["c"]), 2),
                h4=round(_pct(px, hours[-4]["c"]), 2), h24=round(_pct(px, hours[-24]["c"]), 2) if len(hours) >= 24 else None,
                dd4=round(_pct(px, max(x["h"] for x in hours[-4:])), 2))

AI_PHASES = ("markup", "distribution", "climax", "markdown", "squeeze", "dead", "quiet", "unknown")
AI_PROMPT = """You read pump-coin lifecycle phases for a counter-operator trading bot. Below is one scan: every contract whose shape
was read, as the numeric footprint the bot extracts. Judge each coin's CURRENT PHASE the way an experienced chart reader would.

run = % rise of the episode, off = % under the 48h high, off_close = % under the highest CLOSE, age_h = hours since the episode
began, twoway24/2h/1h = two-way path (churn), ratio = 24h volume / its own 7-day median (99 = fresh listing), ign = last 3h volume
vs the prior 48h median 3h, up3 = 3h price change, atr_pct/atr15_pct = ATR(1m)/ATR(15m) as % of price, upwick = upper-wick share of
the top bar, lower_high = the last swing high failed to exceed the previous one, vmax_at_high = the episode's biggest-volume bar
sits at the high, vmax_share = that bar's share, post_red = red bars after it, exhaustion = rising price on shrinking bodies and dry
volume, leg_down = an unconfirmed lower-low leg, hint15 = 15m structure, fund = funding %/8h, qv = 24h quote volume, dead = volume
collapsed, phase/votes = THE BOT'S OWN READING, shown so that you can disagree.

Phases: markup (the operator is still pushing, new money, near the highs) / distribution (the top is being sold INTO strength:
price flat or grinding up while bodies shrink, upper wicks fatten, highs stop rising) / climax (the blow-off top just printed) /
markdown (the operator is out, price is being marked down) / squeeze (late shorts are the next meal) / dead / quiet / unknown.

The bot's reader has NO distribution phase at all and falls back to `unknown` whenever its 15m zigzag finds no pivot, which is 36%
of its readings. That gap is the reason you are here. Do not copy `phase`; read the footprint yourself.

Answer with ONE line of JSON and nothing else:
{"reads":[{"symbol":"X","phase":"<one of the phases>","conf":0-100,"why":"<=12 words"}]}
Every symbol below must appear exactly once."""

def ai_read(rows, hunt, log=log):
    """국면을 AI 세션이 다시 읽는다(`codex exec`, 사용자 결정 2026-09-04). 결정론 판독은 `phase_det`/`side_det` 로 남아
    그림자가 된다 — 매 스캔 둘 다 `hunt-history` 에 기록되므로 나중에 "밀려난 쪽이 옳았나" 를 전방 가격으로 계산할 수 있다
    (그 계산이 `bot.campaigns`). **자격(통행료 게이트)은 건드리지 않는다** — vol·ATR·레버·현물·펀딩·twoway 는 측정 가능한
    veto 라 그대로다. AI 가 대체하는 것은 국면 판독과 그것이 정하는 방향뿐이다(메모리: 자격과 순위는 다른 질문).
    실패·타임아웃·형식 오류에는 **아무것도 바꾸지 않는다** — 모델이 없다고 매매가 멈추면 안 된다. {} 를 돌려주면 호출자가 결정론을 쓴다."""
    K = ("symbol", "phase", "votes", "run", "off", "off_close", "age_h", "twoway24", "twoway2h", "twoway1h", "ratio", "ign", "up3",
         "atr_pct", "atr15_pct", "upwick", "lower_high", "vmax_at_high", "vmax_share", "post_red", "exhaustion", "leg_down", "hint15",
         "fund", "qv", "new", "dead")
    if not rows: return {}
    body = json.dumps([{k: r.get(k) for k in K} for r in rows], ensure_ascii=False)
    d = os.path.join(LOGS, "ai"); os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "read.json")
    try:
        if os.path.exists(out): os.remove(out)
        cmd = [hunt.get("ai_cmd") or "codex", "exec", "--skip-git-repo-check", "-s", "read-only", "--cd", d, "-o", out]
        if hunt.get("ai_model"): cmd += ["-m", str(hunt["ai_model"])]        # 기본은 ~/.codex/config.toml (gpt-5.6-sol / xhigh).
        if hunt.get("ai_effort"): cmd += ["-c", f"model_reasoning_effort={hunt['ai_effort']}"]   # luna-max 등은 API 키 인증이라야 뜬다
        p = subprocess.run(cmd + ["-"],
                           input=AI_PROMPT + chr(10) + body, capture_output=True, text=True, errors="replace",
                           timeout=float(hunt.get("ai_timeout_s") or 240))
        with open(out, encoding="utf-8") as fh: raw = fh.read()
    except Exception as e:
        log(f"hunt: ai_read failed ({type(e).__name__}: {str(e)[:80]}) - keeping the deterministic read"); return {}
    i, j = raw.find("{"), raw.rfind("}")
    try: got = json.loads(raw[i:j + 1])["reads"]
    except Exception as e:
        log(f"hunt: ai_read unparsable ({type(e).__name__}); rc={p.returncode} {raw[:120]!r}"); return {}
    want = {r["symbol"] for r in rows}
    reads = {x["symbol"]: (x["phase"], x.get("conf"), str(x.get("why") or "")[:60])
             for x in got if isinstance(x, dict) and x.get("symbol") in want and x.get("phase") in AI_PHASES}
    log(f"hunt: ai_read {len(reads)}/{len(rows)} coins, {sum(1 for r in rows if reads.get(r['symbol'], (None,))[0] != r.get('phase'))} disagree")
    return reads

def scan(hunt, held=(), st=None, b=None, log=log):
    """Rows for every contract with 24h volume >= `universe` (daily candles for the ratio), the full shape for those with an episode
    (ratio >= min_ratio, a fresh listing counts) or held. Public REST only. Sorted: eligible by two-way path (desc), then by ratio."""
    b = b or Bitget("", "", ""); t0 = time.time(); hs = (st or {}).get("held") or {}
    contracts = {c["symbol"]: c for c in b.get("/api/v2/mix/market/contracts", auth=False, productType=PRODUCT) if c.get("symbolStatus") == "normal"}
    tickers = {t["symbol"]: t for t in b.get("/api/v2/mix/market/tickers", auth=False, productType=PRODUCT)}
    spots = spot_markets(b, log)
    pool = [s for s, t in tickers.items() if s in contracts and s not in hunt["exclude"] and (float(t.get("quoteVolume") or 0) >= hunt["universe"] or s in held)]
    log(f"hunt: {len(pool)} contracts with 24h volume >= {hunt['universe'] / 1e6:.0f}M; daily candles ...")
    rows, dailies = [], {}
    for s in pool:
        t = tickers[s]; px = float(t["lastPr"]); qv = float(t.get("quoteVolume") or 0)
        try: days = b.candles(s, "1D", limit=10)[:-1]
        except Exception as e: log(f"  {s}: 1D failed {type(e).__name__}"); continue
        dailies[s] = days; prior = [d["qv"] for d in days[-7:]]
        base = statistics.median(prior) if len(prior) >= 3 else 0.0
        rows.append(dict(symbol=s, px=px, qv=qv, base7=base, ratio=99.0 if len(prior) < 3 else round(qv / base, 1), new=len(prior) < 3, spot=spots.get(s),
                         chg24=round(float(t.get("change24h") or 0) * 100, 1), fund=round(float(t.get("fundingRate") or 0) * 100, 3),
                         oi=float(t.get("holdingAmount") or 0) * px, spread_bp=round(_pct(float(t.get("askPr") or px), float(t.get("bidPr") or px)) * 100, 1),
                         lever_max=int(float(contracts[s].get("maxLever") or 0)), min_notional=float(contracts[s].get("minTradeNum") or 0) * px,
                         tick_pct=round(float(contracts[s].get("priceEndStep") or 1) * 10 ** -int(contracts[s].get("pricePlace") or 0) / px * 100, 4)))
    deep = [r for r in rows if r["ratio"] >= hunt["min_ratio"] or r["symbol"] in held]
    for r in [r for r in rows if r not in deep]:               # under the ratio gate: one 1H call each for the hourly footprint — an ignition or a big run is an
        s = r["symbol"]                                        # episode the 24h ratio cannot see (SIREN 0.8x at its ignition; audit 2026-09-03); the rest keep ign / run for the tables
        try:
            hours = b.candles(s, "1H", limit=120)[:-1]
            if len(hours) < int(hunt["min_hours"]): continue
            f = footprints(hours, [], dailies[s], dict(qv=r["qv"], fund=r["fund"]), None)
        except Exception as e: log(f"  {s}: 1H failed {type(e).__name__}: {str(e)[:60]}"); continue
        r.update({k: f[k] for k in ("high48", "run", "off", "off_close", "age_h", "twoway24", "ign", "up3")})
        if pre_qualifies(f): r["pre"] = "big_run" if f["run"] >= WHALE["big_run"] else "ignition"; deep.append(r)
    log(f"hunt: {len(deep)} with an episode (ratio >= {hunt['min_ratio']}x, new, an ignition or a big run) or held; shapes ...")
    for r in deep:
        s = r["symbol"]
        try:
            hours = b.candles(s, "1H", limit=120)[:-1]; minutes = b.candles(s, "1m", limit=200)[:-1]; bars15 = b.candles(s, "15m", limit=200)[:-1]
            hours, bars15, days, src = longer_history(s, hours, bars15, dailies[s])          # a listing hours old on Bitget: Binance's chart for the shape
            if len(hours) < int(hunt["min_hours"]) or len(bars15) < 20: raise ValueError(f"too few bars ({len(hours)}h, {len(bars15)}x15m)")
            r.update(measures(hours, minutes, bars15, days, dict(qv=None if src == "binance" else r["qv"], fund=r["fund"]), hs.get(s))); r["src"] = src
        except Exception as e:
            log(f"  {s}: shape failed {type(e).__name__}: {str(e)[:60]}"); r.update(phase="unread", votes=[], side=None, twoway24=None, run=None, off=None, high48=None, atr_pct=None, hint15=None, dead=False)
        r["flags"] = flags_of(r, hunt)
    if hunt.get("ai_read") and deep:
        for sym, (ph, conf, why) in ai_read([r for r in deep if r.get("phase") not in (None, "unread")], hunt, log).items():
            r = next(x for x in deep if x["symbol"] == sym)
            r["phase_det"], r["side_det"] = r.get("phase"), r.get("side")      # 그림자: 결정론이 무엇이라 했는지 매 스캔 남는다
            r["phase"], r["ai_conf"], r["ai_why"] = ph, conf, why
            r["side"] = "long" if ph == "markup" else "short" if ph == "markdown" else None
            r["flags"] = flags_of(r, hunt)                                     # 통행료 게이트는 새 국면 위에서 다시 — veto 는 AI 가 못 뒤집는다
    for r in rows:
        if "flags" not in r:
            for k in ("twoway24", "run", "off", "high48"): r.setdefault(k, None)      # the hourly pre-read's numbers stay for the evidence tables
            r.update(phase="shallow", votes=[], side=None, atr_pct=None, hint15=None, dead=False, flags=[f"ratio{r['ratio']:.1f}x"])
    ok = sorted([r for r in rows if not r["flags"]], key=lambda r: -(r["twoway24"] or 0))
    rest = sorted([r for r in rows if r["flags"]], key=lambda r: -r["ratio"])
    log(f"hunt: {len(ok)} eligible, {len(rest)} flagged, {time.time() - t0:.0f}s")
    return ok + rest

def table(rows, n=15):
    out = [f"{'symbol':12}{'px':>10}{'qv24':>7}{'ratio':>7}{'chg24':>7}{'run':>6}{'off':>6}{'2way':>6}{'atr%':>6}{'ign':>5}{'fund':>7}{'lev':>4}{'spot':>5}{'hint':>6}  {'phase':9}{'side':6}flags | votes"]
    for r in rows[:n]:
        g = lambda k, d=0: r[k] if r.get(k) is not None else d
        out.append(f"{r['symbol']:12}{r['px']:>10.5g}{r['qv'] / 1e6:>6.0f}M{r['ratio']:>6.1f}x{r['chg24']:>+6.1f}%{g('run'):>6.0f}{g('off'):>6.1f}{g('twoway24'):>6.0f}"
                   f"{g('atr_pct'):>6.2f}{g('ign'):>5.1f}{r['fund']:>+7.3f}{r.get('lever_max') or 0:>4}{(r.get('spot') or '-')[:4]:>5}{str(r.get('hint15')):>6}  {str(r.get('phase')):9}{str(r.get('side')):6}{' '.join(r['flags'])} | {' '.join(r.get('votes') or [])}")
    return "\n".join(out)

def verdict(rows, books, hunt, st, now):
    """What this scan says. books = params.books. Returns dict(refuse|wind|add|top|cur); add/top = (symbol, side). A non-hunt book
    in `books` (a basket, a hand book) makes the job refuse: it never rewrites a basket. Streaks live in st (`streak`, `xstreak`)."""
    by = {r["symbol"]: r for r in rows}
    held = [s for s, bk in books.items() if bk.get("hunt")]
    other = [s for s in books if s not in held]
    if other: return dict(refuse=f"books holds non-hunt symbols {other}: stop this job or empty the basket first", cur=None, wind=None, add=None, top=None)
    if len(held) > 1: return dict(refuse=f"more than one hunt book {held}", cur=None, wind=None, add=None, top=None)
    cur = held[0] if held else None
    wind = resume = None
    if cur and books[cur].get("wind_down") and cur in by and not _leave_coin((st.get("held", {}).get(cur) or {}).get("exit", "")):
        # a phase-flip exit is undone when the read comes back to our side for `confirm` scans before the book is flat (a single bad
        # 15m close must not dump a good book: exit_confirm is 1 — audit 2026-09-03)
        r = by[cur]; mine = (books[cur].get("sides") or ["short"])[0]
        back = r.get("side") == mine and not exit_flags(r, st.get("held", {}).get(cur, {}), hunt)
        st.setdefault("rstreak", {})[cur] = st.get("rstreak", {}).get(cur, 0) + 1 if back else 0
        if back and st["rstreak"][cur] >= int(hunt["confirm"]): resume = cur; st["rstreak"][cur] = 0
    if cur and not books[cur].get("wind_down"):
        r = by.get(cur)
        if r and r.get("phase") not in ("unread", "shallow"):        # absent or unread = not evidence: keep
            hs = st.setdefault("held", {}).setdefault(cur, {})
            hs["peak"] = max(hs.get("peak") or 0.0, r.get("qv_shape") or r["qv"]); hs.setdefault("side", (books[cur].get("sides") or ["short"])[0])
            hs["tw_peak"] = max(hs.get("tw_peak") or 0.0, r.get("twoway24") or 0.0)   # the churn when this coin was hot: leaving reads against it (quiet_frac)
            xf = exit_flags(r, hs, hunt)
            st.setdefault("xstreak", {})[cur] = st.get("xstreak", {}).get(cur, 0) + 1 if xf else 0
            if xf and st["xstreak"][cur] >= int(hunt["exit_confirm"]): wind = (cur, ",".join(xf)); hs["exit"] = wind[1]
    cool = st.get("cool") or {}
    def free(r):   # not held, or the leaving book itself on the OTHER side (the lifecycle flip: a long wound down at the climax comes back short) —
        bk = books.get(r["symbol"])   # unless it left for quiet / dead: that coin is cooling, and holding the top slot would freeze the streak, the drop and the cooldown (deadlock, audit 2026-09-03)
        if bk is None: return True
        if not bk.get("wind_down") or _leave_coin((st.get("held", {}).get(r["symbol"]) or {}).get("exit", "")): return False
        return r["side"] != (bk.get("sides") or [None])[0]
    cands = [(r["symbol"], r["side"]) for r in rows if not r["flags"] and r.get("side") and free(r) and cool.get(r["symbol"], 0) <= now]
    top = cands[0] if cands else None
    key = f"{top[0]}:{top[1]}" if top else None
    st["streak"] = {key: (st.get("streak") or {}).get(key, 0) + 1} if key else {}
    slot_open = cur is None or books[cur].get("wind_down") or wind is not None
    add = top if top and slot_open and st["streak"][key] >= int(hunt["confirm"]) else None
    if cur and not slot_open and top and top[0] == cur: add = None
    if resume: add = None                                                   # the book stays: nothing replaces it this scan
    return dict(refuse=None, cur=cur, wind=wind, add=add, top=top, resume=resume)

def _illiquid(why): return any(k in (why or "") for k in ("dead", "vol", "still", "hold:"))   # volume gone, or the coin stopped moving: dumping into a book with nothing in it
#                                                                                      hurts and there is no hurry — leave gently (stalls above cost, or the cap)
def _leave_coin(why): return any(k in (why or "") for k in ("dead", "vol", "flat", "quiet"))   # the episode is over or the coin went quiet: cool down, chase a different one.
#   `still` is deliberately NOT here (2026-09-04): a coin that stopped moving has not ended its episode, it went quiet for an hour — banishing
#   it for cooldown_h on one soft ATR reading is the wrong price for a floor that sits only ~18% under the lowest ATR we have actually held
#   (0.306, MUBARAK). So it leaves gently like an illiquid one, keeps no cooldown, and HUNT_RESUME can undo it if the tape wakes before flat.
#            everything else (a phase flip: climax / markdown / markup / squeeze / newhigh) is a same-coin side change — no cooldown, exit fast into a stall

def apply(p, rows, v, flats, hunt, st, now, recent=()):
    """Bring params.json to the verdict. One live hunt book at a time: the leaving book is dropped only when it is flat AND a
    replacement opens in the same write (books never empties — an empty `books` would send ws.portfolio() to whole-wallet
    strat.symbol on the common sides). The same coin may come back at once on the other side after a phase exit (no cooldown);
    an episode death starts the cooldown. Returns [(action, symbol, detail)]; nothing is written here."""
    by = {r["symbol"]: r for r in rows}; acts = []
    books = p.get("books") or {}; sp = p.setdefault("strat", {})
    if v["wind"]:
        s, why = v["wind"]
        if s in books and not books[s].get("wind_down"):
            books[s]["wind_down"] = 1                                       # no more adds; trims and the stop keep working
            if not _illiquid(why): books[s]["exit"] = 1                     # a phase flip OR the coin gone quiet: the engine sells the whole position into the next stall whatever the cost (leave fast)
            acts.append(("wind", s, why))                                   # (an episode death leaves gently: stalls above cost, or the cap)
    if v.get("resume"):                                                     # a phase-flip exit whose read reverted before the book was flat: undo it (audit 2026-09-03)
        s = v["resume"]
        if s in books and books[s].get("wind_down") and not _leave_coin((st.get("held", {}).get(s) or {}).get("exit", "")):
            books[s].pop("wind_down", None); books[s].pop("exit", None); st.get("held", {}).get(s, {}).pop("exit", None); acts.append(("resume", s, "phase back on our side"))
    leaving = [s for s in books if books[s].get("wind_down")]; live = [s for s in books if not books[s].get("wind_down")]
    add = v["add"]
    cooling = {s for s in leaving if _leave_coin((st.get("held", {}).get(s) or {}).get("exit", ""))}   # a quiet/dead leaver must not come straight back on the other side (audit 2026-09-03)
    if add and add[0] in cooling: add = None
    if add and not live and all(flats.get(s) for s in leaving):
        for s in leaving:
            why = (st.get("held", {}).get(s) or {}).get("exit", "")
            del books[s]
            if _leave_coin(why): st.setdefault("cool", {})[s] = now + float(hunt["cooldown_h"]) * 3600
            st.get("held", {}).pop(s, None); acts.append(("drop", s, f"flat ({why or 'replaced'})"))
        sym, side = add; r = by[sym]
        books[sym] = {"wallet_frac": 1.0, "sides": [side], "hunt": 1, **(hunt.get("strat") or {})}   # the track's risk profile rides on the book, not on params.strat
        if side == "long" and float(hunt.get("blowoff_atr") or 0) > 0:               # the standing blow-off target, long books only (the basket never sees it)
            books[sym].update(blowoff_atr=float(hunt["blowoff_atr"]), blowoff_frac=float(hunt.get("blowoff_frac") or 1.0))
        st.setdefault("held", {})[sym] = dict(peak=r.get("qv_shape") or r["qv"], climax=r.get("high48"), side=side, t=now); st["streak"] = {}
        acts.append(("add", sym, f"{side} phase {r.get('phase')} votes {' '.join(r.get('votes') or [])} ratio {r['ratio']}x run {r.get('run')}% off {r.get('off')}% twoway {r.get('twoway24')} atr {r.get('atr_pct')} fund {r['fund']}"))
    prof = hunt.get("strat") or {}                                          # the risk profile rides on EVERY hunt book, not only on the one being created: a profile edit
    for s, bk in books.items():                                             # reaches the live book on the next scan (the engine hot-reloads; reductions apply at once — RULES
        if not bk.get("hunt"): continue                                     # 사이징). Audit 2026-09-03: the 20:08 normalization reached the EGLD book only by hand
        diff = {k: v for k, v in prof.items() if bk.get(k) != v}; stale = [k for k in bk if k not in BOOK_KEYS and k not in prof]
        if diff or stale:
            bk.update(diff)
            for k in stale: bk.pop(k)
            acts.append(("profile", s, " ".join([f"{k}={v}" for k, v in diff.items()] + [f"-{k}" for k in stale])))
    if books: p["books"] = books
    held = list(books)
    if held and sp.get("symbol") not in held: sp["symbol"] = held[0]
    if held: sp["side"] = (books[held[0]].get("sides") or [sp.get("side")])[0]
    rec = {s: CHANNELS for s in held + [x for x in recent if x not in held]}
    for r in [r for r in rows if not r["flags"] and r["symbol"] not in rec][:int(hunt["record_top"])]: rec[r["symbol"]] = CHANNELS
    rec["BTCUSDT"] = ["candle1m"]; p["record"] = rec
    return acts

ALERT = {"add": "HUNT_ADD", "wind": "HUNT_WIND_DOWN", "drop": "HUNT_DROP", "resume": "HUNT_RESUME", "profile": "HUNT_PROFILE"}

def pid_alive(path, match=("bot.supervise select", "bot.select")):
    """Why this job must not write params.books, or "" when it may. The pid must be alive AND its command line must be one of
    `match`: a Windows pid is reused, and `tasklist /FI "PID eq N"` only says somebody holds that number — bot.select stopped
    2026-09-02 23:52, hunt wrote books all the next day, and a stranger inheriting its pid raised HUNT_BLOCKED at 09-04 00:16.
    Both forms are matched because logs/select.pid holds the SUPERVISOR's pid (`bot.supervise select`, supervise.main writes its
    own), not the `bot.select` child's. An unreadable command line blocks: refusing to write is the recoverable error, two
    writers of params.books is not."""
    try:
        with open(path) as f: pid = int(f.read().strip())
    except Exception: return ""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                             capture_output=True, text=True, errors="replace", timeout=60).stdout
    except Exception as e:
        return f"pid {pid} in {os.path.basename(path)} unreadable ({type(e).__name__}): blocking as if {match[0]} were alive"
    hit = next((m for m in match if m in out), "")
    return f"{hit} is running (pid {pid})" if hit else ""

def main():
    once, dry = "--once" in sys.argv, "--dry" in sys.argv
    while True:
        p = load_params() or {}; hunt = {**HUNT, **(p.get("hunt") or {})}
        books = p.get("books") or {}; held = [s for s, bk in books.items() if bk.get("hunt")]
        t0 = time.time(); st = read_json(os.path.join(LOGS, "hunt-state.json"), {}); b = Bitget("", "", "")
        try: rows = scan(hunt, held=tuple(held), st=st, b=b)
        except Exception as e: log(f"hunt scan failed: {type(e).__name__}: {e}"); rows = None
        if rows:
            os.makedirs(LOGS, exist_ok=True)
            rec = dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), hunt={k: v for k, v in hunt.items() if k != "exclude"}, whale=WHALE,
                       market=market(b), rows=rows)          # 기록만 — 아무도 읽지 않는다(CONCEPT 트랙 B, NEXT 17b)
            write_json(os.path.join(LOGS, "hunt.json"), rec)
            with open(os.path.join(LOGS, "hunt-history.jsonl"), "a", encoding="utf-8") as f: f.write(json.dumps(rec) + "\n")
            log("\n" + table(rows))
            now = time.time(); flats = flats_now()
            v = verdict(rows, books, hunt, st, now)
            owner = bool(hunt.get("on")) and not dry
            ev("HUNT", on=int(bool(hunt.get("on"))), dry=dry, held=held, flat={s: flats.get(s) for s in held}, cur=v["cur"], wind=v["wind"], add=v["add"], top=v["top"],
               refuse=v["refuse"], streak=st.get("streak"), xstreak={s: st.get("xstreak", {}).get(s) for s in held},
               phases={r["symbol"]: [r.get("phase"), r.get("votes")] for r in rows if r.get("phase") not in ("shallow", None)}, took_s=int(now - t0),
               rows=[[r["symbol"], r.get("phase"), r.get("side"), r["ratio"], r.get("run"), r.get("off"), r.get("twoway24"), r.get("atr_pct"), r["fund"], r.get("hint15"), " ".join(r["flags"])] for r in rows[:8]])
            if v["refuse"]: log(f"hunt: {v['refuse']}")
            elif owner:
                blocked = pid_alive(os.path.join(LOGS, "select.pid"))
                if blocked:
                    ev("HUNT_BLOCKED", alert=True, why=f"{blocked}: two writers of params.books — stop it (or set hunt.on 0)")
                else:
                    before = json.dumps(p, sort_keys=True)
                    acts = apply(p, rows, v, flats, hunt, st, now, recent=recent_engines(load_states(), now))
                    if json.dumps(p, sort_keys=True) != before: write_json(PARAMS, p, indent=2)
                    for kind, sym, detail in acts:
                        ev(ALERT[kind], alert=True, symbol=sym, why=detail, books={s: (b.get("sides") or [None])[0] for s, b in (p.get("books") or {}).items()})
                    write_json(os.path.join(LOGS, "hunt-state.json"), st)
            else:
                log(f"hunt: report only ({'--dry' if dry else 'hunt.on=0'}); would: wind={v['wind']} add={v['add']} top={v['top']}")
        if once: break
        time.sleep(max(60, float(hunt["every_min"]) * 60 - (time.time() - t0)))

if __name__ == "__main__":
    main()
