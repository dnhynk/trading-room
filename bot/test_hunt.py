"""Invariants of the pump-coin hunting side pipeline (bot/hunt.py): phase names the side, the toll vetoes, a book per confirmed
eligible coin under the capital pool (one book without it), phase exits rotate without cooldown, episode deaths cool down.  python -m unittest bot.test_hunt"""
import os, shutil, tempfile, types, unittest
from bot import hunt
from bot.hunt import flags_of, exit_flags, verdict, apply, pid_alive, HUNT

def row(sym, phase="markdown", **kw):
    r = dict(symbol=sym, px=129.0, qv=4e7, base7=4e6, ratio=10.0, new=False, chg24=-5.0, fund=0.01, oi=1e7, spread_bp=2.0, lever_max=25, min_notional=1.0,
             tick_pct=0.001, twoway24=30.0, net24=-3.0, run=50.0, off=14.0, off_close=14.0, high48=150.2, atr_pct=0.6, atr15_pct=2.0, hint15="short", dead=False, spot="bitget",
             phase=phase, votes=[], flags=[])
    r.update(kw); r["side"] = "long" if r["phase"] == "markup" else "short" if r["phase"] == "markdown" else None
    r["flags"] = list(kw.get("flags", flags_of(r, HUNT))); return r

class Flags(unittest.TestCase):
    def test_the_phase_names_the_side_and_only_markup_or_markdown_is_a_candidate(self):
        self.assertEqual(flags_of(row("A", "markdown"), HUNT), []); self.assertEqual(row("A", "markdown")["side"], "short")
        self.assertEqual(flags_of(row("A", "markup", hint15="long", off=2.0), HUNT), []); self.assertEqual(row("A", "markup")["side"], "long")
        for ph in ("climax", "squeeze", "dead", "quiet", "unknown", "unread", "shallow"):
            self.assertEqual(flags_of(row("A", ph), HUNT), [f"phase:{ph}"], ph)

    def test_the_toll_vetoes_fire_alone_and_funding_is_read_per_side(self):
        for kw, tag in ((dict(qv=5e6), "vol"), (dict(atr_pct=1.5), "atr"), (dict(atr_pct=0.1), "atr"), (dict(atr_pct=None), "atr"),
                        (dict(lever_max=5), "lever"), (dict(twoway24=10.0), "twoway"), (dict(fund=-0.3), "fund")):
            f = flags_of(row("A", "markdown", **kw), HUNT); self.assertEqual(len(f), 1, (kw, f)); self.assertTrue(f[0].startswith(tag), (kw, f))
        self.assertEqual(flags_of(row("A", "markdown", fund=0.5), HUNT), [])                      # a short is paid by hot funding
        self.assertTrue(flags_of(row("A", "markup", fund=0.5), HUNT)[0].startswith("fund"))       # a long would pay it
        self.assertEqual(flags_of(row("A", "markup", fund=-0.3), HUNT), [])
        self.assertEqual(flags_of(row("A", "markup"), {**HUNT, "long_on": 0}), ["long_off"])
        self.assertEqual(flags_of(row("A", "markdown"), {**HUNT, "short_on": 0}), ["short_off"])
        self.assertEqual(flags_of(row("A", "markdown", spot=None), HUNT), ["nospot"])                    # a perp-only pump (AKE, USELESS) is not a candidate
        self.assertEqual(flags_of(row("A", "markdown", spot=None), {**HUNT, "require_spot": 0}), [])

    def test_the_lever_gate_reads_the_profiles_leverage(self):
        h = {**HUNT, "strat": {"lever": 20}}
        self.assertEqual(flags_of(row("A", "markdown", lever_max=10), h), ["lever10"])     # BRUSDT 2026-09-04 19:14: admitted at max 10, LEVER_SET 20 refused every 5 min
        self.assertEqual(flags_of(row("A", "markdown", lever_max=20), h), [])
        self.assertEqual(flags_of(row("A", "markdown", lever_max=10), HUNT), [])          # no profile leverage: min_lever alone

    def test_exit_flags_read_the_side_and_the_phase(self):
        short = dict(side="short", peak=1e8, climax=150.2); long_ = dict(side="long", peak=1e8, climax=150.2)
        self.assertEqual(exit_flags(row("A", "markdown"), short, HUNT), [])
        self.assertEqual(exit_flags(row("A", "markup", hint15="long", off=2.0), long_, HUNT), [])
        self.assertEqual(exit_flags(row("A", "climax"), long_, HUNT), ["phase:climax"])          # the long stops at the climax
        self.assertEqual(exit_flags(row("A", "climax"), short, HUNT), [])                         # a short does not care about a climax vote
        self.assertEqual(exit_flags(row("A", "unknown", off=70.0, off_close=31.0, hint15="long"), long_, HUNT), ["far70.0"])   # 70% under the top and 31% under its highest close: the long leaves whatever the structure says
        self.assertEqual(exit_flags(row("A", "unknown", off=30.7, off_close=-10.0, hint15=None), long_, HUNT), [])          # under a spike wick but above every close: a shakeout, the long stays (STO)
        self.assertEqual(exit_flags(row("A", "unknown", off=70.0, hint15="long"), short, HUNT), [])            # a short far under the top is where it earns
        self.assertEqual(exit_flags(row("A", "markup"), short, HUNT), ["phase:markup"])           # a short leaves a relaunch
        self.assertEqual(exit_flags(row("A", "squeeze"), short, HUNT), ["phase:squeeze"])
        self.assertEqual(exit_flags(row("A", "markdown", px=151.0), short, HUNT), ["newhigh"])
        self.assertEqual(exit_flags(row("A", "markdown", dead=True), short, HUNT), ["dead"])
        self.assertEqual(exit_flags(row("A", "markdown", twoway24=5.0), short, HUNT), [])              # the churn floor is off (inverted); movement is ATR's question now
        self.assertEqual(exit_flags(row("A", "markdown", atr_pct=0.2), short, HUNT), ["still0.2"])

class StoppedMoving(unittest.TestCase):
    """ATR(1m) is the movement question at the scale we trade — twoway24's 24h window was the weakest predictor (NEXT 17f)."""
    def test_a_held_coin_under_the_atr_floor_leaves_but_gently(self):
        held = dict(side="long", peak=1e8, climax=150.2)
        self.assertEqual(exit_flags(row("A", "markup", hint15="long", off=2.0, atr_pct=0.24), held, HUNT), ["still0.24"])
        self.assertEqual(exit_flags(row("A", "markup", hint15="long", off=2.0, atr_pct=0.27), held, HUNT), [])   # between the two floors: HELD, not re-entered
        self.assertTrue(flags_of(row("A", "markup", hint15="long", off=2.0, atr_pct=0.27), HUNT))               # ... and it would not be opened here either
        self.assertLess(HUNT["exit_atr_min"], HUNT["min_atr"])   # the gap IS the hysteresis: equal floors ended 69% of campaigns in 1.7h
        self.assertTrue(hunt._illiquid("still0.24"))          # wind down only: a book with nothing in it is not worth a market dump
        self.assertFalse(hunt._leave_coin("still0.24"))       # ... but NO cooldown: going quiet for an hour is not the end of the episode
        self.assertTrue(hunt._leave_coin("quiet12/40")); self.assertTrue(hunt._leave_coin("dead"))   # those two do cool down

    def test_an_unread_atr_keeps_the_book_and_the_ceiling_is_still_entry_only(self):
        held = dict(side="long", peak=1e8, climax=150.2)
        self.assertEqual(exit_flags(row("A", "markup", hint15="long", off=2.0, atr_pct=None), held, HUNT), [])   # absence is not evidence
        self.assertEqual(exit_flags(row("A", "markup", hint15="long", off=2.0, atr_pct=9.0), held, HUNT), [])    # a held coin's ATR exploding IS the pump
        self.assertTrue(flags_of(row("A", "markup", hint15="long", off=2.0, atr_pct=9.0), HUNT))                 # ... but it never opens one

    def test_the_inverted_churn_floor_is_off_and_turns_back_on_with_a_number(self):
        held = dict(side="short", peak=1e8, climax=150.2)
        self.assertEqual(exit_flags(row("A", "markdown", twoway24=1.0), held, HUNT), [])                          # exit_twoway 0 = off
        self.assertEqual(exit_flags(row("A", "markdown", twoway24=1.0), held, {**HUNT, "exit_twoway": 8.0}), ["flat1.0"])

class Verdicts(unittest.TestCase):
    def test_an_empty_book_adds_the_churn_leader_after_confirm_scans_with_the_phase_side(self):
        rows = [row("A", "markup", hint15="long", off=2.0, twoway24=30.0), row("B", "markdown", twoway24=20.0), row("C", "quiet")]
        st, p = {}, dict(strat=dict(symbol="OLD", sides=["long", "short"]), books={})
        v = verdict(rows, {}, HUNT, st, 1000.0); self.assertEqual((v["top"], v["adds"]), (("A", "long"), [])); self.assertEqual(st["streak"], {"A:long": 1, "B:short": 1})   # every candidate counts its own scans
        v = verdict(rows, {}, HUNT, st, 1000.0); self.assertEqual((v["adds"], v["overflow"]), ([("A", "long")], []))   # no pool: one book, the churn leader — B is not an overflow, it is the rank
        acts = apply(p, rows, v, {}, HUNT, st, 1000.0)
        self.assertEqual([a[:2] for a in acts], [("add", "A")])
        self.assertEqual(p["books"], {"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1, "blowoff_atr": 8.0, "blowoff_frac": 0.5}})   # a long book carries the standing blow-off target
        h2 = {**HUNT, "strat": {"cap_frac": 0.77, "unit_frac": 4.0, "lever": 20}}; p2 = dict(strat=dict(symbol="OLD"), books={}); st2 = dict(streak={"B:short": 2})
        v2 = verdict([row("B", "markdown")], {}, h2, st2, 1000.0); apply(p2, [row("B", "markdown")], v2, {}, h2, st2, 1000.0)
        self.assertEqual(p2["books"]["B"], {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "cap_frac": 0.77, "unit_frac": 4.0, "lever": 20})   # the track's risk profile rides on the book
        self.assertNotIn("cap_frac", p2["strat"])                                                                                     # the common strat is untouched
        self.assertEqual((p["strat"]["symbol"], p["strat"]["side"], p["strat"]["sides"]), ("A", "long", ["long", "short"]))
        self.assertEqual(set(p["record"]), {"A", "B", "BTCUSDT"}); self.assertEqual(st["held"]["A"]["side"], "long"); self.assertEqual(st["streak"], {"B:short": 2})   # the added coin's streak is spent

    def test_with_a_pool_every_confirmed_candidate_gets_a_book_and_the_overflow_is_reported(self):
        """The FCFS basket (user 2026-09-04): eligibility is the selector, the engines' pool (cycle.Pool) caps the campaigns, so every
        confirmed coin holds a book on the whole wallet (wallet/cap above cap 1) and rank only orders what max_books cannot run."""
        h = {**HUNT, "pool": 1, "max_books": 2}
        rows = [row("A", "markup", hint15="long", off=2.0, twoway24=30.0), row("B", "markdown", twoway24=20.0), row("C", "markdown", twoway24=16.0)]
        st = dict(streak={"A:long": 1, "B:short": 1, "C:short": 1}); p = dict(strat=dict(symbol="OLD"), books={})
        v = verdict(rows, {}, h, st, 1000.0); self.assertEqual((v["adds"], v["overflow"]), ([("A", "long"), ("B", "short")], [("C", "short")]))
        acts = apply(p, rows, v, {}, h, st, 1000.0)
        self.assertEqual([a[:2] for a in acts], [("add", "A"), ("add", "B"), ("overflow", "C")]); self.assertIn("max_books 2", acts[2][2])
        self.assertEqual({s: b["wallet_frac"] for s, b in p["books"].items()}, {"A": 1.0, "B": 1.0})                      # cap 1: each holder sizes on the whole wallet
        v = verdict(rows, p["books"], h, st, 2000.0); self.assertEqual((v["adds"], v["overflow"]), ([], [("C", "short")]))   # full: C stays confirmed, still reported
        h2 = {**h, "pool": 2, "max_books": 3}                                                                                 # cap 2: wallet/2 each, and the share reaches the held books
        v = verdict(rows, p["books"], h2, st, 3000.0); self.assertEqual(v["adds"], [("C", "short")])
        acts = apply(p, rows, v, {}, h2, st, 3000.0)
        self.assertEqual([a[:2] for a in acts], [("add", "C"), ("profile", "A"), ("profile", "B")])
        self.assertEqual({s: b["wallet_frac"] for s, b in p["books"].items()}, {"A": 0.5, "B": 0.5, "C": 0.5})
        self.assertEqual(set(p["record"]), {"A", "B", "C", "BTCUSDT"})

    def test_leaving_books_are_dropped_as_they_go_flat_and_a_candidate_dropping_out_restarts_its_streak(self):
        h = {**HUNT, "pool": 1}
        st = dict(held={"A": dict(side="short", exit="phase:markup"), "B": dict(side="short", exit="dead"), "C": dict(side="long")})
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1, "exit": 1},
                                               "B": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1}, "C": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1}})
        rows = [row("C", "markup", hint15="long", off=2.0), row("D", "markdown", twoway24=20.0), row("E", "markdown", twoway24=16.0)]
        v = verdict(rows, p["books"], h, st, 1000.0); self.assertEqual(st["streak"], {"D:short": 1, "E:short": 1})
        acts = apply(p, rows, v, {"A": True, "B": False, "C": False}, h, st, 1000.0)
        self.assertEqual([a[:2] for a in acts], [("drop", "A")]); self.assertEqual(list(p["books"]), ["B", "C"])                # A flat: gone now, B waits for its own flat
        v = verdict([rows[0], rows[1]], p["books"], h, st, 2000.0); self.assertEqual(st["streak"], {"D:short": 2}); self.assertEqual(v["adds"], [("D", "short")])   # E dropped out: its count is gone
        acts = apply(p, [rows[0], rows[1]], v, {"B": True, "C": False}, h, st, 2000.0)
        self.assertEqual([a[:2] for a in acts], [("drop", "B"), ("add", "D")]); self.assertGreater(st["cool"]["B"], 2000.0 + 23 * 3600); self.assertEqual(list(p["books"]), ["C", "D"])
        v = verdict(rows, p["books"], h, st, 3000.0); self.assertEqual(st["streak"], {"E:short": 1})                            # back after a gap: from one

    def test_a_funding_tax_stops_the_adds_without_the_exit_flag_and_without_a_cooldown(self):
        """A funding tax is a slow bleed, not a broken premise (user 2026-09-04): wind_down only, no exit, no cooldown, and HUNT_RESUME
        when the rate comes back. Before this it took the phase-flip branch (exit=1: the whole book at market within ten minutes)."""
        st = dict(held={"A": dict(side="short", peak=4e7, climax=150.2, tw_peak=30.0)}); p = dict(strat=dict(symbol="A", side="short"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}})
        taxed = row("A", "markdown", fund=-0.3)
        v = verdict([taxed], p["books"], HUNT, st, 1000.0); self.assertEqual(v["winds"], [("A", "fund-0.30%")])
        apply(p, [taxed], v, {"A": False}, HUNT, st, 1000.0)
        self.assertEqual(p["books"]["A"].get("wind_down"), 1); self.assertNotIn("exit", p["books"]["A"])
        back = row("A", "markdown", fund=0.01)
        for _ in range(2): v = verdict([back], p["books"], HUNT, st, 1000.0)
        self.assertEqual(v["resumes"], ["A"])
        st = dict(held={"A": dict(side="short", peak=4e7, climax=150.2, tw_peak=30.0, exit="fund-0.30%")}, streak={"B:short": 1}, xstreak={"A": 1})
        p = dict(strat=dict(symbol="A", side="short"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1}})
        rows = [row("B", "markdown", twoway24=50.0), taxed]
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertEqual(v["adds"], [("B", "short")])
        apply(p, rows, v, {"A": True}, HUNT, st, 1000.0)
        self.assertEqual(list(p["books"]), ["B"]); self.assertNotIn("A", st.get("cool", {})); self.assertNotIn("A", st["xstreak"])   # replaced flat: no cooldown, its streaks gone

    def test_a_re_added_coin_starts_its_streaks_fresh(self):
        """xstreak / rstreak are a campaign's, not a coin's: a stale count from an earlier campaign would end a re-added coin on its
        first flagged scan once exit_confirm > 1 (audit 2026-09-04)."""
        st = dict(streak={"A:short": 1}, xstreak={"A": 1, "Z": 1}, rstreak={"A": 1}); p = dict(strat=dict(symbol="OLD"), books={})
        rows = [row("A", "markdown")]
        v = verdict(rows, {}, HUNT, st, 1000.0); self.assertEqual(v["adds"], [("A", "short")]); apply(p, rows, v, {}, HUNT, st, 1000.0)
        self.assertNotIn("A", st["xstreak"]); self.assertNotIn("A", st["rstreak"]); self.assertEqual(st["xstreak"]["Z"], 1)
        v = verdict([row("A", "markup", hint15="long", off=2.0)], p["books"], {**HUNT, "exit_confirm": 2}, st, 1000.0)
        self.assertEqual(v["winds"], []); self.assertEqual(st["xstreak"]["A"], 1)     # the new campaign's first flagged scan counts one, not two

    def test_a_long_leaves_at_a_confirmed_climax_and_the_same_coin_comes_back_short_without_cooldown(self):
        st = dict(held={"A": dict(side="long", peak=4e7, climax=150.2)}); p = dict(strat=dict(symbol="A", side="long"), books={"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1}})
        v = verdict([row("A", "climax")], p["books"], HUNT, st, 1000.0); self.assertEqual(v["winds"], [("A", "phase:climax")])   # exit_confirm 1: leaving is fast
        acts = apply(p, [row("A", "climax")], v, {"A": False}, HUNT, st, 1000.0)             # positioned: winds down, stays
        self.assertEqual([a[:2] for a in acts], [("wind", "A")]); self.assertEqual(p["books"]["A"]["wind_down"], 1)
        self.assertEqual(p["books"]["A"]["exit"], 1)                                          # a phase exit: the engine sells the whole position into the next stall
        rows = [row("A", "markdown")]                                                          # the top is in: the same coin is a short candidate
        v = verdict(rows, p["books"], HUNT, st, 2000.0); self.assertEqual(v["top"], ("A", "short")); self.assertEqual(v["adds"], [])   # streak 1 of 2
        apply(p, rows, v, {"A": True}, HUNT, st, 2000.0); self.assertEqual(p["books"]["A"]["sides"], ["long"])                # flat, but not confirmed yet: stays as the placeholder
        v = verdict(rows, p["books"], HUNT, st, 3000.0); self.assertEqual(v["adds"], [("A", "short")])
        acts = apply(p, rows, v, {"A": True}, HUNT, st, 3000.0)                                 # the flip: dropped and re-added short in one write
        self.assertEqual([a[:2] for a in acts], [("drop", "A"), ("add", "A")])
        self.assertEqual(p["books"], {"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}}); self.assertEqual(st["held"]["A"]["side"], "short")
        self.assertNotIn("A", st.get("cool", {}))                                              # a phase exit leaves no cooldown
        v = verdict(rows, p["books"], HUNT, st, 3500.0); self.assertEqual(v["adds"], [])       # confirmed but its old book is not flat yet: the flip waits
        acts = apply(p, rows, dict(v, adds=[("A", "short")]), {"A": False}, HUNT, st, 3500.0); self.assertEqual(acts, [])
        st2 = dict(held={"A": dict(side="long", peak=4e7, climax=150.2, exit="phase:climax")}, streak={"B:short": 1})
        p2 = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1, "wind_down": 1}})
        v = verdict([row("B", "markdown")], p2["books"], HUNT, st2, 4000.0); self.assertEqual(v["adds"], [("B", "short")])
        acts = apply(p2, [row("B", "markdown")], v, {"A": True}, HUNT, st2, 4000.0)
        self.assertEqual([a[:2] for a in acts], [("drop", "A"), ("add", "B")]); self.assertNotIn("A", st2.get("cool", {}))     # phase exit: no cooldown
        self.assertEqual(p2["books"], {"B": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}}); self.assertEqual(p2["strat"]["side"], "short")   # a short book: no blow-off keys

    def test_an_episode_death_winds_down_gently_without_the_exit_flag(self):
        st = dict(held={"A": dict(side="short", peak=1e8, climax=150.2)})
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}})
        v = verdict([row("A", "markdown", dead=True)], p["books"], HUNT, st, 1000.0); self.assertIn("dead", v["winds"][0][1])   # volume gone: illiquid, exit_confirm 1 winds at once
        apply(p, [row("A", "markdown", dead=True)], v, {"A": False}, HUNT, st, 1000.0)
        self.assertEqual(p["books"]["A"]["wind_down"], 1); self.assertNotIn("exit", p["books"]["A"])                      # gentle: no market dump into thin books

    def test_the_coin_going_quiet_off_its_own_hot_leaves_fast_with_exit_and_a_cooldown(self):
        st = dict(held={"A": dict(side="short", peak=1e8, climax=150.2, tw_peak=40.0)})   # entered when churn was 40
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}})
        v = verdict([row("A", "markdown", twoway24=18.0)], p["books"], HUNT, st, 1000.0)  # above the absolute floor (8) but under half its own peak
        self.assertTrue(v["winds"][0][1].startswith("quiet")); self.assertEqual(st["xstreak"], {"A": 1})                 # one scan is enough (exit_confirm 1)
        acts = apply(p, [row("A", "markdown", twoway24=18.0)], v, {"A": False}, HUNT, st, 1000.0)
        self.assertEqual(p["books"]["A"]["exit"], 1)                                                                     # leave fast: sell the whole book into the next stall
        st2 = dict(held={"A": dict(side="short", exit="quiet18/40")}, streak={"B:short": 1})
        p2 = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1, "exit": 1}})
        v2 = verdict([row("B", "markdown", twoway24=30.0)], p2["books"], HUNT, st2, 2000.0)
        apply(p2, [row("B", "markdown", twoway24=30.0)], v2, {"A": True}, HUNT, st2, 2000.0)
        self.assertGreater((st2.get("cool") or {}).get("A", 0), 2000.0 + 23 * 3600)                                     # a quiet coin cools down: chase a different one, do not re-add it

    def test_an_episode_death_starts_the_cooldown(self):
        st = dict(held={"A": dict(side="short", peak=1e8, climax=150.2, exit="dead")}, streak={"B:short": 1})
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1}})
        v = verdict([row("B", "markdown")], p["books"], HUNT, st, 4000.0)
        apply(p, [row("B", "markdown")], v, {"A": True}, HUNT, st, 4000.0)
        self.assertGreater(st["cool"]["A"], 4000.0 + 23 * 3600)
        v = verdict([row("A", "markdown")], p["books"], HUNT, st, 5000.0); self.assertIsNone(v["top"])   # in cooldown: not a candidate

    def test_a_quiet_leaver_does_not_come_straight_back_on_the_other_side_in_the_same_write(self):
        st = dict(held={"A": dict(side="short", exit="quiet18/40")}, streak={"A:long": 1})
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1, "exit": 1}})
        rows = [row("A", "markup", hint15="long", off=2.0)]                                   # the quiet coin now reads markup: a long candidate on paper
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertIsNone(v["top"]); self.assertEqual(v["adds"], [])   # a cooling coin is no candidate, on either side
        acts = apply(p, rows, v, {"A": True}, HUNT, st, 1000.0)
        self.assertEqual(acts, []); self.assertEqual(list(p["books"]), ["A"]); self.assertEqual(p["books"]["A"]["sides"], ["short"])   # stays as the flat placeholder
        self.assertNotIn("A", st.get("cool", {}))                                              # not dropped, so not cooled yet either

    def test_a_quiet_leaver_does_not_hold_the_top_slot_so_the_next_coin_confirms_and_the_drop_happens(self):
        """Deadlock (audit 2026-09-03): the streak is counted for the top candidate only, so a quiet leaver reading the other side with the
        best churn froze the streak, its own drop / cooldown and the second-ranked coin, until its read changed."""
        st = dict(held={"A": dict(side="short", exit="quiet18/40")}, streak={})
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1, "exit": 1}})
        rows = [row("A", "markup", hint15="long", off=2.0, twoway24=60.0), row("B", "markdown", twoway24=30.0)]   # the quiet coin has the best churn on paper
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertEqual(v["top"], ("B", "short")); self.assertEqual(st["streak"], {"B:short": 1})
        self.assertEqual(apply(p, rows, v, {"A": True}, HUNT, st, 1000.0), [])                                        # B not confirmed yet: A stays as the flat placeholder
        v = verdict(rows, p["books"], HUNT, st, 2000.0); self.assertEqual(v["adds"], [("B", "short")])
        acts = apply(p, rows, v, {"A": True}, HUNT, st, 2000.0)
        self.assertEqual([a[:2] for a in acts], [("drop", "A"), ("add", "B")]); self.assertGreater(st["cool"]["A"], 2000.0 + 23 * 3600)

    def test_the_risk_profile_is_synced_onto_an_existing_hunt_book_and_stale_keys_are_stripped(self):
        """Audit 2026-09-03: hunt.strat was copied only when a book was created, so the 20:08 normalization reached the live EGLD book by hand."""
        h2 = {**HUNT, "strat": {"cap_frac": 0.4, "unit_frac": 2.0, "max_units": 3}}
        st = dict(held={"A": dict(side="long", peak=4e7, climax=150.2)})
        p = dict(strat=dict(symbol="A", side="long"), books={"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1, "blowoff_atr": 8.0, "blowoff_frac": 0.5,
                                                                    "cap_frac": 0.77, "unit_frac": 4.0, "max_units": 4, "max_stops_day": 6}})
        rows = [row("A", "markup", hint15="long", off=2.0)]
        v = verdict(rows, p["books"], h2, st, 1000.0); acts = apply(p, rows, v, {"A": False}, h2, st, 1000.0)
        self.assertEqual([a[:2] for a in acts], [("profile", "A")]); self.assertIn("-max_stops_day", acts[0][2])
        self.assertEqual(p["books"]["A"], {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1, "blowoff_atr": 8.0, "blowoff_frac": 0.5, "cap_frac": 0.4, "unit_frac": 2.0, "max_units": 3})
        self.assertEqual(apply(p, rows, verdict(rows, p["books"], h2, st, 2000.0), {"A": False}, h2, st, 2000.0), [])   # in sync: nothing to report

    def test_a_phase_flip_exit_is_undone_when_the_read_comes_back_before_flat(self):
        st = dict(held={"A": dict(side="long", exit="phase:climax", peak=4e7, climax=150.2)})
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1, "wind_down": 1, "exit": 1}})
        rows = [row("A", "markup", hint15="long", off=2.0)]                                   # one bad 15m close read climax; now it is markup again
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertEqual(v["resumes"], []); self.assertEqual(st["rstreak"], {"A": 1})
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertEqual(v["resumes"], ["A"]); self.assertEqual(v["adds"], [])
        acts = apply(p, rows, v, {"A": False}, HUNT, st, 1000.0)
        self.assertEqual([a[:2] for a in acts], [("resume", "A")]); self.assertNotIn("wind_down", p["books"]["A"]); self.assertNotIn("exit", p["books"]["A"])
        st2 = dict(held={"A": dict(side="long", exit="quiet10/40")})                           # a quiet leaver never resumes: it cools and we chase another coin
        v = verdict(rows, {"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1, "wind_down": 1, "exit": 1}}, HUNT, st2, 1000.0); self.assertEqual(v["resumes"], [])

    def test_a_basket_or_a_hand_book_makes_the_job_refuse(self):
        v = verdict([row("A")], {"HYPEUSDT": {"wallet_frac": 0.3}}, HUNT, {}, 1000.0)
        self.assertIn("non-hunt", v["refuse"]); self.assertEqual(v["adds"], [])

    def test_books_never_empties_when_the_only_book_is_gone_and_nothing_qualifies(self):
        st = dict(held={"A": {"side": "short"}}); p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1}})
        v = verdict([row("C", "quiet")], p["books"], HUNT, st, 1000.0); self.assertEqual(v["adds"], [])
        apply(p, [row("C", "quiet")], v, {"A": True}, HUNT, st, 1000.0)
        self.assertEqual(list(p["books"]), ["A"])

class MarketTape(unittest.TestCase):
    """CONCEPT 트랙 B: the market's move is RECORDED and read by nothing — the threshold waits for the cross-section (NEXT 17b)."""
    def stub(self, closes, highs=None):
        bars = lambda xs, hs: [dict(o=x, h=(hs or xs)[i], l=x, c=x, qv=1.0) for i, x in enumerate(xs)]
        return types.SimpleNamespace(candles=lambda sym, tf, limit=0: bars(closes, highs) + [dict(o=0, h=0, l=0, c=0, qv=0)])

    def test_the_market_read_is_a_drawdown_from_the_4h_high_not_just_a_return(self):
        m = hunt.market(self.stub([100.0] * 26 + [90.0], highs=[100.0] * 26 + [95.0]))
        self.assertEqual(m["sym"], "BTCUSDT"); self.assertEqual(m["px"], 90.0)
        self.assertEqual(m["h1"], 0.0)                     # vs the last CLOSED hour's close, which is still 100 ...
        self.assertEqual(m["dd4"], -10.0)                  # ... but price is 10% under the 4h high: the cascade shows here

    def test_an_unreachable_market_is_recorded_as_nothing_and_never_raises(self):
        def boom(*a, **kw): raise TimeoutError("read timed out")
        self.assertIsNone(hunt.market(types.SimpleNamespace(candles=boom), log=lambda *a: None))
        self.assertIsNone(hunt.market(self.stub([100.0, 100.0]), log=lambda *a: None))   # too few closed 1H bars to read

class PositivelyNotOurSide(unittest.TestCase):
    """새로 열지 않을 국면에서 계속 담는 것은 CONCEPT-B 와 어긋난다(담기도 리스크를 여는 것). 다만 `unknown` 은 근거가 아니다."""
    long_ = dict(side="long", peak=1e8, climax=150.2, tw_peak=30.0)
    short = dict(side="short", peak=1e8, climax=150.2, tw_peak=30.0)

    def test_a_positive_reading_against_us_stops_the_adds_without_dumping(self):
        for ph in ("distribution", "dead", "quiet"):
            f = exit_flags(row("A", ph), self.long_, HUNT)
            self.assertEqual(f, [f"hold:{ph}"], ph)
            self.assertTrue(hunt._illiquid(f[0]), ph)          # wind_down 만: 검증 안 된 라벨에 포지션을 던지지 않는다
        self.assertFalse(hunt._leave_coin("hold:distribution"))  # 쿨다운 없음: 분배는 에피소드의 끝이 아니다
        self.assertTrue(hunt._leave_coin("hold:dead")); self.assertTrue(hunt._leave_coin("hold:quiet"))

    def test_unknown_is_not_a_reading_and_keeps_the_book(self):
        self.assertEqual(exit_flags(row("A", "unknown"), self.long_, HUNT), [])    # 판독의 36%, 나가는 변형은 기각됐다(NEXT 19a)
        self.assertEqual(exit_flags(row("A", "unknown"), self.short, HUNT), [])

    def test_distribution_is_asymmetric_because_a_short_is_on_its_side(self):
        self.assertEqual(exit_flags(row("A", "distribution"), self.short, HUNT), [])   # 고점이 팔리는 중 = 숏에게는 우리 편
        self.assertEqual(exit_flags(row("A", "distribution"), self.long_, HUNT), ["hold:distribution"])
        self.assertTrue(flags_of(row("A", "distribution"), HUNT))                      # 어느 방향으로도 새로 열지는 않는다

    def test_a_phase_flip_still_liquidates(self):
        for ph in ("climax", "markdown", "squeeze"):
            f = exit_flags(row("A", ph), self.long_, HUNT)
            self.assertEqual(f, [f"phase:{ph}"], ph); self.assertFalse(hunt._illiquid(f[0]), ph)   # 전량 청산은 그대로

class SecondOpinion(unittest.TestCase):
    """AI 판독은 국면과 방향만 대체한다. 자격(통행료 veto)은 못 뒤집고, 실패하면 결정론이 그대로 선다."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = hunt.LOGS
        hunt.LOGS = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, hunt, "LOGS", self.logs)

    def run_with(self, out, rc=0):
        real = hunt.subprocess.run
        def fake(cmd, **kw):
            i = cmd.index("-o")
            with open(cmd[i + 1], "w", encoding="utf-8") as fh: fh.write(out)
            return types.SimpleNamespace(returncode=rc, stdout="", stderr="")
        hunt.subprocess.run = fake
        try: return hunt.ai_read([row("A", "unknown"), row("B", "markdown")], {**HUNT, "ai_read": 1}, log=lambda *a: None)
        finally: hunt.subprocess.run = real

    def test_the_reading_is_taken_only_for_known_symbols_and_known_phases(self):
        r = self.run_with('{"reads":[{"symbol":"A","phase":"distribution","conf":88,"why":"fat wicks"},'
                          '{"symbol":"B","phase":"nonsense"},{"symbol":"ZZZ","phase":"markup"}]}')
        self.assertEqual(list(r), ["A"]); self.assertEqual(r["A"][0], "distribution")
        self.assertEqual(r["A"][3:], (1, 1))                    # 합의 1/1

    def test_the_ensemble_votes_and_records_how_far_it_split(self):
        """같은 입력에 답이 17% 흔들리는 것을 실측했다(2026-09-04) — 다수결이 그것을 흡수하고 합의율이 실측 신뢰도가 된다."""
        outs = ['{"reads":[{"symbol":"A","phase":"markdown","conf":90},{"symbol":"B","phase":"markup","conf":70}]}',
                '{"reads":[{"symbol":"A","phase":"markdown","conf":80},{"symbol":"B","phase":"quiet","conf":60}]}',
                'garbage']                                      # 한 번 실패해도 나머지로 진행한다
        real = hunt.subprocess.run; seq = iter(outs)
        def fake(cmd, **kw):
            i = cmd.index("-o")
            with open(cmd[i + 1], "w", encoding="utf-8") as fh: fh.write(next(seq))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        hunt.subprocess.run = fake
        try: r = hunt.ai_read([row("A", "markup"), row("B", "markup")], {**HUNT, "ai_read": 1, "ai_runs": 3}, log=lambda *a: None)
        finally: hunt.subprocess.run = real
        self.assertEqual(r["A"][0], "markdown"); self.assertEqual(r["A"][3:], (2, 3))
        self.assertEqual(r["A"][1], 85)                                                  # 이긴 라벨의 평균 conf
        self.assertEqual(r["B"][0], "markup"); self.assertEqual(r["B"][3:], (1, 3))  # no majority: rule, not first run

    def test_every_run_failing_keeps_the_deterministic_read(self):
        real = hunt.subprocess.run
        hunt.subprocess.run = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("codex timed out"))
        try: self.assertEqual(hunt.ai_read([row("A")], {**HUNT, "ai_read": 1, "ai_runs": 3}, log=lambda *a: None), {})
        finally: hunt.subprocess.run = real

    def test_any_failure_keeps_the_deterministic_read(self):
        self.assertEqual(self.run_with("not json at all"), {})          # 형식 오류
        real = hunt.subprocess.run
        def boom(*a, **kw): raise TimeoutError("codex timed out")
        hunt.subprocess.run = boom
        try: self.assertEqual(hunt.ai_read([row("A")], {**HUNT, "ai_read": 1}, log=lambda *a: None), {})
        finally: hunt.subprocess.run = real

    def test_the_ai_cannot_overturn_a_toll_veto(self):
        thin = row("A", "unknown", qv=1e6)                              # 거래대금 바닥 아래
        thin.update(phase="markup", side="long")                        # AI 가 markup 이라 해도
        self.assertTrue([f for f in flags_of(thin, HUNT) if f.startswith("vol")])   # vol veto 는 그대로 선다

    def test_chart_slots_reserve_the_holding_and_include_unknown_potential_candidates(self):
        held = row("HELD", "quiet", qv=1e6, twoway24=1.0)               # 보유는 현재 통행료와 무관하게 첫 슬롯
        thin = row("THIN", "unknown", qv=1e6, twoway24=100.0)          # AI가 고칠 수 없는 거래대금 veto
        unknown = row("UNKNOWN", "unknown", twoway24=90.0)             # AI가 markup/markdown으로 바꿀 수 있으므로 차트가 필요
        taxed = row("TAXED", "markup", fund=0.5, twoway24=80.0)        # 방향이 바뀌면 funding veto도 사라질 수 있다
        self.assertEqual(hunt.chart_symbols([thin, taxed, unknown, held], ["HELD"], 3), ["HELD", "UNKNOWN", "TAXED"])

    def test_chart_slot_limit_never_lets_a_candidate_displace_the_holding(self):
        self.assertEqual(hunt.chart_symbols([row("HOT", twoway24=99), row("HELD", "quiet")], ["HELD"], 1), ["HELD"])

    def test_a_tie_is_broken_by_the_rule_reader_when_it_sits_among_the_tied_answers(self):
        """1:1:1 of three runs is no majority. The rule reader's phase wins when it is one of the tied answers, else the first run —
        which is what the run order alone did before (user decision 2026-09-04; 11 of 688 rows that day were ties)."""
        outs = ['{"reads":[{"symbol":"A","phase":"markdown","conf":70},{"symbol":"B","phase":"markdown","conf":70}]}',
                '{"reads":[{"symbol":"A","phase":"markup","conf":70},{"symbol":"B","phase":"markup","conf":70}]}',
                '{"reads":[{"symbol":"A","phase":"quiet","conf":70},{"symbol":"B","phase":"quiet","conf":70}]}']
        real = hunt.subprocess.run
        def fake(cmd, **kw):
            i = cmd.index("-o"); n = int(os.path.basename(cmd[i + 1])[4:-5])        # read<n>.json: run n gets outs[n-1], whatever thread runs it
            with open(cmd[i + 1], "w", encoding="utf-8") as fh: fh.write(outs[n - 1])
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        hunt.subprocess.run = fake
        try: r = hunt.ai_read([row("A", "markup"), row("B", "unknown")], {**HUNT, "ai_read": 1, "ai_runs": 3}, log=lambda *a: None)
        finally: hunt.subprocess.run = real
        self.assertEqual(r["A"][0], "markup"); self.assertEqual(r["A"][3:], (1, 3))      # the rule reader's markup is among the three answers
        self.assertEqual(r["B"][0], "unknown"); self.assertEqual(r["B"][3:], (0, 3))

    def test_failed_process_output_is_not_a_vote(self):
        self.assertEqual(self.run_with('{"reads":[{"symbol":"A","phase":"markup"}]}', rc=1), {})

    def test_new_hunt_settings_or_a_failed_read_invalidate_the_finished_scan(self):
        p = {"hunt": {"on": 1, "strat": {"cap_frac": .05}}, "record": {}}
        self.assertTrue(hunt.scan_config_current(p, {**p, "record": {"X": []}}))
        self.assertFalse(hunt.scan_config_current(p, {"hunt": {"on": 0}}))
        self.assertFalse(hunt.scan_config_current(p, None))

class AiOnlyExit(unittest.TestCase):
    """A phase the AI alone reads (the rule reader disagrees) leaves after `confirm` scans, like an entry; a measured flag or a flip the rule
    reader agrees with keeps `exit_confirm` 1 (2026-09-04: the AI label flips between scans 2.5x as often, 14 one-scan blips vs the reader's 0,
    and the exit floor is one scan, so a blip's sale cannot be undone by HUNT_RESUME)."""
    bk = {"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1}}
    def held(self): return dict(held={"A": dict(side="long", peak=4e7, climax=150.2)})

    def test_an_ai_only_phase_flip_waits_a_second_scan_and_a_rule_agreed_one_does_not(self):
        st = self.held(); blip = row("A", "climax", phase_det="markup")                                       # TRIA 16:10 / 18:11 / 18:30: climax by the AI alone
        v = verdict([blip], self.bk, HUNT, st, 1000.0); self.assertEqual(v["winds"], []); self.assertEqual(st["xstreak"]["A"], 1)
        v = verdict([blip], self.bk, HUNT, st, 2000.0); self.assertEqual(v["winds"], [("A", "phase:climax")])   # the second scan leaves
        v = verdict([row("A", "climax", phase_det="climax")], self.bk, HUNT, self.held(), 1000.0); self.assertEqual(v["winds"], [("A", "phase:climax")])   # both readers: one scan
        v = verdict([row("A", "climax")], self.bk, HUNT, self.held(), 1000.0); self.assertEqual(v["winds"], [("A", "phase:climax")])                      # no AI read: as before
        st = self.held(); v = verdict([blip], self.bk, HUNT, st, 1000.0); v = verdict([row("A", "markup", hint15="long", off=2.0, phase_det="unknown")], self.bk, HUNT, st, 2000.0)
        self.assertEqual((v["winds"], st["xstreak"]["A"]), ([], 0))                                            # the blip passed: the count restarts

    def test_hold_flags_from_the_ai_wait_too_but_a_measured_flag_beside_them_does_not(self):
        st = self.held()
        v = verdict([row("A", "distribution", phase_det="unknown")], self.bk, HUNT, st, 1000.0); self.assertEqual(v["winds"], [])   # ARB 2026-09-04: distribution on 7 of 20 scans, alternating with markup
        v = verdict([row("A", "distribution", phase_det="unknown")], self.bk, HUNT, st, 2000.0); self.assertEqual(v["winds"], [("A", "hold:distribution")])
        v = verdict([row("A", "distribution", phase_det="unknown", atr_pct=0.2)], self.bk, HUNT, self.held(), 1000.0)
        self.assertEqual(v["winds"], [("A", "still0.2,hold:distribution")])                                    # a measured flag beside it: one scan, as before

class ScanClock(unittest.TestCase):
    """Scans sit on the bar clock: the period grid plus SCAN_OFFSET_S after each close (user 2026-09-04 "정렬"): the phase comes from closed 15m
    bars, so an unaligned scan only added latency (mean 7.6 min after the close) and same-bar re-reads (37%, where the AI flipped 12% on dice)."""
    def test_the_next_scan_is_the_next_grid_slot_after_now(self):
        from bot.hunt import next_scan_t, SCAN_OFFSET_S
        b = 1_788_500_000 - 1_788_500_000 % 900                                 # a 15-minute boundary
        self.assertEqual(next_scan_t(b + 10, 900), b + SCAN_OFFSET_S)           # just after the close: this period's slot
        self.assertEqual(next_scan_t(b + 100, 900), b + 900 + SCAN_OFFSET_S)    # past the slot: the next close's
        self.assertEqual(next_scan_t(b + 899, 900), b + 900 + SCAN_OFFSET_S)
        self.assertEqual(next_scan_t(b + SCAN_OFFSET_S, 900), b + 900 + SCAN_OFFSET_S)   # exactly on the slot: the next one, never a zero wait
        self.assertEqual(next_scan_t(b + 100, 600), b + 600 + SCAN_OFFSET_S)    # another period aligns to its own grid

class TheOtherWriter(unittest.TestCase):
    """pid_alive gates the only write of params.books, so both of its errors must be the safe one."""
    def setUp(self):
        self.dir = tempfile.mkdtemp(); self.pidfile = os.path.join(self.dir, "select.pid"); self.real = hunt.subprocess.run
    def tearDown(self):
        hunt.subprocess.run = self.real; shutil.rmtree(self.dir, ignore_errors=True)
    def pid(self, v):
        with open(self.pidfile, "w") as f: f.write(v)

    def cmdline(self, out):
        hunt.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout=out) if out is not None else (_ for _ in ()).throw(OSError("powershell gone"))

    def test_a_reused_pid_running_something_else_does_not_block(self):
        self.pid("14976")                   # the pid is alive, but it is not bot.select: the 2026-09-04 00:16 HUNT_BLOCKED
        self.cmdline('"C:\\Python313\\python.exe" -m bot.supervise hunt\n')
        self.assertEqual(pid_alive(self.pidfile), "")
        self.cmdline("\n"); self.assertEqual(pid_alive(self.pidfile), "")          # dead pid: no command line at all

    def test_the_real_other_writer_blocks_in_both_of_its_forms(self):
        self.pid("4242")
        self.cmdline('"C:\\Python313\\python.exe" -m bot.supervise select\n')       # what select.pid actually holds: the supervisor
        self.assertIn("bot.supervise select", pid_alive(self.pidfile))
        self.cmdline('"C:\\Python313\\python.exe" -u -m bot.select\n')              # the child it spawns
        self.assertIn("bot.select", pid_alive(self.pidfile))

    def test_an_unreadable_command_line_blocks_rather_than_risking_two_writers(self):
        self.pid("4242"); self.cmdline(None)
        self.assertIn("unreadable", pid_alive(self.pidfile))

    def test_no_pid_file_and_a_garbage_pid_file_do_not_block(self):
        self.cmdline('"C:\\Python313\\python.exe" -m bot.supervise select\n')     # would block if the file were read at all
        self.assertEqual(pid_alive(self.pidfile), "")
        self.pid("not-a-pid"); self.assertEqual(pid_alive(self.pidfile), "")

if __name__ == "__main__":
    unittest.main()
