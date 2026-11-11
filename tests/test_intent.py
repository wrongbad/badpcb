"""Design intent: parsing, the penalty and its deltas, actions (add/rm/capture + inverses), reference drift, the audit INTENT detector."""
import copy
import json
import os
import tempfile
import unittest

from badpcb import actions as A
from badpcb import audit, config, engine as E, evaluate, intent, state
from badpcb.cli import sec_summary, Ctx
from tests import synth
from tests.test_audit import P, part, qfn


def board_parts(extra=()):
    """U9 (fine-pitch, N row at y=23, +3V3 on its first N pin), decap C1 near it, C2 far away, connector J1 in the N fan, flash U2 far east."""
    nets = ["+3V3"] + ["s%d" % i for i in range(1, 10)]
    parts = [qfn("U9", 50, 25, nets)]
    parts.append(part("C1", [P("1", (48.5, 29.5), "+3V3"), P("2", (49.5, 29.5), "GND")], (49, 29.5), half=(0.8, 0.5), value="100n"))
    parts.append(part("C2", [P("1", (80.0, 40.0), "+3V3"), P("2", (81.0, 40.0), "GND")], (80.5, 40.0), half=(0.8, 0.5), value="100n"))
    parts.append(part("J1", [P("1", (49, 21.2), "j1"), P("2", (51, 21.2), "j2")], (50, 21.0), half=(3.0, 1.5)))
    parts.append(part("U2", [P(str(i + 1), (88 + 0.5 * i, 10), "q%d" % i) for i in range(8)], (90, 10), half=(2.5, 2.0), value="W25Q128"))
    return parts + list(extra)


def stage(intent_cfg, extra=(), **kw):
    config.use({"intent": intent_cfg} if intent_cfg is not None else {})
    b = synth.board(board_parts(extra), **kw)
    return state.State("t_intent", "synthetic", b, [], 21.0)


class Base(unittest.TestCase):
    def tearDown(self):
        config.use({})


class Parse(Base):
    def test_sections_defaults_and_ids(self):
        P_ = config.use({"intent": {
            "blocks": {"vreg": {"parts": ["L1", "decaps:U1"], "max_span_mm": 8}},
            "near": [["C1", "U1.5", 3], ["C2,C3", "U1", 2, 5]],
            "apart": [["J1", "U1", 6]],
            "edge": [["J1", "back"], ["J2", "any", 1.5, 2]],
            "clear": [{"zone": "escape:U1", "except": ["decaps:U1"]}, {"zone": [10, 20, 0, 5], "weight": 4}]}})
        d = P_.intent()
        self.assertEqual(sorted(d), ["apart1", "block:vreg", "clear1", "clear2", "edge1", "edge2", "near1", "near2"])
        self.assertEqual(d["near1"], {"kind": "near", "a": ["C1"], "b": "U1.5", "mm": 3.0, "weight": 1.0, "id": "near1"})
        self.assertEqual(d["near2"]["a"], ["C2", "C3"])
        self.assertEqual(d["near2"]["weight"], 5.0)
        self.assertEqual(d["edge1"]["max_mm"], config.INTENT_EDGE_MM)
        self.assertEqual(d["edge1"]["weight"], 0.5)
        self.assertEqual(d["clear2"]["zone"], [0.0, 5.0, 10.0, 20.0])         # box normalised
        self.assertEqual(d["clear2"]["weight"], 4.0)
        self.assertEqual(d["clear1"]["zone"], "escape:U1")
        self.assertEqual(d["block:vreg"]["max_span_mm"], 8.0)
        self.assertIsNone(d["block:vreg"]["ref"])

    def test_no_intent_is_empty(self):
        self.assertEqual(config.use({}).intent(), {})

    def test_intent_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "i.json"), "w") as fh:
                json.dump({"near": [["C1", "U1", 2]]}, fh)
            self.assertEqual(list(config.use({"intent": "i.json"}, root=tmp).intent()), ["near1"])

    def test_refs_by_name_and_comments(self):
        P_ = config.use({"intent": {"_comment": "x", "refs": {"mini": {"pcb": "ref.kicad_pcb", "anchor": "U1"}},
                                    "blocks": {"b": {"parts": ["U2"], "ref": "mini"}}}}, root="/proj")
        r = P_.intent()["block:b"]["ref"]
        self.assertEqual(r, {"pcb": "/proj/ref.kicad_pcb", "anchor": "U1"})

    def test_errors(self):
        for bad in ({"near": [["C1", "U1"]]}, {"near": [["C1", "U1", "x"]]}, {"edge": [["J1", "top"]]}, {"clear": [{"zone": "nowhere"}]},
                    {"blocks": {"b": {}}}, {"bogus": []}, {"apart": [["A", "B", -1]]}):
            with self.assertRaises(config.IntentError, msg=str(bad)):
                config.parse_intent(bad)

    def test_round_trip(self):
        d = config.parse_intent({"near": [["C1", "U1.5", 3, 2]], "edge": [["J1", "front", 1]], "clear": [{"zone": "escape:U1:N", "except": ["C*"]}],
                                 "apart": [["A", "B,C", 2]], "blocks": {"v": {"parts": ["L1"], "max_span_mm": 5}}})
        for e in d.values():
            o = config.intent_to_obj(e)
            back = config.intent_entries(o)[0]
            back["id"] = e["id"]
            self.assertEqual(back, e)


class Penalty(Base):
    def test_near_apart_edge_clear_values(self):
        st = stage({"near": [["C2", "U9", 5]],                         # C2 is far: gap(C2 box, U9 box) - 5
                    "apart": [["J1", "U9", 6]],                        # courtyards overlap: shortfall 6
                    "edge": [["J1", "back", 2]],                       # J1 top at y=19.5: 19.5 mm from the back edge, 2 allowed
                    "clear": [{"zone": "escape:U9", "except": []}]})
        w = st.world
        rows = {it["id"]: (p, v) for it, p, v in intent.report(w)}
        b = w.board
        g = intent.gap(b.parts["C2"].bbox, b.parts["U9"].bbox)
        self.assertAlmostEqual(rows["near1"][0], g - 5, places=6)
        self.assertAlmostEqual(rows["apart1"][0], 6.0, places=6)
        self.assertAlmostEqual(rows["edge1"][0], 0.5 * (b.parts["J1"].bbox[1] - 0 - 2), places=6)
        self.assertEqual({v["part"] for v in rows["clear1"][1]} >= {"J1"}, True)
        self.assertAlmostEqual(st.metrics()["intent"], sum(p for p, _ in rows.values()), places=2)

    def test_weights_scale(self):
        a = stage({"near": [["C2", "U9", 5]]}).metrics()["intent"]
        b = stage({"near": [["C2", "U9", 5, 3]]}).metrics()["intent"]
        self.assertAlmostEqual(b, 3 * a, places=2)

    def test_pad_target_and_roles(self):
        st = stage({"near": [["decaps:U9", "U9.1", 2], ["C2", "U9.1", 2]]})
        C = intent.compiled(st.world)
        self.assertEqual([it["A"] for it in C.items], [["C1", "C2"], ["C2"]])      # both caps hang on +3V3; nearest IC pin is U9's (U2 has no supply)
        rows = {it["id"]: v for it, p, v in intent.report(st.world)}
        self.assertEqual([v["part"] for v in rows["near2"]], ["C2"])

    def test_unknown_selector_is_a_warning_not_an_error(self):
        st = stage({"near": [["NOPE*", "U9", 5], ["C1", "ZZ", 5]]})
        self.assertEqual(st.metrics()["intent"], 0.0)
        self.assertEqual(len(intent.compiled(st.world).warnings), 2)

    def test_block_span(self):
        st = stage({"blocks": {"b": {"parts": ["C1", "C2"], "max_span_mm": 10}}})
        b = st.world.board
        d = ((80.5 - 49) ** 2 + (40 - 29.5) ** 2) ** 0.5
        self.assertAlmostEqual(st.metrics()["intent"], intent.SPAN_W * (d - 10), places=2)

    def test_no_intent_metrics_unchanged(self):
        st = stage(None)
        m = st.metrics()
        self.assertEqual(m["intent"], 0.0)
        self.assertNotIn("intent", state.fmt_metrics(m))
        self.assertNotIn("intent", state.fmt_delta({k: 0 for k in E.METRIC_KEYS}))
        c = Ctx(st.world.board)
        self.assertFalse([l for l in sec_summary(c, False) if "intent" in l])

    def test_metrics_line_and_summary_with_intent(self):
        st = stage({"near": [["C2", "U9", 5]]})
        self.assertIn(" intent=", state.fmt_metrics(st.metrics()))
        c = Ctx(st.world.board)
        self.assertIn("intent=", "\n".join(sec_summary(c, False)))


class Delta(Base):
    def full(self, st):
        """Intent metric of the session's current board by a fresh engine on a deep copy (no incremental state)."""
        w = A.World(copy.deepcopy(st.world.board))
        w.intent, w.intent_ver = copy.deepcopy(st.world.intent), 1
        return E.Engine(w, st.skel, st.assume).metrics()["intent"]

    def test_incremental_equals_full_recompute(self):
        st = stage({"near": [["C1,C2", "U9", 3]], "clear": [{"zone": "escape:U9"}], "edge": [["J1", "back", 2]],
                    "blocks": {"b": {"parts": ["C1", "C2"], "max_span_mm": 4}}, "apart": [["C1", "C2", 50]]})
        res = evaluate.evaluate(st, ["move C2 52 33", "move J1 30 5", "move C1 60 40 rot=90; move C2 63 40", "rot J1 90"])
        base = st.metrics()["intent"]
        for r in res:
            self.assertTrue(r.ok, r.errors)
            self.assertAlmostEqual(r.m["intent"] - base, r.d["intent"], places=2)
            self.assertAlmostEqual(r.dscore, E.score(r.m) - E.score(st.base_m) + E.score(st.base_m) - E.score(st.metrics()), places=6)
        self.assertEqual(st.metrics()["intent"], base)                        # nothing applied
        for spec in ("move C2 52 33", "move J1 30 5", "move C1 60 40 rot=90"):
            st.act(spec)
            self.assertAlmostEqual(st.metrics()["intent"], self.full(st), places=2)
        st.undo(3)
        self.assertAlmostEqual(st.metrics()["intent"], base, places=6)

    def test_part_cost_matches_full_delta(self):
        st = stage({"near": [["C1,C2", "U9", 3]], "clear": [{"zone": "escape:U9"}], "edge": [["J1", "back", 2]]})
        w, b = st.world, st.world.board
        for ref, (x, y) in (("J1", (30, 5)), ("C2", (52, 33)), ("C1", (44, 31))):
            r0 = intent.penalty(w)[0]
            c0 = intent.part_cost(w, ref, b.parts[ref].bbox)
            bb, _ = A.place_geom(w, ref, (x, y), b.parts[ref].rot, b.parts[ref].side)
            c1 = intent.part_cost(w, ref, bb)
            r = evaluate.evaluate(st, ["move %s %g %g" % (ref, x, y)])[0]
            self.assertTrue(r.ok, r.errors)
            self.assertAlmostEqual(r.m["intent"] - r0, c1 - c0, places=2, msg=ref)

    def test_intent_in_dscore_and_suggest(self):
        st = stage({"near": [["C2", "U9", 3]]})
        r = evaluate.evaluate(st, ["move C2 52 33"])[0]
        self.assertTrue(r.ok, r.errors)
        self.assertLess(r.d["intent"], -3)
        self.assertLess(r.dscore, r.d["intent"] + 5)                          # the intent term is part of the ranked score
        self.assertIn("intent", state.fmt_delta(r.d))


class Actions(Base):
    def test_add_rm_capture_and_inverses(self):
        st = stage({"near": [["C2", "U9", 5]], "blocks": {"b": {"parts": ["U9", "C1"]}}})
        w = st.world
        base = copy.deepcopy(w.intent)
        r = st.act('intent add {"near": ["C1", "U9.1", 2, 3]}')
        self.assertIn("near2", w.intent)
        self.assertEqual(w.intent["near2"]["weight"], 3.0)
        self.assertEqual(r["applied"][0].startswith("intent add"), True)
        st.act("intent rm near1")
        self.assertNotIn("near1", w.intent)
        st.act("intent capture b")
        self.assertIn("pos", w.intent["block:b"]["ref"])
        self.assertEqual(list(w.intent["block:b"]["ref"]["pos"]), ["C1"])
        st.snapshot("s")
        st.undo(3)
        self.assertEqual(w.intent, base)
        st.revert("s")                                                        # forward again through the log: near1 gone, near2 there, block captured
        self.assertEqual(sorted(w.intent), ["block:b", "near2"])
        self.assertIn("pos", w.intent["block:b"]["ref"])

    def test_session_replay(self):
        st = stage({"near": [["C2", "U9", 5]]})
        st.act('intent add {"edge": ["J1", "back", 2]}; intent rm near1')
        st.act('intent add {"near": ["C1", "U9", 1]}')
        entries = [st._ser(e) for e in st.log]
        st2 = stage({"near": [["C2", "U9", 5]]})
        st2._replay(entries)
        self.assertEqual(st2.world.intent, st.world.intent)
        self.assertEqual(sorted(st2.world.intent), ["edge1", "near1"])
        self.assertAlmostEqual(st2.metrics()["intent"], st.metrics()["intent"], places=6)

    def test_errors_and_warnings(self):
        st = stage({"near": [["C2", "U9", 5]]})
        with self.assertRaises(A.ActionError):
            st.act("intent rm nothere")
        with self.assertRaises(A.ActionError):
            st.act("intent add {not json")
        with self.assertRaises(A.ActionError):
            st.act("intent capture nothere")
        r = st.act('intent add {"near": ["ZZ9", "U9", 3]}')
        self.assertTrue(any("matches no part" in w for w in r["warnings"]))
        self.assertEqual(A.parse("intent rm a b")[0].a["ids"], ["a", "b"])

    def test_eval_and_json_round_trip(self):
        a = A.parse('intent add {"clear": {"zone": "escape:U9", "except": ["C*"]}}')[0]
        self.assertEqual(A.Action.from_dict(a.to_dict()), a)
        self.assertIn("intent add", a.compact())


class Drift(Base):
    def world(self):
        pads = [P("1", (46, 22), "a"), P("2", (54, 22), "b"), P("3", (54, 28), "c"), P("4", (46, 28), "d")]
        parts = [part("U1", pads, (50, 25), half=(5, 4)),
                 part("C1", [P("1", (46, 18), "a"), P("2", (47, 18), "g")], (46.5, 18), half=(0.8, 0.5)),
                 part("C2", [P("1", (58, 25), "b"), P("2", (58, 26), "g")], (58, 25.5), half=(0.5, 0.8)),
                 part("R1", [P("1", (50, 33), "c"), P("2", (51, 33), "e")], (50.5, 33), half=(0.8, 0.5))]
        parts[2]["rot"] = 90.0
        config.use({"intent": {"blocks": {"mcu": {"parts": ["U1", "C1", "C2", "R1"], "anchor": "U1"}}}})
        b = synth.board(parts)
        return state.State("t_drift", "synthetic", b, [], 21.0)

    def drift(self, st):
        C = intent.compiled(st.world)
        return intent.drift(st.world, C.items[0])

    def test_capture_then_zero_then_moves(self):
        st = self.world()
        self.assertEqual(st.metrics()["intent"], 0.0)                         # no reference yet
        st.act("intent capture mcu")
        d = self.drift(st)
        self.assertAlmostEqual(d["rms"], 0.0, places=6)
        self.assertEqual(d["rot"], 0)
        st.act("move R1 50.5 36")                                             # 3 mm off of 3 members
        d = self.drift(st)
        self.assertAlmostEqual(d["rms"], (9 / 3) ** 0.5, places=3)
        self.assertGreater(st.metrics()["intent"], 0.0)
        self.assertAlmostEqual(st.metrics()["intent"], intent.DRIFT_W * (d["cost"] - intent.REF_TOL_MM), places=2)
        st.act("move R1 50.5 33; rot C1 90")                                  # back in place, one part turned 90 degrees
        d = self.drift(st)
        self.assertEqual(d["rot"], 1)
        self.assertAlmostEqual(d["cost"], d["rms"] + intent.ROT_MISS_MM, places=6)

    def test_rigid_motion_of_the_whole_block_is_free(self):
        st = self.world()
        st.act("intent capture mcu")
        # rotate the anchor 90 degrees about its centre and carry the members with it: A._to_world(lx, ly, 90) = (ly, -lx)
        acts = ["rot U1 90"]
        for r, (x, y, rot) in (("C1", (46.5, 18, 0)), ("C2", (58, 25.5, 90)), ("R1", (50.5, 33, 0))):
            dx, dy = x - 50, y - 25
            acts.append("move %s %.4f %.4f rot=%g" % (r, 50 + dy, 25 - dx, rot + 90))
        st.act("; ".join(acts))
        d = self.drift(st)
        self.assertAlmostEqual(d["rms"], 0.0, places=3)
        self.assertEqual(d["rot"], 0)
        st.act("move U1 70 40; move C1 %.4f %.4f; move C2 %.4f %.4f; move R1 %.4f %.4f" % tuple(
            [70 + (18 - 25) + 0.0, 40 - (46.5 - 50)] + [70 + (25.5 - 25), 40 - (58 - 50)] + [70 + (33 - 25), 40 - (50.5 - 50)]))
        self.assertAlmostEqual(self.drift(st)["rms"], 0.0, places=3)

    def test_reference_board(self):
        st = self.world()
        ref = copy.deepcopy(st.world.board)                                   # the reference pcb = this arrangement, shifted by (100, 7)
        for p in ref.parts.values():
            p.pos = (p.pos[0] + 100, p.pos[1] + 7)
            for d in p.pads:
                d.pos = (d.pos[0] + 100, d.pos[1] + 7)
        ref.parts["C1"].pos = (ref.parts["C1"].pos[0], ref.parts["C1"].pos[1] - 2.0)       # in the reference C1 sits 2 mm further out
        config.use({"intent": {"blocks": {"mcu": {"parts": ["U1", "C1", "C2", "R1"], "ref": {"pcb": "ref.kicad_pcb", "anchor": "U1"}}}}})
        w = A.World(st.world.board)
        C = intent.compile_intent(w, lambda path: ref)
        self.assertEqual(C.warnings, [])
        d = intent.drift(w, C.items[0])
        self.assertAlmostEqual(d["rms"], (4 / 3) ** 0.5, places=3)
        self.assertAlmostEqual(d["parts"]["C1"][0], 2.0, places=3)
        self.assertFalse(d["mirror"])


class AuditIntent(Base):
    CFG = {"near": [["C2", "U9", 3]], "clear": [{"zone": "escape:U9", "except": ["decaps:U9"]}], "edge": [["J1", "back", 2]],
           "blocks": {"flash": {"parts": ["U2"], "max_span_mm": 1}}}

    def test_findings_severity_and_fixes(self):
        st = stage(self.CFG)
        rep = audit.run(st, ["INTENT"])
        f = {x.title.split(":")[0].split(" ")[0] + x.handles["intent"]: x for x in rep.findings}
        self.assertEqual({x.det for x in rep.findings}, {"INTENT"})
        clr = [x for x in rep.findings if x.handles["intent"] == "clear1"][0]
        self.assertIn("J1", clr.handles["intruders"])
        self.assertIn(clr.sev, ("WARN", "ERR"))
        near = [x for x in rep.findings if x.handles["intent"] == "near1"][0]
        self.assertEqual(near.sev, "ERR")                                      # ~35 mm over x weight 1 >= intent_err
        self.assertEqual(near.handles["parts"], ["C2"])
        for x in (clr, near):
            self.assertTrue(x.best is not None, x.title)
            self.assertLess(x.best.res.d["intent"], 0)                         # every fix reduces the intent penalty and says so
        self.assertIn("move C2", near.best.spec)
        edge = [x for x in rep.findings if x.handles["intent"] == "edge1"]
        self.assertTrue(edge)                                                  # J1 is 19.5 mm from the back edge
        self.assertIn("INTENT", "\n".join(audit.text(rep)))

    def test_warn_below_threshold_and_quiet_without_intent(self):
        st = stage({"near": [["C2", "U9", 27]]})                               # C2 gap is ~28.7 mm: 1.7 mm excess
        rep = audit.run(st, ["INTENT"])
        self.assertEqual([x.sev for x in rep.findings], ["WARN"])
        self.assertEqual(audit.run(stage(None), ["INTENT"]).findings, [])

    def test_fix_is_applied_and_clears_the_finding(self):
        st = stage({"near": [["C2", "U9", 3]]})
        f = audit.run(st, ["INTENT"]).findings[0]
        st.act(f.best.spec)
        self.assertEqual(audit.run(st, ["INTENT"]).findings, [])
        self.assertEqual(st.metrics()["intent"], 0.0)

    def test_reference_drift_finding(self):
        st = Drift().world()
        st.act("intent capture mcu; move R1 50.5 40; move C1 46.5 12")
        rep = audit.run(st, ["INTENT"])
        self.assertEqual(len(rep.findings), 1)
        f = rep.findings[0]
        self.assertIn("off the reference", f.title)
        self.assertTrue(f.best and f.best.res.d["intent"] < 0)
        self.assertIn("move R1 50.5 33", f.best.spec.replace("50.50 33", "50.5 33"))

    def test_clear_zone_box_and_joint_cost(self):
        st = stage({"clear": [{"zone": [40, 15, 60, 24]}]})
        self.assertGreater(st.metrics()["intent"], 0)
        w = st.world
        far, _ = A.place_geom(w, "J1", (10, 5), 0, "F")
        near_, _ = A.place_geom(w, "J1", (50, 21), 0, "F")
        self.assertEqual(intent.part_cost(w, "J1", far), 0.0)
        self.assertGreater(intent.part_cost(w, "J1", near_), 0.0)


if __name__ == "__main__":
    unittest.main()
