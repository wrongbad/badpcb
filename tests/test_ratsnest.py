import unittest

from badpcb import ratsnest
from tests.synth import board, pad, part, singles, track, via, zone


class RatsnestTest(unittest.TestCase):
    def test_mst_between_pads(self):
        b = board(singles([("A", (10, 10), "N"), ("B", (20, 10), "N"), ("C", (80, 10), "N")]))
        rn = ratsnest.build(b)
        self.assertEqual(len(rn.conns), 2)
        self.assertAlmostEqual(sorted(c.length for c in rn.conns)[0], 10)
        self.assertAlmostEqual(sorted(c.length for c in rn.conns)[1], 60)

    def test_existing_copper_merges_islands(self):
        b = board(singles([("A", (10, 10), "N"), ("B", (20, 10), "N"), ("C", (80, 10), "N")]),
                  tracks=[track((20, 10), (80, 10), "N")])
        rn = ratsnest.build(b)
        self.assertEqual(len(rn.islands["N"]), 2)
        self.assertEqual(len(rn.conns), 1)
        self.assertAlmostEqual(rn.conns[0].length, 10)

    def test_wrong_layer_does_not_connect(self):
        b = board(singles([("A", (10, 10), "N"), ("B", (20, 10), "N")]), tracks=[track((10, 10), (20, 10), "N", layer="B.Cu")])
        rn = ratsnest.build(b)
        self.assertEqual(len(rn.islands["N"]), 3)       # pads stay separate; the B.Cu track is a pad-less relay island

    def test_via_joins_layers(self):
        parts = [part("A", [pad("1", (10, 10), "N")], (10, 10)), part("B", [pad("1", (30, 10), "N", layers=("B.Cu",))], (30, 10))]
        tr = [track((10, 10), (20, 10), "N"), track((20, 10), (30, 10), "N", layer="B.Cu")]
        rn = ratsnest.build(board(parts, tr, [via((20, 10), "N")]))
        self.assertEqual(len(rn.conns), 0)
        rn = ratsnest.build(board(parts, tr))      # without the via they stay apart
        self.assertEqual(len(rn.conns), 1)

    def test_plane_net_excluded(self):
        thru = pad("1", (10, 10), "GND", layers=("F.Cu", "In1.Cu", "B.Cu"), drill=0.3)
        parts = [part("A", [thru], (10, 10))] + singles([("B", (20, 10), "GND"), ("C", (30, 10), "GND")])
        b = board(parts, tracks=[track((30, 10), (35, 10), "GND")], vias=[via((35, 10), "GND")],
                  zones=[zone("GND", "In1.Cu")], layers=("F.Cu", "In1.Cu", "B.Cu"))
        rn = ratsnest.build(b)
        self.assertEqual(rn.conns, [])
        self.assertEqual(rn.plane_nets["GND"], (2, 1))          # A (thru pad) and C (stub+via) served; B not
        self.assertEqual([I.pads for I in rn.stubs], [["B.1"]])

    def test_via_outside_zone_not_served(self):
        b = board(singles([("A", (10, 10), "P")]), vias=[via((10, 10), "P")],
                  zones=[{"layers": ["In1.Cu"], "net": "P", "priority": 0,
                          "polys": [{"outer": [[50, 0], [100, 0], [100, 50], [50, 50]], "holes": []}]}],
                  layers=("F.Cu", "In1.Cu", "B.Cu"))
        self.assertEqual(len(ratsnest.build(b).stubs), 1)

    def test_copper_relay_and_pruned_leaf(self):
        # lane copper between two far pads acts as a relay; a second dangling lane is pruned
        parts = singles([("A", (10, 10), "N"), ("B", (90, 10), "N")])
        tr = [track((10, 20), (90, 20), "N"), track((40, 40), (60, 40), "N")]
        rn = ratsnest.build(board(parts, tr))
        self.assertEqual(len(rn.islands["N"]), 4)
        self.assertEqual(len(rn.conns), 2)                      # A->lane, B->lane
        self.assertAlmostEqual(sum(c.length for c in rn.conns), 20)
        self.assertTrue(all("N~" in c.a + c.b for c in rn.conns))

    def test_same_pad_number_merges(self):
        p1 = pad("1", (10, 10), "N")
        p2 = pad("1", (10, 10), "N", layers=("F.Cu", "B.Cu"), size=(0.6, 0.6), drill=0.3)
        parts = [part("A", [p1, p2], (10, 10))] + singles([("B", (30, 10), "N")])
        rn = ratsnest.build(board(parts))
        self.assertEqual(len(rn.islands["N"]), 2)


if __name__ == "__main__":
    unittest.main()
