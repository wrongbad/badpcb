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
