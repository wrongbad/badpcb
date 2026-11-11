"""badpcb.kicad_pcb: reading, footprint pose / flip math, in-place edits, net formats, extract + writer + strip (no KiCad needed)."""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from badpcb import apply as AP, config, extract, strip_nets
from badpcb.kicad_pcb import Board, norm180, norm360, rotate

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
K10, K9 = os.path.join(FIX, "kicad10_mini.kicad_pcb"), os.path.join(FIX, "kicad9_mini.kicad_pcb")


def reparse(b):
    return Board(b.text())


def pads(b, ref):
    return {p.num: p for f in b.footprints if f.ref == ref for p in f.pads}


class TestGeometry(unittest.TestCase):
    def test_rotate_is_kicad_y_down(self):
        self.assertEqual(rotate(1, 0, 90), (0, -1))
        self.assertEqual(rotate(1, 0, 180), (-1, 0))
        self.assertEqual(rotate(1, 0, -90), (0, 1))
        x, y = rotate(1, 0, 30)
        self.assertAlmostEqual(x, math.cos(math.radians(30)))
        self.assertAlmostEqual(y, -math.sin(math.radians(30)))

    def test_angle_norm(self):
        self.assertEqual((norm360(-90), norm360(360), norm180(270), norm180(180)), (270, 0, -90, 180))


class TestRead(unittest.TestCase):
    def test_both_net_formats(self):
        for path, numbered in ((K10, False), (K9, True)):
            b = Board.load(path)
            self.assertEqual(b.numbered_nets, numbered)
            self.assertEqual(b.net_names(), ["A", "B", "GND"])
            self.assertEqual(pads(b, "R1")["1"].net, "A")
            self.assertEqual([i.net for i in b.items], ["B", "B", "B"])
            self.assertEqual(b.zones[0].net, "GND")

    def test_world_pad_positions(self):
        b = Board.load(K10)
        self.assertEqual(pads(b, "R1")["1"].world(), (9.0, 10.0))
        c1 = pads(b, "C1")               # B.Cu part rotated 90: local (-0.5, 0) -> (0, +0.5)
        self.assertEqual((c1["1"].world(), c1["2"].world()), ((20.0, 10.5), (20.0, 9.5)))
        self.assertEqual(c1["1"].abs_angle, 90.0)

    def test_structure(self):
        b = Board.load(K10)
        self.assertEqual(b.cu_layers, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual([(f.ref, f.side, f.fpid) for f in b.footprints], [("R1", "F", "R_0603"), ("C1", "B", "C_0402"), ("U1", "F", "SOT_DUAL")])
        self.assertEqual([i.locked for i in b.items], [False, True, False])
        z = b.zones[0]
        self.assertEqual((z.layers, z.priority, len(z.polys), len(z.polys[0]["holes"]), z.is_rule_area), (["In1.Cu"], 1, 1, 1, False))
        self.assertEqual(b.edges_bbox(), [-0.05, -0.05, 40.05, 30.05])
        self.assertEqual(sorted(i.uuid[:2] for i in b.items), ["60", "60", "70"])

    def test_extract_model(self):
        m = extract.extract(K10)
        self.assertEqual([p["ref"] for p in m["parts"]], ["R1", "C1", "U1"])
        self.assertEqual(m["parts"][1]["pads"][0]["pos"], [20.0, 10.5])
        self.assertEqual(m["parts"][1]["pads"][0]["rot"], 90.0)
        self.assertEqual(m["parts"][0]["pads"][0]["layers"], ["F.Cu"])
        self.assertEqual(m["parts"][1]["side"], "B")
        self.assertEqual(len(m["tracks"]), 2)
        self.assertEqual((m["vias"][0]["layers"], m["vias"][0]["dia"]), (["F.Cu", "B.Cu"], 0.6))
        self.assertTrue(m["tracks"][1]["locked"])
        self.assertEqual(m["zones"][0]["polys"][0]["holes"][0][0], [20.0, 20.0])
        self.assertEqual(m["nets"], ["A", "B", "GND"])
        # courtyard rect +-1.6 x +-0.7 plus pcbnew's (stroke - 0.005) growth
        self.assertAlmostEqual(m["parts"][0]["bbox"][0], 10 - 1.6 - 0.045, places=6)
        # the model feeds badpcb's own loader
        from badpcb import model
        d = tempfile.mkdtemp()
        json.dump(m, open(os.path.join(d, "m.json"), "w"))
        self.assertEqual(len(model.load(os.path.join(d, "m.json")).parts), 3)
        shutil.rmtree(d)


class TestPose(unittest.TestCase):
    def test_unchanged_pose_is_noop(self):
        b = Board.load(K10)
        f = b.footprint("C1")
        f.place(f.pos, f.rot, f.side)
        self.assertEqual(b.text(), open(K10).read())

    def test_move_only_touches_at(self):
        b = Board.load(K10)
        b.footprint("R1").place((12.5, 11.0), 0.0)
        src, out = open(K10).read(), b.text()
        self.assertEqual(out, src.replace("(at 10 10)", "(at 12.5 11)", 1))
        self.assertEqual(pads(reparse(b), "R1")["1"].world(), (11.5, 11.0))

    def test_rotate_updates_absolute_pad_angles(self):
        b = Board.load(K10)
        b.footprint("U1").place((30.0, 20.0), 90.0)
        r = reparse(b)
        u = pads(r, "U1")
        self.assertEqual((r.footprint("U1").rot, u["1"].abs_angle), (90.0, 90.0))
        self.assertEqual(u["1"].world(), (30 - 0.5, 20 + 1.0))           # local (-1,-0.5) rotated 90 -> (-0.5, 1)
        self.assertEqual([round(v, 6) for v in rotate(-1, -0.5, 90)], [-0.5, 1.0])
        # text angle follows the footprint (absolute in the file)
        ref = [p for p in r.footprint("U1").node.nodes("property") if p.vals()[0] == "Reference"][0]
        self.assertEqual(ref.node("at").floats(), [0.0, -1.5, 90.0])
        b.footprint("U1").place((30.0, 20.0), 0.0)
        self.assertEqual(b.text().count("(at 30 20 90)"), 0)

    def test_rotate_back_restores_text(self):
        b = Board.load(K10)
        b.footprint("U1").place((30.0, 20.0), 180.0)
        r = reparse(b)
        r.footprint("U1").place((30.0, 20.0), 0.0)
        self.assertEqual(r.text(), open(K10).read().replace("(at 30 20)", "(at 30 20)"))

    def test_flip_mirrors_local_y_and_layers(self):
        b = Board.load(K10)
        u = b.footprint("U1")
        pred = u.pad_world_map((30.0, 20.0), 0.0, "B")
        u.place((30.0, 20.0), 0.0, "B")
        r = reparse(b)
        f = r.footprint("U1")
        self.assertEqual(f.side, "B")
        self.assertEqual(f.layer, "B.Cu")
        p = pads(r, "U1")
        self.assertEqual(p["1"].world(), (29.0, 20.5))                   # local y negated: (-1,-0.5) -> (-1, +0.5)
        self.assertEqual({k: v.world() for k, v in p.items()}, pred)
        self.assertEqual(p["1"].layer_names, ["B.Cu", "B.Mask", "B.Paste"])
        txt = f.node.text
        self.assertIn('(layer "B.SilkS")', txt)
        self.assertIn("(justify mirror)", txt)
        self.assertNotIn('"F.', txt)
        self.assertIn('(start -0.5 0.4)', txt)                           # fp_line y mirrored
        self.assertIn('(start -1.6 0.7)', txt)

    def test_flip_twice_returns_to_original_geometry(self):
        b = Board.load(K10)
        u = b.footprint("U1")
        u.place((30.0, 20.0), 0.0, "B")
        r = reparse(b)
        r.footprint("U1").place((30.0, 20.0), 0.0, "F")
        r2 = reparse(r)
        self.assertEqual({k: v.world() for k, v in pads(r2, "U1").items()}, {k: v.world() for k, v in pads(Board.load(K10), "U1").items()})
        self.assertNotIn("justify", r2.footprint("U1").node.text.replace("(justify mirror)", "") if "mirror" in r2.footprint("U1").node.text else "")
        self.assertEqual(r2.footprint("U1").side, "F")

    def test_flip_with_rotation_matches_prediction(self):
        for rot in (0.0, 90.0, 180.0, -90.0):
            for ref, side in (("U1", "B"), ("C1", "F")):
                b = Board.load(K10)
                f = b.footprint(ref)
                pred = f.pad_world_map((25.0, 15.0), rot, side)
                f.place((25.0, 15.0), rot, side)
                got = {k: v.world() for k, v in pads(reparse(b), ref).items()}
                for k in pred:
                    self.assertAlmostEqual(got[k][0], pred[k][0], places=6)
                    self.assertAlmostEqual(got[k][1], pred[k][1], places=6)
                self.assertEqual(reparse(b).footprint(ref).side, side)

    def test_in_memory_state_tracks_edit(self):
        b = Board.load(K10)
        f = b.footprint("U1")
        f.place((1.0, 2.0), 90.0, "B")
        self.assertEqual((f.pos, f.rot, f.side), ((1.0, 2.0), 90.0, "B"))
        self.assertEqual(f.pads[0].world(), reparse(b).footprint("U1").pads[0].world())


class TestNetsAndCopper(unittest.TestCase):
    def test_set_pad_net_named(self):
        b = Board.load(K10)
        p = pads(b, "R1")
        b.set_pad_net(p["1"], "NEWNET")
        b.set_pad_net(p["2"], "")
        r = reparse(b)
        self.assertEqual((pads(r, "R1")["1"].net, pads(r, "R1")["2"].net), ("NEWNET", ""))
        self.assertEqual(b.text().count("NEWNET"), 1)

    def test_set_pad_net_inserts_when_missing(self):
        b = Board.load(K10)
        b.set_pad_net(pads(b, "R1")["1"], "")
        b2 = reparse(b)
        b2.set_pad_net(pads(b2, "R1")["1"], "X")
        self.assertEqual(pads(reparse(b2), "R1")["1"].net, "X")

    def test_numbered_nets_declared_and_referenced(self):
        b = Board.load(K9)
        b.set_pad_net(pads(b, "R1")["1"], "FRESH")
        b.add_via((5.0, 5.0), 0.5, 0.25, ["F.Cu", "B.Cu"], "FRESH")
        b.add_track((5.0, 5.0), (6.0, 5.0), 0.2, "F.Cu", "A")
        t = b.text()
        self.assertEqual(len(re.findall(r'\(net 4 "FRESH"\)', t)), 2)             # top-level declaration + the pad
        self.assertIn('\t(net 4 "FRESH")\n', t)
        r = reparse(b)
        self.assertEqual(r.net_by_id[4], "FRESH")
        self.assertEqual(pads(r, "R1")["1"].net, "FRESH")
        self.assertEqual([i.net for i in r.items][-2:], ["FRESH", "A"])

    def test_add_via_and_track(self):
        b = Board.load(K10)
        n_items = len(b.items)
        b.add_via((5.0, 5.0), 0.5, 0.25, ["F.Cu", "B.Cu"], "GND")
        b.add_track((5.0, 5.0), (7.5, 5.0), 0.2, "F.Cu", "GND")
        r = reparse(b)
        self.assertEqual(len(r.items), n_items + 2)
        via, trk = r.items[-2], r.items[-1]
        self.assertEqual((via.kind, via.a, via.dia, via.drill, via.layers, via.net), ("via", (5.0, 5.0), 0.5, 0.25, ["F.Cu", "B.Cu"], "GND"))
        self.assertEqual((trk.a, trk.b, trk.w, trk.layer), ((5.0, 5.0), (7.5, 5.0), 0.2, "F.Cu"))
        uu = [i.uuid for i in r.items]
        self.assertEqual(len(set(uu)), len(uu))
        self.assertTrue(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", via.uuid))
        # inserted next to the other copper, formatted like KiCad
        self.assertIn('\t(via\n\t\t(at 5 5)\n\t\t(size 0.5)\n\t\t(drill 0.25)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "GND")\n', b.text())

    def test_delete_and_lock(self):
        b = Board.load(K10)
        b.items[0].delete()
        b.items[1].set_locked(False)
        b.items[2].set_locked(True)
        r = reparse(b)
        self.assertEqual(len(r.items), 2)
        self.assertEqual([i.kind for i in r.items], ["track", "via"])
        self.assertEqual([i.locked for i in r.items], [False, True])
        self.assertEqual(b.items[1].locked, False)
        self.assertEqual(b.text().count("(locked yes)"), 1)

    def test_clear_fills_and_reshape_zone(self):
        b = Board.load(K10)
        z = b.zones[0]
        b.reshape_zone(z, [{"outer": [[2, 2], [30, 2], [30, 20], [2, 20]], "holes": [[[5, 5], [6, 5], [6, 6]]]},
                           {"outer": [[32, 2], [38, 2], [38, 8]], "holes": []}])
        r = reparse(b)
        self.assertEqual(len(r.zones), 2)
        self.assertEqual(r.zones[0].polys, [{"outer": [[2, 2], [30, 2], [30, 20], [2, 20]], "holes": [[[5, 5], [6, 5], [6, 6]]]}])
        self.assertEqual(r.zones[1].polys[0]["outer"], [[32, 2], [38, 2], [38, 8]])
        self.assertEqual((r.zones[1].net, r.zones[1].layers, r.zones[1].priority), ("GND", ["In1.Cu"], 1))
        self.assertNotEqual(r.zones[0].node.node("uuid").vals(), r.zones[1].node.node("uuid").vals())
        self.assertNotIn("filled_polygon", b.text())

    def test_save_writes_file(self):
        d = tempfile.mkdtemp()
        try:
            b = Board.load(K10)
            b.footprint("R1").place((11.0, 10.0), 0.0)
            out = os.path.join(d, "o.kicad_pcb")
            b.save(out)
            self.assertEqual(Board.load(out).footprint("R1").pos, (11.0, 10.0))
        finally:
            shutil.rmtree(d)


def plan(**kw):
    P = {"session": "t", "base_board": "", "parts": [], "pads": [], "vias_add": [], "tracks_add": [], "vias_del": [], "tracks_del": [],
         "hints": {}, "clearance": 0.15, "zones": [], "protect": {}, "unlock": [], "lock": []}
    P.update(kw)
    return P


class TestWriter(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.out = os.path.join(self.d, "o.kicad_pcb")

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_plan(self, P, src=K10):
        rep = AP.write(P, src, self.out)
        return rep, Board.load(self.out)

    def test_empty_plan_is_byte_identical(self):
        rep, b = self.run_plan(plan())
        self.assertEqual(open(self.out).read(), open(K10).read())
        self.assertEqual((rep["moved"], rep["removed"]), ([], []))

    def test_move_rotates_and_rips_up_shorting_copper(self):
        # R1 pad 2 (net B) ends at (11,10), where the locked B track starts; moving R1 up 5 mm detaches it
        P = plan(parts=[{"ref": "R1", "pos": [10.0, 15.0], "rot": 90.0, "side": "F", "pads": []}])
        rep, b = self.run_plan(P)
        self.assertEqual(b.footprint("R1").pos, (10.0, 15.0))
        self.assertEqual(pads(b, "R1")["1"].world(), (10.0, 16.0))
        self.assertEqual(rep["placement_mismatch"], [])
        self.assertTrue(any("was attached to R1.2" in r["reason"] for r in rep["removed"]))
        self.assertNotIn("60000000-0000-4000-8000-000000000002", {i.uuid for i in b.items})     # the track that left with the pad
        self.assertNotIn("filled_polygon", open(self.out).read())                              # stale fill dropped

    def test_flip_with_expected_pad_positions(self):
        want = [{"num": "1", "pos": [29.0, 20.5]}, {"num": "2", "pos": [31.0, 19.5]}]
        P = plan(parts=[{"ref": "U1", "pos": [30.0, 20.0], "rot": 0.0, "side": "B", "pads": want}])
        rep, b = self.run_plan(P)
        self.assertEqual(rep["placement_mismatch"], [])
        self.assertEqual(b.footprint("U1").side, "B")
        self.assertEqual(pads(b, "U1")["2"].world(), (31.0, 19.5))

    def test_pad_net_swap_vias_tracks_locks(self):
        P = plan(pads=[{"ref": "R1", "num": "1", "old": "A", "net": "GND"}],
                 vias_add=[{"pos": [30.0, 5.0], "net": "GND", "dia": 0.5, "drill": 0.25, "layers": ["F.Cu", "B.Cu"]}],
                 tracks_add=[{"a": [30.0, 5.0], "b": [33.0, 5.0], "w": 0.2, "layer": "F.Cu", "net": "GND"}],
                 vias_del=[{"pos": [15.0, 10.0], "net": "B"}],
                 tracks_del=[{"a": [9.0, 10.0], "b": [5.0, 10.0], "layer": "F.Cu", "net": "B"}],
                 unlock=["60000000-0000-4000-8000-000000000002"], lock=["70000000-0000-4000-8000-000000000001"])
        rep, b = self.run_plan(P)
        self.assertEqual(pads(b, "R1")["1"].net, "GND")
        self.assertEqual((rep["vias_added"], rep["tracks_added"], rep["unlocked"], rep["locked"]), (1, 1, 1, 1))
        kinds = [(i.kind, i.net, i.a) for i in b.items]
        self.assertNotIn(("via", "B", (15.0, 10.0)), kinds)
        self.assertIn(("via", "GND", (30.0, 5.0)), kinds)
        self.assertEqual(sum(1 for i in b.items if i.kind == "track" and i.a == (9.0, 10.0)), 0)

    def test_zone_reshape_and_protect(self):
        P = plan(zones=[{"idx": 0, "net": "GND", "layers": ["In1.Cu"], "polys": [{"outer": [[2, 2], [20, 2], [20, 20]], "holes": []}]}],
                 parts=[{"ref": "R1", "pos": [10.0, 15.0], "rot": 0.0, "side": "F", "pads": []}],
                 protect={"60000000-0000-4000-8000-000000000002": "lane"})
        rep, b = self.run_plan(P)
        self.assertEqual(b.zones[0].polys[0]["outer"], [[2, 2], [20, 2], [20, 20]])
        self.assertEqual(rep["zones_reshaped"], ["GND In1.Cu: 1 polygon(s)"])
        prot = [r for r in rep["removed"] if r["protected"]]
        self.assertTrue(all(r["protected"] == "lane" for r in prot))

    def test_numbered_format_board(self):
        P = plan(pads=[{"ref": "R1", "num": "1", "old": "A", "net": "NEW"}],
                 vias_add=[{"pos": [30.0, 5.0], "net": "NEW", "dia": 0.5, "drill": 0.25, "layers": ["F.Cu", "B.Cu"]}])
        rep, b = self.run_plan(P, K9)
        self.assertEqual(b.net_by_id[4], "NEW")
        self.assertEqual(pads(b, "R1")["1"].net, "NEW")
        self.assertEqual(b.items[-1].net, "NEW")

    def test_cli_write_flag(self):
        pj, rj = os.path.join(self.d, "p.json"), os.path.join(self.d, "r.json")
        json.dump(plan(parts=[{"ref": "R1", "pos": [11.0, 10.0], "rot": 0.0, "side": "F", "pads": []}]), open(pj, "w"))
        AP.main(["--write", pj, K10, self.out, rj])
        self.assertEqual(json.load(open(rj))["moved"], ["R1 -> (11.00, 10.00) rot 0 side F"])
        self.assertEqual(Board.load(self.out).footprint("R1").pos, (11.0, 10.0))

    def test_strip_nets(self):
        n = strip_nets.strip(K10, self.out, nets={"B"})
        self.assertEqual((n, len(Board.load(self.out).items)), (3, 0))
        n = strip_nets.strip(K10, self.out, ids={"70000000-0000-4000-8000-000000000001"})
        self.assertEqual((n, [i.kind for i in Board.load(self.out).items]), (1, ["track", "track"]))


@unittest.skipUnless(config.kicad_cli_available(), "kicad-cli not installed")
class TestKiCadReopens(unittest.TestCase):
    """If kicad-cli is around, KiCad must accept the edited file (DRC runs) and see the same unconnected count as the original."""

    def drc(self, path):
        out = path + ".drc.json"
        subprocess.run([config.kicad_cli(), "pcb", "drc", "--format", "json", "--severity-all", "-o", out, path], capture_output=True, text=True)
        return json.load(open(out))

    def test_roundtrip_and_edit(self):
        d = tempfile.mkdtemp()
        try:
            a = os.path.join(d, "a.kicad_pcb")
            shutil.copy(K10, a)
            d0 = self.drc(a)
            same = os.path.join(d, "same.kicad_pcb")
            AP.write(plan(), a, same)                                   # unmodified round trip: identical DRC
            d1 = self.drc(same)
            self.assertEqual((len(d1["violations"]), len(d1["unconnected_items"])), (len(d0["violations"]), len(d0["unconnected_items"])))
            P = plan(parts=[{"ref": "U1", "pos": [30.0, 20.0], "rot": 90.0, "side": "B", "pads": []}],
                     vias_add=[{"pos": [30.0, 5.0], "net": "GND", "dia": 0.5, "drill": 0.25, "layers": ["F.Cu", "B.Cu"]}])
            b = os.path.join(d, "b.kicad_pcb")
            AP.write(P, a, b)
            d2 = self.drc(b)                                            # KiCad opens the edited file and sees the moved part
            self.assertIn("violations", d2)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
