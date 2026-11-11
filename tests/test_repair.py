"""repair.plan: rips only unlocked, non-plane copper near the open connection ends; strategy ladder."""
import unittest

from badpcb import ratsnest, repair
from tests import synth


def trk(i, a, b, net, locked=False, w=0.2):
    d = synth.track(a, b, net, locked=locked, w=w)
    d["id"] = i
    return d


class Plan(unittest.TestCase):
    def board(self):
        parts = synth.singles([("R1", (10, 10), "A"), ("R2", (60, 10), "A"), ("R3", (12, 13), "B"), ("R4", (30, 13), "B"),
                               ("R5", (11, 12), "C"), ("R6", (40, 40), "C"), ("R7", (80, 40), "D"), ("R8", (90, 40), "D")])
        tracks = [trk("near_free", (12, 13), (20, 13), "B"),                # B copper next to A's open end: ripped
                  trk("near_locked", (11, 12), (30, 12), "C", locked=True),  # locked: kept, counted
                  trk("far", (80, 40), (90, 40), "D"),                       # far from every open end
                  trk("a_stub", (10, 10), (10, 11), "A")]                    # failing net's own stub: ripped
        vias = [synth.via((12.5, 12.5), "B")]
        vias[0]["locked"] = False
        vias[0]["id"] = "via_b"
        return synth.board(parts, tracks, vias)

    def test_disk_selection(self):
        b = self.board()
        rn = ratsnest.build(b, [])
        conns = [c for c in rn.conns if c.net == "A"]
        self.assertTrue(conns)
        p = repair.plan(b, conns, radius=4.0)
        self.assertIn("near_free", p["ids"])
        self.assertIn("via_b", p["ids"])
        self.assertIn("a_stub", p["ids"])
        self.assertNotIn("far", p["ids"])
        self.assertNotIn("near_locked", p["ids"])
        self.assertEqual(p["locked"], {"C": 1})
        self.assertEqual(p["failing"], ["A"])
        self.assertEqual(p["nets"], ["A", "B"])
        txt = repair.describe(p, 4.0)
        self.assertIn("unlock C", txt)

    def test_scope_and_plane_nets_skipped(self):
        b = self.board()
        rn = ratsnest.build(b, [])
        p = repair.plan(b, rn.conns, radius=4.0, scope={"D"}, plane_nets=("B",))
        self.assertEqual(p["failing"], [])
        p = repair.plan(b, [c for c in rn.conns if c.net == "A"], radius=4.0, plane_nets=("B",))
        self.assertNotIn("near_free", p["ids"])


class ScopeReport(unittest.TestCase):
    def test_widening_beyond_the_region_is_logged(self):
        b = Plan().board()
        rn = ratsnest.build(b, [])
        p = repair.plan(b, [c for c in rn.conns if c.net == "A"], radius=4.0)        # A runs R1 (10,10) -> R2 (60,10); B's copper is ripped too
        L = repair.scope_report(p, {"A"}, (0, 0, 20, 20), 4.0)                      # request: a region around R1 only
        txt = "\n".join(L)
        self.assertIn("WIDENED", txt)
        self.assertIn("outside the requested region", txt)                         # R2 end at x=60
        self.assertIn("+1 net(s) beyond the request: B", txt)
        self.assertIn("bystanders", txt)

    def test_nothing_to_report_inside_the_request(self):
        b = Plan().board()
        rn = ratsnest.build(b, [])
        p = repair.plan(b, [c for c in rn.conns if c.net == "A"], radius=4.0)
        self.assertEqual(repair.scope_report(p, {"A", "B"}, (0, 0, 100, 100), 4.0), [])


def rec(n, cuts):
    return {"n": n, "cuts": {h: {"id": "C97", "nets": sorted(nets), "n": len(nets), "dem": 4, "cap": 22} for h, nets in cuts.items()}}


class CutCycle(unittest.TestCase):
    def test_same_cut_rotating_nets_fires_although_net_sets_differ_a_lot(self):
        h = [rec(3, {"U1 ring S": ["EN4", "EN5", "ADC0"]}), rec(3, {"U1 ring S": ["EN6", "EN7", "ADC3"]}), rec(3, {"U1 ring S": ["EN2", "EN5", "ADC1"]})]
        c = repair.cut_cycle(h)
        self.assertIsNotNone(c)
        self.assertEqual(c["handle"], "U1 ring S")
        self.assertEqual(len(c["nets"]), 8)
        txt = repair.cut_cycle_advice(c, "s1", "U1")
        self.assertIn("U1 ring S", txt)
        self.assertIn("ESCALATE", txt)
        self.assertIn("--only CHANNEL", txt)

    def test_name_detector_would_stay_quiet_with_more_than_seven_nets_but_cut_fires(self):
        # 4 open nets per pass, 12 distinct names over the window: the net-name ping-pong rule needs <= 7 distinct nets
        h = [rec(4, {"U1 ring S": ["a1", "a2", "a3", "a4"]}), rec(4, {"U1 ring S": ["b1", "b2", "b3", "b4"]}), rec(4, {"U1 ring S": ["c1", "c2", "c3", "c4"]})]
        self.assertEqual(repair.cut_cycle(h)["handle"], "U1 ring S")

    def test_different_cuts_each_pass_is_not_a_cycle(self):
        h = [rec(2, {"U1 ring S": ["a", "b"]}), rec(2, {"R4|R5": ["c", "d"]}), rec(2, {"U1 ring N": ["e", "f"]})]
        self.assertIsNone(repair.cut_cycle(h))

    def test_same_nets_same_cut_is_plain_failure_not_rotation(self):
        h = [rec(2, {"U1 ring S": ["a", "b"]})] * 3
        self.assertIsNone(repair.cut_cycle(h))

    def test_needs_three_passes_with_cut_data_and_few_open_conns(self):
        h = [rec(2, {"x": ["a"]}), rec(2, {"x": ["b"]})]
        self.assertIsNone(repair.cut_cycle(h))
        h = [{"n": 2, "cuts": None}] + h
        self.assertIsNone(repair.cut_cycle(h))                                   # old passes.jsonl records without cut data
        big = [rec(40, {"x": ["a"] * 40}), rec(40, {"x": ["b"] * 40}), rec(40, {"x": ["c"] * 40})]
        self.assertIsNone(repair.cut_cycle(big))

    def test_open_cuts_from_a_topology(self):
        from badpcb import cli
        b = Plan().board()
        c = cli.Ctx(b)
        cuts = repair.open_cuts(c)
        self.assertTrue(isinstance(cuts, dict))
        for h, e in cuts.items():
            self.assertTrue(e["n"] >= 1 and e["nets"])


class Ladder(unittest.TestCase):
    def test_escalation(self):
        h = [{"mode": "plain", "names": ["ADC2"]}, {"mode": "plain", "names": ["ADC4"]}, {"mode": "plain", "names": ["ADC7"]}]
        self.assertIn("--repair", repair.strategy(h, ["ADC7"], "U1", "s1"))
        self.assertIn("--fanout U1", repair.strategy(h, ["ADC7"], "U1", "s1"))
        h[-1]["mode"] = "repair"
        self.assertIn("--strip", repair.strategy(h, ["ADC7"], "U1", "s1"))
        h[-1]["mode"] = "strip"
        self.assertIn("badpcb plan s1", repair.strategy(h, ["ADC7"], None, "s1"))


class Advice(unittest.TestCase):
    def test_escape_advice_and_progress(self):
        import os
        import tempfile
        from badpcb import droute
        placed = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "key_board_placed.model.json")
        if os.path.exists(placed):
            out = repair.escape_advice(placed, ["ADC0", "ADC1", "ADC2", "ADC3", "ADC4", "ADC5", "ADC6", "ADC7"], "U1")
            for ln in out:
                self.assertIn("--strip --fanout U1", ln)
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("noise\n[3/40] Routing ADC2\n  something else\n  SUCCESS in 0.4s\n")
        self.assertIn("SUCCESS", droute.progress_line(fh.name))
        self.assertEqual(droute.progress_line("/nonexistent/x.log"), "no log yet")
        os.unlink(fh.name)


if __name__ == "__main__":
    unittest.main()
