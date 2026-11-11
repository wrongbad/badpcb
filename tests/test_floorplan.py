"""floorplan: blocks (declared + automatic), arrays, connectors and their edges, what faces a fine-pitch IC side, long links, crossings."""
import json
import unittest

from badpcb import actions as A
from badpcb import config, floorplan
from tests import synth
from tests.test_audit import P, part, qfn


def board(rough=False):
    nets = ["+3V3", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"]
    parts = [qfn("U9", 50, 25, nets)]
    parts.append(part("C1", [P("1", (48.5, 29.5), "+3V3"), P("2", (49.5, 29.5), "GND")], (49, 29.5), half=(0.8, 0.5), value="100n"))
    parts.append(part("R1", [P("1", (45, 31), "s9"), P("2", (46, 31), "xr")], (45.5, 31), half=(0.8, 0.5)))
    parts.append(part("J1", [P("1", (49, 21.2), "j1"), P("2", (51, 21.2), "j2")], (50, 21.0), half=(3.0, 1.5)))        # in U9's north fan
    parts.append(part("J2", [P("1", (95, 45), "s2")], (95, 45), half=(3.0, 2.0)))
    parts.append(part("J3", [P("1", (95, 36), "s1")], (95, 36), half=(3.0, 2.0)))
    parts.append(part("U2", [P(str(i + 1), (88 + 0.5 * i, 10), "s%d" % (i + 3)) for i in range(6)] + [P("7", (92, 10), "+3V3"), P("8", (93, 10), "GND")],
                      (90, 10), half=(2.5, 2.0), value="W25Q128"))
    for k in range(14):                                                                   # a regular 7 x 2 array of identical cells
        x, y = 10 + 6 * (k % 7), 45 + 8 * (k // 7)
        parts.append(part("K%d" % (k + 1), [P("1", (x, y), "k%d" % k), P("2", (x + 1, y), "GND")], (x, y), half=(2.0, 2.0)))
    if rough:                                                                             # everything piled on one spot
        for p in parts[1:]:
            dx, dy = 20 - p["pos"][0], 20 - p["pos"][1]
            p["pos"] = [20, 20]
            p["bbox"] = [19, 19, 21, 21]
            for pd in p["pads"]:
                pd["pos"] = [pd["pos"][0] + dx, pd["pos"][1] + dy]
    return synth.board(parts)


def run(intent=None, rough=False, depth=8.0):
    config.use({"intent": intent} if intent else {})
    w = A.World(board(rough))
    return w, floorplan.analyse(w, ["U9"], depth)


class Floorplan(unittest.TestCase):
    def tearDown(self):
        config.use({})

    def blocks(self, F):
        return {b.name: b for b in F["blocks"]}

    def test_auto_blocks(self):
        w, F = run()
        B = self.blocks(F)
        self.assertEqual(set(B), {"U9", "U2", "J1", "J2", "J3", "Kx14"})
        self.assertEqual(B["Kx14"].kind, "array")
        self.assertEqual(len(B["Kx14"].parts), 14)
        self.assertEqual(B["J1"].kind, "conn")
        self.assertEqual(B["U9"].kind, "ic")
        self.assertEqual(sorted(B["U9"].parts), ["C1", "R1", "U9"])               # decap by its supply pin, R1 by the shared net s9
        m = {b.name: F["m"][i] for i, b in enumerate(F["blocks"])}
        self.assertEqual(m["Kx14"]["parts"], 14)
        self.assertGreater(m["Kx14"]["fill"], 0.1)
        self.assertLess(m["Kx14"]["fill"], 1.0)
        self.assertAlmostEqual(m["J2"]["fill"], 1.0, places=2)

    def test_declared_blocks_first(self):
        w, F = run({"blocks": {"filt": {"parts": ["R1", "C*"]}}})
        B = self.blocks(F)
        self.assertEqual(sorted(B["filt"].parts), ["C1", "R1"])
        self.assertTrue(B["filt"].declared)
        self.assertEqual(sorted(B["U9"].parts), ["U9"])
        self.assertEqual(F["blocks"][0].name, "filt")

    def test_connectors_and_edges(self):
        w, F = run()
        c = {x["block"]: x for x in F["conn"]}
        self.assertEqual(c["J2"]["side"], "right")
        self.assertAlmostEqual(c["J2"]["mm"], 2.0, places=1)
        self.assertFalse(c["J2"]["inboard"])
        self.assertEqual(c["J1"]["side"], "back")
        self.assertTrue(c["J1"]["inboard"])

    def test_escape_facing(self):
        w, F = run()
        rows = {r["side"]: r for r in F["esc"]}
        n = rows["U9.N"]
        self.assertIn("J1", n["fan"])                                              # the connector sits in the north fan zone
        self.assertIn("J1", F["foreign"]["U9"])
        self.assertGreater(n["fan"]["J1"], 3.0)
        self.assertNotIn("J1", rows.get("U9.S", {"fan": {}})["fan"])
        txt = "\n".join(floorplan.text(F))
        self.assertIn("FOREIGN IN FAN: J1", txt)

    def test_links_and_crossings(self):
        w, F = run()
        ln = {x["block"]: x for x in F["links"]}
        self.assertTrue(ln["U2"]["long"])                                          # flash 40 mm from the MCU pins
        self.assertEqual(ln["U2"]["nets"], 6)
        self.assertEqual(ln["U2"]["kind"], "flash")
        self.assertEqual(F["x"], 1)                                                # s1 and s2 swap order on their way to J3/J2
        pairs = {(F["blocks"][i].name, F["blocks"][j].name): n for (i, j), n in F["pairs"].items()}
        self.assertEqual(pairs.get(("U9", "U2"), pairs.get(("U2", "U9"))), 6)

    def test_text_is_short_and_json_works(self):
        w, F = run({"blocks": {"filt": {"parts": ["R1"]}}, "near": [["C1", "U9", 1]]})
        txt = floorplan.text(F, "t")
        self.assertLess(len(txt), 50)
        self.assertIn("map (", "\n".join(txt))
        d = json.loads(json.dumps(floorplan.as_json(F, "t"), default=str))
        self.assertEqual(d["ratsnest_crossings"], F["x"])
        self.assertEqual(len(d["blocks"]), len(F["blocks"]))

    def test_rough_placement(self):
        w, F = run(rough=True)                                                      # all parts piled up: still a floorplan
        self.assertTrue(floorplan.text(F))
        self.assertIn("U9", self.blocks(F))

    def test_intent_shown_with_blocks(self):
        w, F = run({"blocks": {"far": {"parts": ["J2", "J3", "U2"], "max_span_mm": 5}}})
        txt = "\n".join(floorplan.text(F))
        self.assertIn("!span", txt)
        self.assertIn("intent=", txt)


if __name__ == "__main__":
    unittest.main()
