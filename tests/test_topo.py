import unittest

from rk import ratsnest, swaps, topo
from tests.synth import board, pad, part, singles, via


def two_nets_cross(size=(60, 20)):
    return board(singles([("A1", (5, 5), "A"), ("A2", (55, 15), "A"), ("B1", (5, 15), "B"), ("B2", (55, 5), "B")]), size=size)


class TopoTest(unittest.TestCase):
    def test_grid_and_cuts(self):
        T = topo.build(two_nets_cross(), ratsnest.build(two_nets_cross()), cell=20)
        self.assertEqual(list(T.regions), ["R1", "R2", "R3"])
        self.assertEqual(list(T.cuts), ["C1", "C2"])
        self.assertEqual((T.cuts["C1"].a, T.cuts["C1"].b), ("R1", "R2"))

    def test_crossing_matrix_and_clusters(self):
        b = two_nets_cross()
        T = topo.build(b, ratsnest.build(b), cell=20)
        nets, M = T.matrix()
        self.assertEqual(M, {("A", "B"): 1})
        self.assertEqual(len(T.crossings), 1)
        self.assertEqual(T.region_at(T.crossings[0][2]).id, "R2")
        self.assertEqual(T.clusters(min_w=1)[0]["nets"], ["A", "B"])

    def test_cut_demand_order_and_inversion(self):
        b = two_nets_cross()
        T = topo.build(b, ratsnest.build(b), cell=20)
        self.assertEqual(T.demand("C1"), 2)
        c1 = [n for n, _, _ in T.cut_nets("C1")]
        c2 = [n for n, _, _ in T.cut_nets("C2")]
        self.assertEqual(c1, c2[::-1])                      # order flips between the cuts
        inv = T.inversions()
        self.assertEqual(inv[0]["inversions"], 1)
        self.assertEqual(inv[0]["pairs"], [("A", "B")])
        self.assertEqual(T.metrics()["inversions"], 1)

    def test_no_inversion_for_parallel_nets(self):
        b = board(singles([("A1", (5, 5), "A"), ("A2", (55, 5), "A"), ("B1", (5, 15), "B"), ("B2", (55, 15), "B")]), size=(60, 20))
        T = topo.build(b, ratsnest.build(b), cell=20)
        self.assertEqual(T.crossings, [])
        self.assertEqual(T.inversions(), [])

    def test_capacity(self):
        b = two_nets_cross()
        T = topo.build(b, ratsnest.build(b), cell=20)
        # cut x=20 spans y 0..20, both ends on the board edge: free 20 - 2*(0.3+0.1) = 19.2 mm, pitch 0.4 -> 49 per layer
        self.assertEqual(T.cuts["C1"].cap, {"F.Cu": 49, "B.Cu": 49})
        b2 = board(singles([("A1", (5, 5), "A"), ("A2", (55, 15), "A")]) + [part("V", [pad("1", (80, 80), "")], (80, 80))],
                   vias=[via((20, 10), "Z")], size=(60, 20))
        T2 = topo.build(b2, ratsnest.build(b2), cell=20)
        # via blocks centre line +-(0.3 + 0.2 + 0.1)=0.6 around y=10: two free runs of 9.0 mm -> 23 each
        self.assertEqual(T2.cuts["C1"].cap["F.Cu"], 46)
        self.assertLess(T2.cuts["C1"].cap["B.Cu"], 49)

    def test_ring_around_big_part(self):
        pads = [pad(str(i), (30 + i * 0.4, 10), "N%d" % i) for i in range(20)]
        big = part("U1", pads, pos=(34, 10), bbox=[29, 5, 39, 15])
        b = board([big] + singles([("X%d" % i, (5 + i, 18), "N%d" % i) for i in range(20)]))
        T = topo.build(b, ratsnest.build(b), cell=25)
        self.assertIn("U1", T.rings)
        ring = [c for c in T.cuts.values() if c.label.startswith("U1 ring")]
        self.assertEqual(len(ring), 4)

    def test_plane_layers_not_signal(self):
        from .synth import zone
        b = board(singles([("A", (5, 5), "A"), ("B", (55, 15), "A")]), zones=[zone("GND", "In1.Cu", (60, 20))],
                  size=(60, 20), layers=("F.Cu", "In1.Cu", "B.Cu"))
        T = topo.build(b, ratsnest.build(b), cell=20)
        self.assertEqual(set(T.cuts["C1"].cap), {"F.Cu", "B.Cu"})


class SwapTest(unittest.TestCase):
    def setUp(self):
        names = {"1": "GPIO4", "2": "GPIO5"}
        for i in range(8):
            names[str(10 + i)] = "GPIO%d/ADC%d" % (40 + i, i)
        self.cfg = {"mcu": "U1", "pin_names": names, "groups": [
            {"id": "adc", "kind": "pin_permutation", "desc": "any permutation", "nets": ["ADC%d" % i for i in range(8)],
             "pin_names": ["GPIO%d/ADC%d" % (40 + i, i) for i in range(8)]},
            {"id": "keys", "kind": "key_assignment", "desc": "", "net_regex": "^EN\\d+$", "n_keys": 58, "n_regions": 8, "n_en": 8, "max_region_size": 8}]}

    def test_key_assignment(self):
        ok = {k: (k % 8, k // 8) for k in range(58)}
        self.assertEqual(swaps.check_key_assignment(self.cfg, ok), [])
        bad = dict(ok)
        bad[1] = bad[2]
        self.assertTrue(swaps.check_key_assignment(self.cfg, bad))

    def test_pin_group_legality(self):
        names = self.cfg["pin_names"]
        nums = {nm: n for n, nm in names.items()}
        pads = [pad(nums["GPIO%d/ADC%d" % (40 + i, i)], (30 + i, 10), "ADC%d" % i) for i in range(8)]
        pads += [pad(nums["GPIO4"], (30, 20), ""), pad(nums["GPIO5"], (31, 20), "SPARE")]
        b = board([part("U1", pads, pos=(34, 14), bbox=[28, 8, 42, 22])])
        G = swaps.groups(b, self.cfg)["adc"]
        rot = {n: G.current["ADC%d" % ((int(n[3:]) + 1) % 8)] for n in G.nets}
        self.assertEqual(swaps.check(b, self.cfg, "adc", rot), [])
        b2 = swaps.apply(b, self.cfg, "adc", rot)
        self.assertEqual(b2.parts["U1"].pads[0].net, "ADC7")
        self.assertEqual(b.parts["U1"].pads[0].net, "ADC0")            # original untouched
        bad = dict(rot)
        bad["ADC0"] = nums["GPIO4"]
        self.assertTrue(swaps.check(b, self.cfg, "adc", bad))
        dup = dict(G.current)
        dup["ADC1"] = dup["ADC0"]
        self.assertTrue(any("same pin" in e for e in swaps.check(b, self.cfg, "adc", dup)))


if __name__ == "__main__":
    unittest.main()
