"""Invariants of the pump-coin hunting side pipeline (bot/hunt.py): phase names the side, the toll vetoes, one book at a time,
phase exits rotate without cooldown, episode deaths cool down.  python -m unittest bot.test_hunt"""
import unittest
from bot.hunt import flags_of, exit_flags, verdict, apply, HUNT

def row(sym, phase="markdown", **kw):
    r = dict(symbol=sym, px=129.0, qv=4e7, base7=4e6, ratio=10.0, new=False, chg24=-5.0, fund=0.01, oi=1e7, spread_bp=2.0, lever_max=25, min_notional=1.0,
             tick_pct=0.001, twoway24=30.0, net24=-3.0, run=50.0, off=14.0, high48=150.2, atr_pct=0.6, atr15_pct=2.0, hint15="short", dead=False,
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

    def test_exit_flags_read_the_side_and_the_phase(self):
        short = dict(side="short", peak=1e8, climax=150.2); long_ = dict(side="long", peak=1e8, climax=150.2)
        self.assertEqual(exit_flags(row("A", "markdown"), short, HUNT), [])
        self.assertEqual(exit_flags(row("A", "markup", hint15="long", off=2.0), long_, HUNT), [])
        self.assertEqual(exit_flags(row("A", "climax"), long_, HUNT), ["phase:climax"])          # the long stops at the climax
        self.assertEqual(exit_flags(row("A", "climax"), short, HUNT), [])                         # a short does not care about a climax vote
        self.assertEqual(exit_flags(row("A", "unknown", off=70.0, hint15="long"), long_, HUNT), ["far70.0"])   # 70% under the top: the long leaves whatever the structure says
        self.assertEqual(exit_flags(row("A", "unknown", off=70.0, hint15="long"), short, HUNT), [])            # a short far under the top is where it earns
        self.assertEqual(exit_flags(row("A", "markup"), short, HUNT), ["phase:markup"])           # a short leaves a relaunch
        self.assertEqual(exit_flags(row("A", "squeeze"), short, HUNT), ["phase:squeeze"])
        self.assertEqual(exit_flags(row("A", "markdown", px=151.0), short, HUNT), ["newhigh"])
        self.assertEqual(exit_flags(row("A", "markdown", dead=True), short, HUNT), ["dead"])
        self.assertTrue(exit_flags(row("A", "markdown", twoway24=5.0), short, HUNT)[0].startswith("flat"))

class Verdicts(unittest.TestCase):
    def test_an_empty_book_adds_the_churn_leader_after_confirm_scans_with_the_phase_side(self):
        rows = [row("A", "markup", hint15="long", off=2.0, twoway24=30.0), row("B", "markdown", twoway24=20.0), row("C", "quiet")]
        st, p = {}, dict(strat=dict(symbol="OLD", sides=["long", "short"]), books={})
        v = verdict(rows, {}, HUNT, st, 1000.0); self.assertEqual((v["top"], v["add"]), (("A", "long"), None)); self.assertEqual(st["streak"], {"A:long": 1})
        v = verdict(rows, {}, HUNT, st, 1000.0); self.assertEqual(v["add"], ("A", "long"))
        acts = apply(p, rows, v, {}, HUNT, st, 1000.0)
        self.assertEqual([a[:2] for a in acts], [("add", "A")])
        self.assertEqual(p["books"], {"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1}})
        self.assertEqual((p["strat"]["symbol"], p["strat"]["side"], p["strat"]["sides"]), ("A", "long", ["long", "short"]))
        self.assertEqual(set(p["record"]), {"A", "B", "BTCUSDT"}); self.assertEqual(st["held"]["A"]["side"], "long")

    def test_a_long_leaves_at_a_confirmed_climax_and_the_same_coin_comes_back_short_without_cooldown(self):
        st = dict(held={"A": dict(side="long", peak=4e7, climax=150.2)}); p = dict(strat=dict(symbol="A", side="long"), books={"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1}})
        v = verdict([row("A", "climax")], p["books"], HUNT, st, 1000.0); self.assertEqual(v["wind"], ("A", "phase:climax"))   # exit_confirm 1: leaving is fast
        acts = apply(p, [row("A", "climax")], v, {"A": False}, HUNT, st, 1000.0)             # positioned: winds down, stays
        self.assertEqual([a[:2] for a in acts], [("wind", "A")]); self.assertEqual(p["books"]["A"]["wind_down"], 1)
        self.assertEqual(p["books"]["A"]["exit"], 1)                                          # a phase exit: the engine sells the whole position into the next stall
        rows = [row("A", "markdown")]                                                          # the top is in: the same coin is a short candidate
        v = verdict(rows, p["books"], HUNT, st, 2000.0); self.assertEqual(v["top"], ("A", "short")); self.assertIsNone(v["add"])   # streak 1 of 2
        apply(p, rows, v, {"A": True}, HUNT, st, 2000.0); self.assertEqual(p["books"]["A"]["sides"], ["long"])                # flat, but not confirmed yet: stays
        v = verdict(rows, p["books"], HUNT, st, 3000.0); self.assertEqual(v["add"], ("A", "short"))
        acts = apply(p, rows, v, {"A": True}, HUNT, st, 3000.0)                                 # the flip: dropped and re-added short in one write
        self.assertEqual([a[:2] for a in acts], [("drop", "A"), ("add", "A")])
        self.assertEqual(p["books"], {"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}}); self.assertEqual(st["held"]["A"]["side"], "short")
        self.assertNotIn("A", st.get("cool", {}))                                              # a phase exit leaves no cooldown
        st2 = dict(held={"A": dict(side="long", peak=4e7, climax=150.2, exit="phase:climax")}, streak={"B:short": 1})
        p2 = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1, "wind_down": 1}})
        v = verdict([row("B", "markdown")], p2["books"], HUNT, st2, 4000.0); self.assertEqual(v["add"], ("B", "short"))
        acts = apply(p2, [row("B", "markdown")], v, {"A": True}, HUNT, st2, 4000.0)
        self.assertEqual([a[:2] for a in acts], [("drop", "A"), ("add", "B")]); self.assertNotIn("A", st2.get("cool", {}))     # phase exit: no cooldown
        self.assertEqual(p2["books"], {"B": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}}); self.assertEqual(p2["strat"]["side"], "short")

    def test_an_episode_death_winds_down_gently_without_the_exit_flag(self):
        st = dict(held={"A": dict(side="short", peak=1e8, climax=150.2)})
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}})
        v = verdict([row("A", "markdown", dead=True)], p["books"], HUNT, st, 1000.0); self.assertIn("dead", v["wind"][1])   # volume gone: illiquid, exit_confirm 1 winds at once
        apply(p, [row("A", "markdown", dead=True)], v, {"A": False}, HUNT, st, 1000.0)
        self.assertEqual(p["books"]["A"]["wind_down"], 1); self.assertNotIn("exit", p["books"]["A"])                      # gentle: no market dump into thin books

    def test_the_coin_going_quiet_off_its_own_hot_leaves_fast_with_exit_and_a_cooldown(self):
        st = dict(held={"A": dict(side="short", peak=1e8, climax=150.2, tw_peak=40.0)})   # entered when churn was 40
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1}})
        v = verdict([row("A", "markdown", twoway24=18.0)], p["books"], HUNT, st, 1000.0)  # above the absolute floor (8) but under half its own peak
        self.assertTrue(v["wind"][1].startswith("quiet")); self.assertEqual(st["xstreak"], {"A": 1})                     # one scan is enough (exit_confirm 1)
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
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertEqual(v["add"], ("A", "long"))
        acts = apply(p, rows, v, {"A": True}, HUNT, st, 1000.0)
        self.assertEqual(acts, []); self.assertEqual(list(p["books"]), ["A"]); self.assertEqual(p["books"]["A"]["sides"], ["short"])   # stays as the flat placeholder
        self.assertNotIn("A", st.get("cool", {}))                                              # not dropped, so not cooled yet either

    def test_a_phase_flip_exit_is_undone_when_the_read_comes_back_before_flat(self):
        st = dict(held={"A": dict(side="long", exit="phase:climax", peak=4e7, climax=150.2)})
        p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1, "wind_down": 1, "exit": 1}})
        rows = [row("A", "markup", hint15="long", off=2.0)]                                   # one bad 15m close read climax; now it is markup again
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertIsNone(v["resume"]); self.assertEqual(st["rstreak"], {"A": 1})
        v = verdict(rows, p["books"], HUNT, st, 1000.0); self.assertEqual(v["resume"], "A"); self.assertIsNone(v["add"])
        acts = apply(p, rows, v, {"A": False}, HUNT, st, 1000.0)
        self.assertEqual([a[:2] for a in acts], [("resume", "A")]); self.assertNotIn("wind_down", p["books"]["A"]); self.assertNotIn("exit", p["books"]["A"])
        st2 = dict(held={"A": dict(side="long", exit="quiet10/40")})                           # a quiet leaver never resumes: it cools and we chase another coin
        v = verdict(rows, {"A": {"wallet_frac": 1.0, "sides": ["long"], "hunt": 1, "wind_down": 1, "exit": 1}}, HUNT, st2, 1000.0); self.assertIsNone(v["resume"])

    def test_a_basket_or_a_hand_book_makes_the_job_refuse(self):
        v = verdict([row("A")], {"HYPEUSDT": {"wallet_frac": 0.3}}, HUNT, {}, 1000.0)
        self.assertIn("non-hunt", v["refuse"]); self.assertIsNone(v["add"])

    def test_books_never_empties_when_the_only_book_is_gone_and_nothing_qualifies(self):
        st = dict(held={"A": {"side": "short"}}); p = dict(strat=dict(symbol="A"), books={"A": {"wallet_frac": 1.0, "sides": ["short"], "hunt": 1, "wind_down": 1}})
        v = verdict([row("C", "quiet")], p["books"], HUNT, st, 1000.0); self.assertIsNone(v["add"])
        apply(p, [row("C", "quiet")], v, {"A": True}, HUNT, st, 1000.0)
        self.assertEqual(list(p["books"]), ["A"])

if __name__ == "__main__":
    unittest.main()
