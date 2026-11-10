"""Phase 5: feasibility check, lock awareness (classes, unlock/lock, trim-lanes), pad-level legality."""
import os
import shutil
import tempfile
import unittest

from badpcb import actions as A
from badpcb import config, feas, legal, locks
from tests import synth


def qfn(pitch=0.4, n=6):
    pads = [synth.pad(str(i + 1), (-2.0, i * pitch - n * pitch / 2), "N%d" % i, size=(0.8, 0.2)) for i in range(n)]
    pads += [synth.pad(str(n + i + 1), (2.0, i * pitch - n * pitch / 2), "M%d" % i, size=(0.8, 0.2)) for i in range(n)]
    pads += [synth.pad(str(2 * n + i + 1), (i * pitch - n * pitch / 2, -2.0), "P%d" % i, size=(0.2, 0.8)) for i in range(n)]
    pads += [synth.pad(str(3 * n + i + 1), (i * pitch - n * pitch / 2, 2.0), "Q%d" % i, size=(0.2, 0.8)) for i in range(n)]
    return synth.part("U1", pads, pos=(0, 0), bbox=[-3, -3, 3, 3])


class TestFeas(unittest.TestCase):
    def setUp(self):
        self.b = synth.board([qfn()], size=(30, 30))

    def eff(self, clr, tmin, area=None):
        e = feas.Eff()
        e.clr, e.track, e.min_track, e.via = clr, 0.2, tmin, 0.5
        if area:
            a = feas.Area("U1_FINE", "U1", 1.0)
            a.clr, a.track = area
            e.areas = [a]
        return e

    def test_old_rules_fail_with_numbers(self):
        res = feas.check(self.b, self.eff(0.2, 0.15))
        self.assertEqual(len(res), 1)
        sides = res[0][3]
        self.assertEqual(len(sides), 4)
        for r in sides:
            self.assertFalse(r["ok"])
            self.assertAlmostEqual(r["fan"], 0.2828, 3)
            self.assertAlmostEqual(r["fan_req"], 0.35, 3)
        self.assertIn("FAIL", feas.fmt_side(sides[0]))
        self.assertIn("0.283", feas.fmt_side(sides[0]))

    def test_local_rule_makes_it_ok(self):
        res = feas.check(self.b, self.eff(0.15, 0.15, area=(0.10, 0.10)))
        for r in res[0][3]:
            self.assertTrue(r["ok"] and r["fan_ok"])
            self.assertEqual(r["areas"], ["U1_FINE"])
            self.assertAlmostEqual(r["fan_max_track"], 0.183, 3)

    def test_default_rules_without_area_still_fail_at_0_15(self):
        res = feas.check(self.b, self.eff(0.15, 0.15))
        self.assertFalse(any(r["ok"] for r in res[0][3]))      # 0.15 + 0.15 = 0.30 > 0.283

    def test_dru_and_pro_parse(self):
        import json
        d = tempfile.mkdtemp()
        pro = os.path.join(d, "b.kicad_pro")
        json.dump({"net_settings": {"classes": [{"name": "Default", "clearance": 0.15, "track_width": 0.2, "via_diameter": 0.5, "via_drill": 0.25}]},
                   "board": {"design_settings": {"rules": {"min_track_width": 0.15}}}}, open(pro, "w"))
        open(os.path.join(d, "b.kicad_dru"), "w").write(
            "(version 1)\n# badpcb-area U1_FINE part=U1 margin=1.00 bbox=-4.000,-4.000,4.000,4.000\n"
            "(rule \"a\"\n  (condition \"A.intersectsArea('U1_FINE') || B.intersectsArea('U1_FINE')\")\n  (constraint clearance (min 0.100mm)))\n"
            "(rule \"b\"\n  (condition \"A.intersectsArea('U1_FINE')\")\n  (constraint track_width (min 0.100mm)))\n")
        e = feas.load_rules(pro)
        self.assertEqual((e.clr, e.track, e.min_track), (0.15, 0.2, 0.15))
        self.assertEqual(len(e.areas), 1)
        self.assertEqual((e.areas[0].clr, e.areas[0].track), (0.1, 0.1))
        self.assertTrue(feas.check(self.b, e)[0][3][0]["ok"])

    def test_grid_divides_pitch(self):
        from badpcb import cli
        c = cli.Ctx(self.b)
        g = feas.region_grids(c, self.eff(0.15, 0.15, area=(0.10, 0.10)))
        self.assertTrue(all(abs(0.4 / v[0] - round(0.4 / v[0])) < 1e-6 and v[0] <= 0.2 for v in g.values() if "fine" in v[1]))


def toyw(tracks=(), vias=(), parts=None):
    parts = parts or [synth.part("R1", [synth.pad("1", (9.5, 10), "N1"), synth.pad("2", (10.5, 10), "N2")], pos=(10, 10), bbox=[8.8, 9.4, 11.2, 10.6]),
                      synth.part("R2", [synth.pad("1", (19.5, 10), "N1"), synth.pad("2", (20.5, 10), "N3")], pos=(20, 10), bbox=[18.8, 9.4, 21.2, 10.6])]
    b = synth.board(parts, tracks=list(tracks), vias=list(vias), size=(40, 30))
    locks.classify(b)
    return A.World(b, {"mcu": "U9", "groups": [], "pin_names": {}})


class TestLocks(unittest.TestCase):
    def test_classes(self):
        w = toyw(tracks=[synth.track((1, 1), (5, 1), "N1", locked=True), synth.track((1, 3), (5, 3), "N2", locked=False)])
        self.assertEqual([locks.cls_of(t) for t in w.board.tracks], ["fanout", "free"])
        self.assertIn("LOCKED", locks.lock_tag(w.board.tracks[0]))

    def test_unlock_lock_roundtrip(self):
        w = toyw(tracks=[synth.track((1, 1), (5, 1), "N1", locked=True), synth.track((1, 3), (5, 3), "N2", locked=True)])
        errs, warns = A.check(w, A.parse("unlock N1")[0])
        self.assertFalse(errs)
        inv, nets = A.apply(w, A.parse("unlock N1")[0])
        self.assertEqual([t.locked for t in w.board.tracks], [False, True])
        self.assertEqual(nets, {"N1"})
        A.apply(w, inv, check_first=False)
        self.assertEqual([t.locked for t in w.board.tracks], [True, True])
        inv, _ = A.apply(w, A.parse("unlock fanout")[0])
        self.assertEqual([t.locked for t in w.board.tracks], [False, False])
        A.apply(w, A.parse("lock N2")[0])
        self.assertEqual([t.locked for t in w.board.tracks], [False, True])

    def test_unlock_errors(self):
        w = toyw(tracks=[synth.track((1, 1), (5, 1), "N1", locked=False)])
        self.assertTrue(A.check(w, A.parse("unlock N1")[0])[0])          # nothing locked
        self.assertTrue(A.check(w, A.parse("unlock NOPE")[0])[0])        # unknown target

    def test_region_target(self):
        w = toyw(tracks=[synth.track((1, 1), (5, 1), "N1", locked=True), synth.track((30, 20), (35, 20), "N2", locked=True)])
        inv, nets = A.apply(w, A.parse("unlock 0,0,10,10")[0])
        self.assertEqual([t.locked for t in w.board.tracks], [False, True])

    def test_plan_has_unlock(self):
        from badpcb import apply as AP
        from badpcb import state
        w = toyw(tracks=[synth.track((1, 1), (5, 1), "N1", locked=True)])
        w.board.tracks[0].id = "abc"
        base = __import__("copy").deepcopy(w.board)
        A.apply(w, A.parse("unlock N1")[0])
        class St:
            pass
        st = St()
        st.base, st.world, st.name, st.board_path = base, w, "x", "x"
        P = AP.plan(st)
        self.assertEqual(P["unlock"], ["abc"])
        self.assertEqual(P["lock"], [])

    def test_blockage_names_locked_lane(self):
        from badpcb import ratsnest
        parts = [synth.part("A", [synth.pad("1", (5, 10), "S")], pos=(5, 10)), synth.part("B", [synth.pad("1", (35, 10), "S")], pos=(35, 10))]
        b = synth.board(parts, tracks=[synth.track((6, 10), (34, 10), "L", locked=True)], size=(40, 30))
        locks.classify(b)
        b.tracks[0].cls = "lane"
        rn = ratsnest.build(b)
        c = rn.conns[0]
        d = locks.describe_blockage(b, c)
        self.assertIn("LOCKED L lane", d)
        self.assertIn("% of straight path blocked", d)

    def test_configured_class_is_classified_and_labelled(self):
        import json
        from badpcb import config, ratsnest
        tmp = tempfile.mkdtemp()
        try:
            json.dump(["u-1"], open(os.path.join(tmp, "lanes.json"), "w"))
            json.dump({"copied": ["u-2"]}, open(os.path.join(tmp, "mimic.json"), "w"))
            config.use({"locks": {"classes": {"lane": "lanes.json", "mimic": "mimic.json:copied"}}}, root=tmp)
            parts = [synth.part("A", [synth.pad("1", (5, 10), "S")], pos=(5, 10)), synth.part("B", [synth.pad("1", (35, 10), "S")], pos=(35, 10))]
            t1, t2, t3 = (synth.track((6, 10), (34, 10), "L", locked=True), synth.track((6, 5), (9, 5), "M", locked=True),
                          synth.track((6, 2), (9, 2), "F", locked=True))
            t1["id"], t2["id"], t3["id"] = "u-1", "u-2", "u-3"
            b = synth.board(parts, tracks=[t1, t2, t3], size=(40, 30))
            self.assertEqual([t.cls for t in b.tracks], ["lane", "mimic", "fanout"])
            self.assertEqual(locks.class_names(), ("lane", "mimic", "fanout", "free"))
            self.assertEqual(locks.class_alias("lanes"), "lane")
            self.assertIn("LOCKED L lane (lanes.json)", locks.describe_blockage(b, ratsnest.build(b).conns[0]))
            self.assertEqual(len(locks.resolve(b, "lanes")[0]), 1)
        finally:
            config.use({})
            shutil.rmtree(tmp)


class TestLockProps(unittest.TestCase):
    def tearDown(self):
        config.use({})

    def test_default_has_no_classes_and_shorthand_has_no_props(self):
        P = config.use({})
        self.assertEqual(P.lock_classes(), {})
        P = config.use({"locks": {"classes": {"a": "a.json:k", "b": {"file": "b.json", "protect": True, "trim": True}}}}, root="/r")
        self.assertEqual(P.lock_classes(), {"a": ("/r/a.json", "k"), "b": ("/r/b.json", None)})
        self.assertEqual(P.lock_props("a"), {"protect": False, "trim": False})
        self.assertEqual(P.lock_props("b"), {"protect": True, "trim": True})

    def test_trim_is_per_class(self):
        parts = [synth.part("A", [synth.pad("1", (5, 10), "S")], pos=(5, 10))]
        t1, t2 = synth.track((6, 10), (30, 10), "S", locked=True), synth.track((6, 20), (30, 20), "S", locked=True)
        t1["id"], t2["id"] = "u-1", "u-2"
        tmp = tempfile.mkdtemp()
        try:
            import json
            json.dump(["u-1"], open(os.path.join(tmp, "x.json"), "w"))
            json.dump(["u-2"], open(os.path.join(tmp, "y.json"), "w"))
            config.use({"locks": {"classes": {"x": {"file": "x.json", "trim": True}, "y": {"file": "y.json"}}}}, root=tmp)
            b = synth.board(parts, tracks=[t1, t2], size=(40, 30))
            self.assertEqual(locks.protected("x"), False)
            self.assertEqual({t.id for t, _ in locks.trim_candidates(b)}, {"u-1"})
            self.assertTrue(all(r.startswith("x ") for _, r in locks.trim_candidates(b)))
            with self.assertRaises(ValueError):
                locks.trim_candidates(b, "y")
            self.assertEqual(A.parse("trim x")[0].a["cls"], "x")
            self.assertEqual(A.parse("trim-lanes")[0].op, "trim")
            self.assertEqual(A.Action.from_dict({"op": "trim_lanes"}).op, "trim")
        finally:
            shutil.rmtree(tmp)


class TestLegal(unittest.TestCase):
    def test_pad_on_pad_refused_courtyard_free(self):
        # R3's courtyard is tiny so the courtyard test passes, but its pad lands on R2's pad
        parts = [synth.part("R2", [synth.pad("1", (19.5, 10), "N1", size=(0.6, 0.6)), synth.pad("2", (20.5, 10), "N3", size=(0.6, 0.6))], pos=(20, 10),
                            bbox=[18.8, 9.4, 21.2, 10.6]),
                 synth.part("R3", [synth.pad("1", (30, 10), "N2", size=(0.6, 0.6))], pos=(30, 10), bbox=[29.9, 9.9, 30.1, 10.1])]
        w = toyw(parts=parts)
        w.board.parts["R2"].bbox = (18.8, 9.4, 19.2, 10.6)       # courtyard deliberately smaller than the pads
        errs, _ = A.check(w, A.parse("move R3 20.6 10")[0])
        self.assertTrue(any("pad R3.1" in e and "R2.2" in e for e in errs), errs)
        errs, _ = A.check(w, A.parse("move R3 25 10")[0])
        self.assertFalse(errs, errs)

    def test_foreign_track_error_for_lane_warn_for_free(self):
        parts = [synth.part("R3", [synth.pad("1", (30, 10), "N2", size=(0.6, 0.6))], pos=(30, 10), bbox=[29.9, 9.9, 30.1, 10.1])]
        w = toyw(tracks=[synth.track((10, 20), (20, 20), "X", locked=True), synth.track((10, 22), (20, 22), "Y", locked=False)], parts=parts)
        w.board.tracks[0].cls = "lane"
        errs, _ = A.check(w, A.parse("move R3 15 20")[0])
        self.assertFalse(any("lane" in e for e in errs), errs)          # class without `protect`: ordinary locked copper
        config.use({"locks": {"classes": {"lane": {"file": "l.json", "protect": True}}}}, root=tempfile.gettempdir())
        w.board.lock_ver = 1                                            # invalidate the cached copper index
        try:
            errs, _ = A.check(w, A.parse("move R3 15 20")[0])
            self.assertTrue(any("[lane]" in e for e in errs), errs)
        finally:
            config.use({})
        errs, warns = A.check(w, A.parse("move R3 15 22")[0])
        self.assertFalse(errs, errs)
        self.assertTrue(any("rip" in x for x in warns), warns)

    def test_existing_conflict_not_new(self):
        parts = [synth.part("R2", [synth.pad("1", (20, 10), "N1", size=(0.6, 0.6))], pos=(20, 10), bbox=[19.9, 9.9, 20.1, 10.1]),
                 synth.part("R3", [synth.pad("1", (20.2, 10), "N2", size=(0.6, 0.6))], pos=(20.2, 10), bbox=[20.15, 9.95, 20.25, 10.05])]
        w = toyw(parts=parts)
        errs, _ = A.check(w, A.parse("nudge R3 0.05 0")[0])           # still overlapping the way it already did
        self.assertFalse(errs, errs)


if __name__ == "__main__":
    unittest.main()
