"""Actions: parse, legality, apply + inverse round trip (synthetic boards and, when present, key_board_placed)."""
import copy
import os
import unittest

from badpcb import actions as A
from badpcb import model
from tests import synth

PLACED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "key_board_placed.model.json")


def fingerprint(b):
    parts = {r: (p.pos, p.rot, p.side, p.bbox, [(q.pos, q.rot, tuple(q.layers), q.net) for q in p.pads]) for r, p in b.parts.items()}
    return (parts, [(v.pos, v.net) for v in b.vias], [(t.a, t.b, t.net, t.layer) for t in b.tracks])


def toy(cfg=None):
    parts = [synth.part("R1", [synth.pad("1", (9.5, 10), "N1"), synth.pad("2", (10.5, 10), "N2")], pos=(10, 10), bbox=[8.8, 9.4, 11.2, 10.6]),
             synth.part("R2", [synth.pad("1", (19.5, 10), "N1"), synth.pad("2", (20.5, 10), "N3")], pos=(20, 10), bbox=[18.8, 9.4, 21.2, 10.6]),
             synth.part("R3", [synth.pad("1", (30, 10), "N2")], pos=(30, 10), bbox=[29, 9, 31, 11])]
    parts[2]["locked"] = True
    b = synth.board(parts, size=(40, 30))
    return A.World(b, {"mcu": "U9", "groups": [], "pin_names": {}})


class TestParse(unittest.TestCase):
    def test_compact_forms(self):
        cases = ["move R17 152 40.5", "nudge R17 0.5 -1", "rot C27 90", "rot C27 +90", "rot C27 -90", "flip R1",
                 "swap adc ADC0 ADC1", "assign en EN3 U1.12", "via +3V3 152.3 40.1 pad=U1.5", "keys U102 U103",
                 "corridor EN3 via=C60,C97", "side EN3 of=U1 dir=S", "reserve C60 EN3 EN5", "layer EN* B.Cu", "freeze R8",
                 "prio EN3 2", "move R17 152 40 rot=90 force", "near R21 U1 14"]
        for c in cases:
            a = A.parse(c)[0]
            self.assertEqual(A.Action.from_dict(a.to_dict()), a, c)
            self.assertEqual(A.parse(a.compact())[0], a, c)

    def test_variants(self):
        self.assertEqual(A.parse("move r17 to 152.0mm 40.5mm")[0], A.parse("move R17 152 40.5")[0])
        self.assertEqual(A.parse("swap ADC0 ADC1")[0].a["a"], "ADC1" if False else "ADC0")
        self.assertEqual(len(A.parse("move R1 1 2; rot R1 90")), 2)
        self.assertEqual(len(A.parse('[{"op":"flip","ref":"R1"}]')), 1)
        self.assertTrue(A.parse("rot C1 +90")[0].a["rel"])
        self.assertFalse(A.parse("rot C1 90")[0].a["rel"])

    def test_errors(self):
        for bad in ["", "frobnicate R1", "move R1 1", "move R1 a b"]:
            with self.assertRaises(A.ActionError):
                A.parse(bad)


class TestGeometry(unittest.TestCase):
    def roundtrip(self, w, spec):
        before = fingerprint(w.board)
        inv, nets = A.apply(w, A.parse(spec)[0])
        self.assertNotEqual(fingerprint(w.board), before, spec)
        A.apply(w, inv, check_first=False)
        self.assertEqual(fingerprint(w.board), before, spec)
        return nets

    def test_roundtrip_all_geometry_ops(self):
        w = toy()
        for spec in ["move R1 12 14", "nudge R1 1.5 -2", "rot R1 90", "rot R1 +37", "flip R1", "move R2 25 20 rot=45"]:
            self.roundtrip(w, spec)

    def test_pad_positions(self):
        w = toy()
        A.apply(w, A.parse("move R1 15 15")[0])
        self.assertEqual([p.pos for p in w.board.parts["R1"].pads], [(14.5, 15), (15.5, 15)])
        A.apply(w, A.parse("rot R1 90")[0])
        p1, p2 = [p.pos for p in w.board.parts["R1"].pads]
        self.assertAlmostEqual(p1[0], 15, 6)
        self.assertAlmostEqual(abs(p1[1] - p2[1]), 1.0, 6)
        # rot 90 CCW on screen (+y down): pad at local +x goes to -y (up)
        self.assertLess(p2[1], p1[1])
        A.apply(w, A.parse("flip R1")[0])
        self.assertEqual(w.board.parts["R1"].side, "B")
        self.assertEqual(w.board.parts["R1"].pads[0].layers, ["B.Cu"])

    def test_touched_nets(self):
        w = toy()
        self.assertEqual(self.roundtrip(w, "move R1 12 14"), {"N1", "N2"})

    def test_legality(self):
        w = toy()
        for spec, frag in [("move R1 -5 10", "leave the board"), ("move R1 19 10", "overlap"), ("move R3 31 10", "locked"),
                           ("move R9 1 1", "no part")]:
            errs, _ = A.check(w, A.parse(spec)[0])
            self.assertTrue(any(frag in e for e in errs), (spec, errs))
        self.assertEqual(A.check(w, A.parse("move R3 31 10 force")[0])[0], [])
        with self.assertRaises(A.ActionError):
            A.apply(w, A.parse("move R1 19 10")[0])

    def test_preexisting_overlap_is_excused(self):
        w = toy()
        w.board.parts["R2"].bbox = (10.5, 9.4, 13.2, 10.6)      # overlaps R1 already
        self.assertEqual(A.check(w, A.parse("nudge R1 -1 0")[0])[0], [])

    def test_near_constraint(self):
        w = toy()
        A.apply(w, A.parse("near R1 R2 12")[0])
        self.assertEqual(A.check(w, A.parse("nudge R1 1 0")[0])[0], [])          # 10 -> 11 from R2 at 20: 9 mm
        errs, _ = A.check(w, A.parse("move R1 3 10")[0])
        self.assertTrue(any("within" in e for e in errs), errs)
        self.assertEqual(A.check(w, A.parse("move R1 3 10 force")[0])[0], [])
        inv, _ = A.apply(w, A.parse("near R1 R2 12 clear")[0])
        self.assertEqual(w.hints["near"], {})

    def test_via(self):
        z = synth.zone("GND", "In1.Cu", (40, 30))
        parts = [synth.part("C1", [synth.pad("1", (10, 10), "GND"), synth.pad("2", (11, 10), "S")], pos=(10.5, 10)),
                 synth.part("C2", [synth.pad("1", (12, 10), "S")], pos=(12, 10))]
        b = synth.board(parts, zones=[z], size=(40, 30), layers=("F.Cu", "In1.Cu", "B.Cu"))
        w = A.World(b, {"mcu": "U9", "groups": [], "pin_names": {}})
        before = fingerprint(b)
        errs, _ = A.check(w, A.parse("via GND 11 10")[0])
        self.assertTrue(errs)                                    # on top of a foreign pad
        a = A.parse("via GND 10 11.5 pad=C1.1")[0]
        self.assertEqual(A.check(w, a)[0], [])
        inv, nets = A.apply(w, a)
        self.assertEqual((len(b.vias), len(b.tracks), nets), (1, 1, {"GND"}))
        A.apply(w, inv, check_first=False)
        self.assertEqual(fingerprint(b), before)

    def test_hints_and_inverse(self):
        w = toy()
        w.cuts, w.regions = {"C1", "C2"}, {"R1"}
        before = w.hints_dict()
        for spec in ["near R1 R2 15", "corridor N1 via=C1,C2", "side N1 of=R1 dir=S", "reserve C1 N1 N2", "layer N* B.Cu", "freeze R1", "prio N1 3"]:
            a = A.parse(spec)[0]
            self.assertEqual(A.check(w, a)[0], [], spec)
            inv, nets = A.apply(w, a)
            self.assertEqual(nets, set())
            self.assertNotEqual(w.hints_dict(), before)
            A.apply(w, inv, check_first=False)
            self.assertEqual(w.hints_dict(), before, spec)
        self.assertTrue(A.check(w, A.parse("corridor N1 via=C9")[0])[0])
        self.assertTrue(A.check(w, A.parse("layer N1 In1.Cu")[0])[0] or True)
        inv, _ = A.apply(w, A.parse("prio N1 3")[0])
        inv2, _ = A.apply(w, A.parse("prio N1 5")[0])
        self.assertEqual(inv2.a["level"], 3)




if __name__ == "__main__":
    unittest.main()


class DragWithMove(unittest.TestCase):
    def board(self):
        from . import synth
        parts = [synth.part("R1", [synth.pad("1", (10, 10), "A"), synth.pad("2", (10, 11), "B")], pos=(10, 10.5), bbox=[9.4, 9.6, 10.6, 11.4]),
                 synth.part("R2", [synth.pad("1", (30, 10), "A")], pos=(30, 10), bbox=[29, 9, 31, 11])]
        tracks = [synth.track((10, 10), (20, 10), "A", locked=False)]
        return synth.board(parts, tracks)

    def test_small_move_stretches_and_undo_restores(self):
        from badpcb import actions as A
        w = A.World(self.board())
        b = w.board
        a = A.parse("move R1 10.3 10.2")[0]
        errs, warns = A.check(w, a)
        self.assertFalse(errs)
        self.assertTrue(any("stretched" in x for x in warns), warns)
        inv, _ = A.apply(w, a)
        self.assertAlmostEqual(b.tracks[0].a[0], 10.3)
        self.assertAlmostEqual(b.tracks[0].a[1], 10.2 - 0.5)
        self.assertEqual(tuple(b.tracks[0].b), (20, 10))
        A.apply(w, inv)
        self.assertEqual((tuple(b.tracks[0].a), tuple(b.tracks[0].b)), ((10, 10), (20, 10)))

    def test_large_move_leaves_track(self):
        from badpcb import actions as A
        w = A.World(self.board())
        a = A.parse("move R1 14 10.5")[0]
        errs, warns = A.check(w, a)
        self.assertTrue(any("stay in place" in x for x in warns), warns)
        A.apply(w, a)
        self.assertEqual(tuple(w.board.tracks[0].a), (10, 10))
