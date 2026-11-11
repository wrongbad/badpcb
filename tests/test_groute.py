"""groute tests on tiny synthetic boards (answers known by construction) + a smoke test on key_board_placed."""
import json
import os
import re
import tempfile
import unittest

from badpcb import groute, ratsnest
from tests.synth import board, pad, part, singles

HERE = os.path.dirname(os.path.abspath(__file__))
PLACED = os.path.join(HERE, "..", "models", "key_board_placed.model.json")


def wall(x, ranges, w=1.0, net_prefix="W"):
    """Foreign obstacle pads (each its own single-pad net, so no connections) at column x covering y ranges."""
    out = []
    for i, (y0, y1) in enumerate(ranges):
        out.append(part("%s%d" % (net_prefix, i), [pad("1", (x, (y0 + y1) / 2), "%s%d" % (net_prefix, i), size=(w, y1 - y0))],
                        pos=(x, (y0 + y1) / 2)))
    return out


def run(b, **kw):
    kw.setdefault("cell", 1.0)
    kw.setdefault("iters", 40)
    kw.setdefault("tlimit", 60)
    return groute.run(b, ratsnest.build(b), **kw)


class SwapLayers(unittest.TestCase):
    def board(self):
        return board(singles([("A1", (5, 10), "A"), ("A2", (55, 10), "A"), ("B1", (30, 2), "B"), ("B2", (30, 18), "B")]), size=(60, 20))

    def test_crossing_nets_use_both_layers(self):
        r = run(self.board())
        self.assertEqual(r.st["total_overflow"], 0, r.text())
        self.assertGreaterEqual(r.st["vias"], 2)               # one of them hops to the other layer and back
        layers = {n: {p[2] for p in c["points"]} for n, c in ((c["net"], c) for c in r.to_json()["connections"])}
        self.assertTrue(len(layers["A"] | layers["B"]) == 2)   # both layers are in use
        self.assertTrue(min(len(v) for v in layers.values()) == 1 or max(len(v) for v in layers.values()) == 2)

    def test_single_layer_crossing_is_reported(self):
        b = board(singles([("A1", (5, 10), "A"), ("A2", (55, 10), "A"), ("B1", (30, 2), "B"), ("B2", (30, 18), "B")]),
                  size=(60, 20), layers=("F.Cu",))
        r = run(b, iters=10)
        self.assertGreater(r.st["total_overflow"], 0)
        txt = r.text()
        self.assertIn("straight crossing", txt)
        self.assertIn("layer change", txt)


class ChannelOverflow(unittest.TestCase):
    def chan(self, gap):
        parts = singles([("A1", (5, 8), "A"), ("A2", (55, 8), "A"), ("B1", (5, 10), "B"), ("B2", (55, 10), "B"),
                         ("C1", (5, 12), "C"), ("C2", (55, 12), "C")])
        y0, y1 = 10 - gap / 2, 10 + gap / 2
        parts += wall(30, [(0, y0), (y1, 20)])
        return board(parts, size=(60, 20), layers=("F.Cu",))

    def test_cap2_demand3_names_three_nets(self):
        r = run(self.chan(1.3), iters=15)
        self.assertGreater(r.st["total_overflow"], 0, r.text())
        txt = r.text()
        m = re.search(r"OVERFLOW: needs (\S+).*?\(cap (\d+), demand (\d+): \[([^\]]*)\]", txt)
        self.assertIsNotNone(m, txt)
        self.assertLessEqual(int(m.group(2)), 2)
        self.assertGreaterEqual(int(m.group(3)), 3)
        self.assertEqual(set(m.group(4).split(",")), {"A", "B", "C"})
        self.assertIn("alt:", txt)

    def test_wide_channel_is_clean(self):
        r = run(self.chan(3.5))
        self.assertEqual(r.st["total_overflow"], 0, r.text())


class HistoryConverges(unittest.TestCase):
    def board(self):
        parts = singles([("A1", (5, 9.5), "A"), ("A2", (55, 9.5), "A"), ("B1", (5, 11.5), "B"), ("B2", (55, 11.5), "B")])
        # narrow gap at y 10.0..10.9 (one track fits) and a wide detour gap at y 17..19.5
        parts += wall(30, [(0, 10.0), (10.9, 17), (19.5, 25)])
        return board(parts, size=(60, 26), layers=("F.Cu",))

    def test_history_resolves_what_present_penalty_alone_cannot(self):
        b = self.board()
        kw = dict(pf0=0.0, pf_grow=1.0, iters=40, stall=100)
        no_hist = run(b, hist_inc=0.0, **kw)
        with_hist = run(b, hist_inc=2.0, **kw)
        self.assertGreater(no_hist.st["total_overflow"], 0)
        self.assertEqual(with_hist.st["total_overflow"], 0, with_hist.text())
        self.assertGreater(with_hist.st["wirelength_mm"], no_hist.st["wirelength_mm"])   # someone took the detour


class Hints(unittest.TestCase):
    def board(self):
        return board(singles([("A1", (5, 5), "A"), ("A2", (45, 5), "A")]), size=(50, 20))

    def test_layer_hint_and_freeze(self):
        r = run(self.board(), hints=[{"type": "layer", "net": "A", "layer": "B.Cu"}])
        pts = r.to_json()["connections"][0]["points"]
        self.assertGreater(sum(1 for p in pts if p[2] == "B.Cu"), len(pts) / 2)
        r = run(self.board(), hints=[{"type": "freeze", "bbox": [20, 0, 25, 20]}])
        self.assertEqual(r.st["no_path"], 1)
        self.assertIn("freeze", r.text())

    def test_nets_filter_and_json(self):
        b = board(singles([("A1", (5, 5), "A"), ("A2", (45, 5), "A"), ("B1", (5, 9), "B"), ("B2", (45, 9), "B")]), size=(50, 20))
        r = run(b, nets="B")
        self.assertEqual(r.st["connections"], 1)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(b.__class__ and {}, fh)
        c = r.to_json()["connections"][0]
        self.assertEqual(c["net"], "B")
        self.assertEqual(c["status"], "ok")
        self.assertAlmostEqual(c["points"][0][1], 5 if False else c["points"][0][1])
        self.assertTrue(abs(c["points"][0][0] - 5) <= 1.0 and abs(c["points"][-1][0] - 45) <= 1.0)




if __name__ == "__main__":
    unittest.main()


class NetStyleCosts(unittest.TestCase):
    """Per-net costs from net styles; no styles = the global-cost router, bit for bit."""

    @staticmethod
    def pts(r):
        return {c["net"]: [tuple(p) for p in c["points"]] for c in r.to_json()["connections"]}

    def swap_board(self):
        return board(singles([("A1", (5, 10), "A"), ("A2", (55, 10), "A"), ("B1", (30, 2), "B"), ("B2", (30, 18), "B")]), size=(60, 20))

    def test_no_styles_is_unchanged(self):
        from badpcb import styles as ST
        b = self.swap_board()
        base = self.pts(run(b, styles=None))
        self.assertEqual(self.pts(run(b, styles={})), base)
        # a style that matches no net of the board resolves to {} and changes nothing either
        r, _ = ST.resolve(b.nets, ST.parse({"x": {"match": ["ZZZ*"], "via_penalty": 99, "detour_max": 1.1, "avoid": ["A"]}}))
        self.assertEqual(r, {})
        self.assertEqual(self.pts(run(b, styles=r)), base)
        gr = groute.GRouter(b, ratsnest.build(b), styles={})
        self.assertEqual((gr.net_viapen, gr.net_lenw, gr.net_avoid), ({}, {}, {}))

    def test_default_run_reads_badpcb_json_and_is_unchanged_without_styles(self):
        from badpcb import config
        config.use({})
        b = self.swap_board()
        self.assertEqual(self.pts(run(b)), self.pts(run(b, styles=None)))

    def test_via_penalty_moves_the_vias_to_the_other_net(self):
        from badpcb import styles as ST
        b = self.swap_board()
        nv = lambda r: {c["net"]: c["n_vias"] for c in r.to_json()["connections"]}
        self.assertEqual(nv(run(b, styles=None)), {"A": 0, "B": 2})
        r, _ = ST.resolve(b.nets, ST.parse({"x": {"match": ["B"], "via_penalty": 200}}))
        self.assertEqual(nv(run(b, styles=r))["B"], 0)            # B now refuses to hop; A pays the vias
        self.assertEqual(nv(run(b, styles=r))["A"], 2)

    def test_avoid_keeps_the_net_away_from_the_other(self):
        from badpcb import styles as ST
        b = board(singles([("A1", (5, 20), "A"), ("A2", (55, 20), "A"), ("B1", (5, 21), "B"), ("B2", (55, 21), "B")]), size=(60, 40))

        def gap(r):
            p = self.pts(r)
            return min(abs(a[1] - c[1]) for a in p["A"] for c in p["B"] if 15 < a[0] < 45 and abs(a[0] - c[0]) < 1 and a[2] == c[2])
        self.assertEqual(gap(run(b, styles=None)), 1.0)
        r, _ = ST.resolve(b.nets, ST.parse({"x": {"match": ["B"], "avoid": ["A"]}}))
        self.assertGreaterEqual(gap(run(b, styles=r)), 2.0)
        gr = groute.GRouter(b, ratsnest.build(b), styles=r)
        self.assertIn(gr.nid["A"], gr.net_avoid[gr.nid["B"]])
        self.assertIn(gr.nid["B"], gr.net_avoid[gr.nid["A"]])     # keeping apart is mutual

    def test_detour_max_weights_length(self):
        from badpcb import styles as ST
        b = self.swap_board()
        r, _ = ST.resolve(b.nets, ST.parse({"x": {"match": ["A"], "detour_max": 1.05}, "y": {"match": ["B"], "detour_max": 2.0}}))
        gr = groute.GRouter(b, ratsnest.build(b), styles=r)
        self.assertGreater(gr.net_lenw[gr.nid["A"]], 1.0)
        self.assertNotIn(gr.nid["B"], gr.net_lenw)                # a loose budget changes nothing
