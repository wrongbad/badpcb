"""audit: one synthetic board per detector, the CLI/suggest wiring, and smoke tests on the real boards."""
import contextlib
import io
import os
import unittest

from badpcb import audit, cli, state
from tests import synth

HERE = os.path.dirname(os.path.abspath(__file__))
PLACED = os.path.join(HERE, "..", "models", "key_board_placed.model.json")
BEST = os.path.join(HERE, "..", ".routes", "torture2", "best.kicad_pcb")
SESSIONS = os.path.join(HERE, "..", ".sessions")


def P(num, pos, net, size=(0.5, 0.5), layers=("F.Cu",), drill=0.0):
    return synth.pad(num, pos, net, layers=layers, size=size, drill=drill)


def part(ref, pads, pos, half=(1.0, 1.0), value="", side="F"):
    d = synth.part(ref, pads, pos=pos, bbox=[pos[0] - half[0], pos[1] - half[1], pos[0] + half[0], pos[1] + half[1]])
    d["value"] = value
    d["side"] = side
    return d


def stage(parts, **kw):
    b = synth.board(parts, **kw)
    return state.State("t_audit", "synthetic", b, [], 21.0)


def qfn(ref, cx, cy, nets_n, pitch=0.4):
    """Fine-pitch part with a N row and a S row (10 pads each, 0.2 x 0.8 pads)."""
    pads = []
    for i in range(10):
        x = cx + (i - 4.5) * pitch
        pads.append(P(str(i + 1), (x, cy - 2.0), nets_n[i], size=(0.2, 0.8)))
        pads.append(P(str(i + 11), (x, cy + 2.0), "", size=(0.2, 0.8)))
    return part(ref, pads, (cx, cy), half=(3.0, 2.8))


def dets(rep, name):
    return [f for f in rep.findings if f.det == name]


class Escape(unittest.TestCase):
    def test_intruders_flagged_and_pushed(self):
        nets = ["s%d" % i for i in range(10)]
        parts = [qfn("U9", 50, 25, nets)]
        # resistors parked in the north fan (2.4 mm deep zone above the pads at y=23)
        for k, x in enumerate((48.4, 49.6, 50.8, 52.0)):
            parts.append(part("R%d" % (k + 1), [P("1", (x, 20.2), "q%d" % k), P("2", (x, 20.9), "r%d" % k)], (x, 20.5), half=(0.4, 0.7)))
        st = stage(parts)
        rep = audit.run(st, ["ESCAPE"])
        f = [x for x in dets(rep, "ESCAPE") if x.handles["side"] == "N"]
        self.assertEqual(len(f), 1)
        f = f[0]
        self.assertEqual(f.sev, "ERR")
        self.assertTrue(set(f.handles["intruders"]) >= {"R1", "R2", "R3", "R4"})
        self.assertGreaterEqual(f.ev["signal_blocked"], 6)
        fx = f.best
        self.assertIsNotNone(fx)
        self.assertTrue(fx.ok)
        self.assertGreater(fx.gain, 0.1)                       # the fan gets freer
        # pasteable: the fix is a compact action list that the session accepts
        st.act(fx.spec)
        rep2 = audit.run(st, ["ESCAPE"])
        self.assertFalse([x for x in dets(rep2, "ESCAPE") if x.handles["side"] == "N" and x.sev == "ERR"])

    def test_decap_on_its_own_pin_is_tolerated(self):
        nets = ["+3V3"] + ["s%d" % i for i in range(1, 10)]
        parts = [qfn("U9", 50, 25, nets)]
        parts.append(part("C1", [P("1", (46.9, 21.0), "+3V3"), P("2", (46.9, 21.6), "GND")], (46.9, 21.3), half=(0.4, 0.7), value="100n"))
        parts.append(part("U8", [P(str(i), (20 + i, 5), n) for i, n in enumerate(["+3V3", "GND", "a", "b", "c", "d"])], (22, 5), half=(4, 2)))
        st = stage(parts)
        ac = audit.Ctx(st)
        self.assertEqual(ac.roles()["C1"].kind, "decap")
        rep = audit.run(st, ["ESCAPE"])
        for f in dets(rep, "ESCAPE"):
            self.assertNotIn("C1", f.handles["intruders"])

    def test_clean_part_is_quiet(self):
        st = stage([qfn("U9", 50, 25, ["s%d" % i for i in range(10)])])
        self.assertEqual(audit.run(st, ["ESCAPE"]).findings, [])


class Plane(unittest.TestCase):
    def board(self, cap_x):
        z1 = {"layers": ["In2.Cu"], "net": "+3V3", "priority": 0, "polys": [{"outer": [[0, 0], [50, 0], [50, 50], [0, 50]], "holes": []}]}
        z2 = {"layers": ["In2.Cu"], "net": "+3V3A", "priority": 0, "polys": [{"outer": [[50.5, 0], [100, 0], [100, 50], [50.5, 50]], "holes": []}]}
        parts = [part("C1", [P("1", (cap_x, 20), "+3V3A"), P("2", (cap_x, 20.6), "GND")], (cap_x, 20.3), half=(0.4, 0.7), value="100n"),
                 part("C2", [P("1", (80, 30), "+3V3A"), P("2", (80, 30.6), "GND")], (80, 30.3), half=(0.4, 0.7), value="100n"),
                 part("R1", [P("1", (10, 10), "+3V3"), P("2", (10, 10.6), "x")], (10, 10.3), half=(0.4, 0.7))]
        zg = synth.zone("GND", "In1.Cu")
        return stage(parts, zones=[z1, z2, zg], layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))

    def test_supply_pad_over_wrong_plane(self):
        st = self.board(20)
        rep = audit.run(st, ["PLANE"])
        f = dets(rep, "PLANE")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].handles["net"], "+3V3A")
        self.assertEqual(f[0].handles["parts"], ["C1"])
        self.assertTrue(f[0].advice and "reshape" in f[0].advice[0] and "+3V3A" in f[0].advice[0])
        self.assertIsNotNone(f[0].best)                        # a legal move onto the +3V3A region
        self.assertTrue([x for x in f[0].fixes if x.tag == "reshape" and x.acts[0].startswith("reshape +3V3A add")])
        a = [x for x in f[0].fixes if x.tag == "onplane"][0].acts[0]
        self.assertTrue(a.startswith("move C1 "))
        x = float(a.split()[2])
        self.assertGreater(x, 50.5)

    def test_pad_on_its_plane_is_quiet(self):
        st = self.board(75)
        self.assertEqual(dets(audit.run(st, ["PLANE"]), "PLANE"), [])


class PlaneExtent(unittest.TestCase):
    """Pad-over-plane is judged by the pad's copper extent and where its stub + via can land, against zone outlines."""
    LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")

    def zones(self):
        return [{"layers": ["In2.Cu"], "net": "+3V3", "priority": 0, "polys": [{"outer": [[0, 0], [50, 0], [50, 50], [0, 50]], "holes": []}]},
                {"layers": ["In2.Cu"], "net": "+3V3A", "priority": 0, "polys": [{"outer": [[50.5, 0], [100, 0], [100, 50], [50.5, 50]], "holes": []}]},
                synth.zone("GND", "In1.Cu")]

    def board(self, x, size=(0.15, 0.6), x60=None, net60="+3V3", **kw):
        """U1.59 (+3V3A) at x, U1.60 (+3V3) 0.4 mm to its left: no zone boundary can separate the two pads."""
        parts = [part("U1", [P("59", (x, 20), "+3V3A", size=size), P("60", (x - 0.4 if x60 is None else x60, 20), net60, size=(0.15, 0.6))], (x, 20), half=(0.6, 0.6))]
        return state.State("t_audit", "synthetic", synth.board(parts, zones=self.zones(), layers=self.LAYERS, **kw), [], 21.0)

    def test_pad_centre_in_other_plane_but_stub_via_lands_in_right_zone(self):
        st = self.board(49.9)                      # pad centre 0.6 mm inside +3V3, +3V3A zone starts at 50.5
        self.assertEqual(dets(audit.run(st, ["PLANE"]), "PLANE"), [])

    def test_pad_copper_overlapping_its_zone_is_served(self):
        st = self.board(50.2, size=(0.7, 0.6), x60=49.4)     # extent 49.85..50.55 reaches into the +3V3A outline (starts at 50.5)
        self.assertEqual(dets(audit.run(st, ["PLANE"]), "PLANE"), [])

    def test_existing_stub_and_via_in_the_right_zone_count(self):
        st = self.board(49.9, vias=[synth.via((51.5, 20), "+3V3A")], tracks=[synth.track((49.9, 20), (51.5, 20), "+3V3A", locked=False)])
        self.assertEqual([f for f in dets(audit.run(st, ["PLANE"]), "PLANE") if "U1.59" in f.handles["pads"]], [])

    def test_zone_outline_is_enough_unfilled_and_without_vias(self):
        """The model only knows outlines (never fills): a zone with no fill and no via in it is a plane for the audit."""
        st = self.board(75, net60="+3V3A")
        self.assertEqual(st.world.board.plane_nets(), ["+3V3", "+3V3A", "GND"])
        self.assertEqual(dets(audit.run(st, ["PLANE"]), "PLANE"), [])

    def test_no_reach_is_still_an_error(self):
        st = self.board(20)                        # 30 mm from the +3V3A zone: no stub can get there
        f = dets(audit.run(st, ["PLANE"]), "PLANE")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].sev, "ERR")


class Bowtie(unittest.TestCase):
    def pair(self):
        a = part("X1", [P("1", (10, 10), "n1"), P("2", (10, 12), "n2")], (10, 11), half=(3, 3))
        b = part("X2", [P("1", (20, 10), "n2"), P("2", (20, 12), "n1")], (20, 11), half=(1, 2))
        return stage([a, b])

    def test_crossing_pair_rotates_smaller(self):
        st = self.pair()
        rep = audit.run(st, ["BOWTIE", "ORIENT"])
        f = dets(rep, "BOWTIE")
        self.assertEqual(len(f), 1)
        self.assertEqual(sorted(f[0].handles["parts"]), ["X1", "X2"])
        fx = f[0].best
        self.assertEqual(fx.spec, "rot X2 180")
        self.assertLess(fx.res.d["X"], 0)
        st.act(fx.spec)
        self.assertEqual(dets(audit.run(st, ["BOWTIE"]), "BOWTIE"), [])


class Orient(unittest.TestCase):
    def test_flipped_resistor(self):
        r = part("R1", [P("1", (27, 10), "a"), P("2", (33, 10), "b")], (30, 10), half=(4, 1))
        pa = part("U5", [P("1", (50, 10), "a")], (50, 10), half=(0.6, 0.6))
        pb = part("U6", [P("1", (10, 10), "b")], (10, 10), half=(0.6, 0.6))
        st = stage([r, pa, pb])
        f = dets(audit.run(st, ["ORIENT"]), "ORIENT")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].best.spec, "rot R1 180")

    def test_right_way_round_is_quiet(self):
        r = part("R1", [P("1", (33, 10), "a"), P("2", (27, 10), "b")], (30, 10), half=(4, 1))
        pa = part("U5", [P("1", (50, 10), "a")], (50, 10), half=(0.6, 0.6))
        pb = part("U6", [P("1", (10, 10), "b")], (10, 10), half=(0.6, 0.6))
        self.assertEqual(dets(audit.run(stage([r, pa, pb]), ["ORIENT"]), "ORIENT"), [])


class Tension(unittest.TestCase):
    def test_outlier(self):
        u = part("U7", [P("1", (60, 10), "a"), P("2", (60, 12), "b")], (60, 11), half=(2, 2))
        u["locked"] = True
        c = part("C5", [P("1", (10, 40), "a"), P("2", (10, 40.6), "b")], (10, 40.3), half=(0.5, 0.8))
        st = stage([u, c])
        f = dets(audit.run(st, ["TENSION"]), "TENSION")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].handles["part"], "C5")
        x, y = float(f[0].best.acts[0].split()[2]), float(f[0].best.acts[0].split()[3])
        self.assertLess(abs(x - 60), 8)
        self.assertLess(abs(y - 11), 8)

    def test_test_points_are_exempt(self):
        u = part("U7", [P("1", (60, 10), "a"), P("2", (60, 12), "b")], (60, 11), half=(2, 2))
        u["locked"] = True
        c = part("TP5", [P("1", (10, 40), "a")], (10, 40), half=(0.5, 0.5))
        self.assertEqual(dets(audit.run(stage([u, c]), ["TENSION"]), "TENSION"), [])


class Roles(unittest.TestCase):
    def ic(self):
        return part("U3", [P(str(i + 1), (30 + 0.5 * i, 20), n) for i, n in enumerate(["+3V3", "GND", "a", "b", "c", "d"])], (31.5, 20), half=(2, 1.5))

    def test_far_decap_and_filter_cap(self):
        c1 = part("C1", [P("1", (60, 40), "+3V3"), P("2", (60, 40.6), "GND")], (60, 40.3), half=(0.4, 0.7), value="100n")
        r1 = part("R1", [P("1", (45, 5), "sig"), P("2", (45, 5.6), "a")], (45, 5.3), half=(0.4, 0.7), value="100")
        c2 = part("C2", [P("1", (46, 10), "a"), P("2", (46, 10.6), "GND")], (46, 10.3), half=(0.4, 0.7), value="470p")
        st = stage([self.ic(), c1, r1, c2])
        ac = audit.Ctx(st)
        roles = ac.roles()
        self.assertEqual(roles["C1"].kind, "decap")
        self.assertEqual(roles["C2"].kind, "filter_cap")
        self.assertEqual(roles["R1"].kind, "series_R")
        rep = audit.run(st, ["ROLES"])
        f = dets(rep, "ROLES")
        kinds = {x.handles["role"] for x in f}
        self.assertEqual(kinds, {"decap", "filter_cap"})
        for x in f:
            self.assertIsNotNone(x.best)
            part_ref = x.best.acts[0].split()[1]
            st.act(x.best.spec)
            self.assertLess(audit.Ctx(st).roles()[part_ref].dist, audit.Ctx(st).roles()[part_ref].limit * 1.3)

    def test_audit_block_overrides_a_threshold(self):
        from badpcb import config
        c1 = part("C1", [P("1", (60, 40), "+3V3"), P("2", (60, 40.6), "GND")], (60, 40.3), half=(0.4, 0.7), value="100n")
        st = stage([self.ic(), c1])
        try:
            self.assertEqual(audit.params()["decap_bypass_mm"], 3.0)
            self.assertEqual(len(dets(audit.run(st, ["ROLES"]), "ROLES")), 1)
            config.use({"audit": {"decap_bypass_mm": 500.0}})
            self.assertEqual(audit.Ctx(st).roles()["C1"].limit, 500.0)
            self.assertEqual(dets(audit.run(st, ["ROLES"]), "ROLES"), [])
            config.use({"audit": {"nope": 1}})
            with self.assertRaises(SystemExit):
                audit.params()
        finally:
            config.use({})

    def test_crystal_and_pullup(self):
        ic = part("U3", [P(str(i + 1), (30 + 0.5 * i, 20), n) for i, n in enumerate(["XIN", "XOUT", "RUN", "b", "c", "d"])], (31.5, 20), half=(2, 1.5))
        y = part("Y1", [P("1", (35, 24), "XIN"), P("2", (35.8, 24), "XOUT")], (35.4, 24), half=(1.5, 1.0))
        cl = part("C3", [P("1", (36, 28), "XIN"), P("2", (36, 28.6), "GND")], (36, 28.3), half=(0.4, 0.7), value="15p")
        pu = part("R2", [P("1", (31, 24), "+3V3"), P("2", (31, 24.6), "RUN")], (31, 24.3), half=(0.4, 0.7), value="10k")
        ac = audit.Ctx(stage([ic, y, cl, pu]))
        r = ac.roles()
        self.assertEqual(r["Y1"].kind, "crystal")
        self.assertEqual(r["C3"].kind, "xtal_load")
        self.assertEqual(r["R2"].kind, "pullup")
        txt = "\n".join(audit.sec_roles(ac))
        self.assertIn("xtal_load", txt)


class BusOrder(unittest.TestCase):
    def test_inverted_bus(self):
        a = part("U5", [P("1", (10, 18.5), "a"), P("2", (11, 18.5), "b"), P("3", (12, 18.5), "c")], (11, 20), half=(2, 2))
        b = part("U6", [P("1", (10, 6.5), "c"), P("2", (11, 6.5), "b"), P("3", (12, 6.5), "a")], (11, 5), half=(2, 2))
        st = stage([a, b])
        rep = audit.run(st, ["BUSORDER"])
        f = dets(rep, "BUSORDER")
        self.assertTrue(f)
        self.assertGreaterEqual(f[0].ev["inversions"], 2)
        self.assertTrue(f[0].best.spec.startswith("rot "))

    def test_ordered_bus_is_quiet(self):
        a = part("U5", [P("1", (10, 18.5), "a"), P("2", (11, 18.5), "b"), P("3", (12, 18.5), "c")], (11, 20), half=(2, 2))
        b = part("U6", [P("1", (10, 6.5), "a"), P("2", (11, 6.5), "b"), P("3", (12, 6.5), "c")], (11, 5), half=(2, 2))
        self.assertEqual(dets(audit.run(stage([a, b]), ["BUSORDER"]), "BUSORDER"), [])


class Courtyard(unittest.TestCase):
    def test_overlap_and_nudge(self):
        a = part("U1x", [P("1", (10, 10), "a")], (10, 10), half=(2, 2))
        b = part("C1", [P("1", (12.1, 10), "b"), P("2", (12.1, 10.6), "c")], (12.1, 10.3), half=(0.5, 0.8))
        st = stage([a, b])
        f = dets(audit.run(st, ["COURTYARD"]), "COURTYARD")
        self.assertEqual(len(f), 1)
        self.assertEqual(sorted(f[0].handles["parts"]), ["C1", "U1x"])
        fx = f[0].best
        self.assertTrue(fx.spec.startswith("nudge C1 "))
        st.act(fx.spec)
        self.assertEqual(dets(audit.run(st, ["COURTYARD"]), "COURTYARD"), [])

    def test_touching_courtyards_are_fine(self):
        a = part("U1x", [P("1", (10, 10), "a")], (10, 10), half=(2, 2))
        b = part("C1", [P("1", (12.6, 10), "b")], (12.6, 10), half=(0.6, 0.6))
        self.assertEqual(dets(audit.run(stage([a, b]), ["COURTYARD"]), "COURTYARD"), [])


class Wiring(unittest.TestCase):
    def test_text_json_and_suggest(self):
        nets = ["s%d" % i for i in range(10)]
        parts = [qfn("U9", 50, 25, nets)]
        for k, x in enumerate((48.4, 49.6, 50.8, 52.0)):
            parts.append(part("R%d" % (k + 1), [P("1", (x, 20.2), "q%d" % k), P("2", (x, 20.9), "r%d" % k)], (x, 20.5), half=(0.4, 0.7)))
        st = stage(parts)
        rep = audit.run(st)
        txt = audit.text(rep)
        self.assertLessEqual(len(txt), 62)
        self.assertTrue(txt[0].startswith("badpcb audit"))
        j = audit.as_json(rep)
        self.assertEqual(len(j["findings"]), len(rep.findings))
        self.assertIn("fixes", j["findings"][0])
        res, notes = audit.suggest(st)
        self.assertTrue(res)
        self.assertTrue(all(r.ok for r in res))
        # nothing was committed
        self.assertEqual(st.step(), 0)
        # the table feeds `act @1`
        from badpcb import evaluate
        res2, _ = evaluate.suggest(st, "audit")
        st.last = [r.label for r in res2]
        r = st.act("@1")
        self.assertTrue(r["applied"])

    def test_cli_rejects_unknown_detector(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                cli.main(["audit", PLACED, "--only", "NOPE"])






if __name__ == "__main__":
    unittest.main()


class Joint(unittest.TestCase):
    def build(self):
        from badpcb import joint  # noqa
        nets = ["s%d" % i for i in range(10)]
        parts = [qfn("U9", 50, 25, nets)]
        for k, x in enumerate((48.4, 49.6, 50.8, 52.0)):                       # series Rs parked in the north fan
            parts.append(part("R%d" % (k + 1), [P("1", (x, 20.2), "q%d" % k), P("2", (x, 20.9), nets[k + 3])], (x, 20.5), half=(0.4, 0.7)))
        # filter cap of s0, far away (needs to come back) and a cap right at the board edge
        parts.append(part("C1", [P("1", (70, 40), nets[0]), P("2", (70, 40.6), "GND")], (70, 40.3), half=(0.4, 0.7), value="470p"))
        return stage(parts)

    def test_joint_plan_clears_fan_and_roles_and_applies(self):
        from badpcb import joint
        st = self.build()
        ac, acts, txt = joint.run(st, ["U9"], 1.5, name="t_audit")
        self.assertTrue(acts, txt)
        self.assertIn("role violations", txt)
        for a in acts:
            self.assertTrue(a.startswith(("move", "via", "delvia")))
        st.act("; ".join(acts))
        rep = audit.run(st, ["ESCAPE", "ROLES"])
        self.assertFalse([x for x in dets(rep, "ESCAPE") if x.sev == "ERR"])
        self.assertFalse([x for x in dets(rep, "ROLES") if x.sev == "ERR"])
        b = audit.Ctx(st).b
        x0, y0, x1, y1 = b.bbox
        for r in ("R1", "R2", "R3", "R4", "C1"):
            bb = b.parts[r].bbox
            self.assertGreaterEqual(min(bb[0] - x0, bb[1] - y0, x1 - bb[2], y1 - bb[3]), 1.5 - 1e-6)

    def test_minimal_moves_only_blockers(self):
        from badpcb import joint
        st = self.build()
        _ac, acts, txt = joint.run(st, ["U9"], 1.5, name="t_audit", minimal=True)
        full = joint.run(st, ["U9"], 1.5, name="t_audit")[1]
        self.assertIn("MINIMAL", txt)
        self.assertLessEqual(len(acts), len(full))

    def test_world_is_restored(self):
        from badpcb import joint
        st = self.build()
        before = {r: (p.pos, p.rot) for r, p in st.world.board.parts.items()}
        joint.run(st, ["U9"], 1.5)
        self.assertEqual(before, {r: (p.pos, p.rot) for r, p in st.world.board.parts.items()})

    def test_delvia_stub_text_roundtrip(self):
        from badpcb import actions as A
        a = A.parse("delvia GND 10 20 stub")[0]
        self.assertTrue(a.a.get("stub"))
        self.assertEqual(a.compact(), "delvia GND 10 20 stub")


class Reshape(unittest.TestCase):
    def test_add_carves_and_undo_restores(self):
        import copy
        st = Plane.board(Plane(), 20)
        b = st.world.board
        z0 = copy.deepcopy([z.polys for z in b.zones])
        r = st.act("reshape +3V3A add 18,18,23,23 layer=In2.Cu")
        self.assertTrue(any("lose their plane" in w for w in r["warnings"]) or r["delta"]["stubs"] <= 0)
        zA = [z for z in b.zones if z.net == "+3V3A"][0]
        z3 = [z for z in b.zones if z.net == "+3V3"][0]
        from badpcb import actions as A
        self.assertTrue(A._in_zone(b, "+3V3A", (20, 20)))
        self.assertFalse(A._in_zone(b, "+3V3", (20, 20)))
        self.assertTrue(A._in_zone(b, "+3V3", (10, 10)))
        self.assertEqual(len(zA.polys), 2)
        self.assertGreaterEqual(len(z3.polys[0]["holes"]) + len(z3.polys), 2)
        st.undo()
        self.assertEqual(z0, [z.polys for z in b.zones])

    def test_sub_and_errors(self):
        st = Plane.board(Plane(), 20)
        from badpcb import actions as A
        r = st.act("reshape +3V3 sub 0,0,5,5")           # corner cut: pieces, no hole
        b = st.world.board
        self.assertFalse(A._in_zone(b, "+3V3", (2, 2)))
        self.assertTrue(A._in_zone(b, "+3V3", (10, 2)))
        self.assertTrue(A._in_zone(b, "+3V3", (2, 10)))
        for bad in ("reshape NOPE add 1,1,5,5", "reshape +3V3 add 1,1,5,5 layer=B.Cu", "reshape +3V3 sub 80,0,90,10", "reshape +3V3 add 1,1,1.1,1.1"):
            with self.assertRaises(A.ActionError):
                st.act(bad)
        self.assertEqual(A.parse("reshape +3V3 add 1 2 3 4 layer=In2.Cu")[0].compact(), "reshape +3V3 add 1,2,3,4 layer=In2.Cu")

    def test_audit_offers_reshape_with_vias(self):
        st = Plane.board(Plane(), 20)
        rep = audit.run(st, ["PLANE"])
        f = dets(rep, "PLANE")[0]
        fx = [x for x in f.fixes if x.tag == "reshape"][0]
        self.assertTrue(fx.ok)
        self.assertTrue(fx.acts[0].startswith("reshape +3V3A add") and any(a.startswith("via +3V3A") for a in fx.acts[1:]))

    def test_apply_plan_carries_zone_edits(self):
        from badpcb import apply as AP
        st = Plane.board(Plane(), 20)
        st.act("reshape +3V3A add 18,18,23,23 layer=In2.Cu")
        P = AP.plan(st)
        self.assertEqual({z["net"] for z in P["zones"]}, {"+3V3", "+3V3A"})


class GScore(unittest.TestCase):
    def test_hints_and_rank_and_cli(self):
        from badpcb import gscore
        nets = ["s%d" % i for i in range(10)]
        parts = [qfn("U9", 50, 25, nets)]
        for k, x in enumerate((48.4, 49.6, 50.8, 52.0)):
            parts.append(part("R%d" % (k + 1), [P("1", (x, 20.2), "q%d" % k), P("2", (x, 20.9), nets[k + 3])], (x, 20.5), half=(0.4, 0.7)))
        st = stage(parts)
        st.act("layer s1 B.Cu")
        st.act("priority s2 2")
        st.act("freeze R1")
        hints, notes = gscore.hints_from_world(st.world)
        self.assertEqual({h["type"] for h in hints}, {"layer", "priority", "freeze"})
        st.act("near R1 U9 20")
        self.assertTrue(any("near" in n for n in gscore.hints_from_world(st.world)[1]))
        if os.path.exists(PLACED):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["groute", PLACED, "--time", "8", "--cell", "2", "--region", "R8", "--no-map", "--max-blocked", "2"])
            self.assertIn("groute:", buf.getvalue())
        g = gscore.score(st.world.board, [], None, None, cell=2.0, tlimit=5.0)
        self.assertIsNotNone(g)
        self.assertIn("score", g)
        rep = audit.run(st, ["ESCAPE"], gscore=20.0)
        fx = [x for f in rep.findings for x in f.fixes if getattr(x, "g1", None)]
        self.assertTrue(fx, "no fix got a groute score")
        d = rep.findings[0].fixes[0].as_dict()
        self.assertTrue("groute" in d or not rep.findings[0].fixes[0].ok)
        self.assertIn("groute", audit._fmt_fix(rep.findings[0], [x for x in rep.findings[0].fixes if getattr(x, "g1", None)][0]))
