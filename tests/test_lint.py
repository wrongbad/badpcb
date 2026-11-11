"""Route lint: each metric on a tiny synthetic board (answers known by construction), scoring, ranking, escalation."""
import json
import os
import unittest

from badpcb import config, lint as LT, styles as ST
from tests.synth import board, pad, part, singles, track, via, zone


def chain(pts, net, layer="F.Cu", w=0.2):
    return [track(a, b, net, layer, w) for a, b in zip(pts, pts[1:])]


def two_pads(net, a, b):
    return singles([(net + "1", a, net), (net + "2", b, net)])


def lint(b, styles=None, **kw):
    config.use({})
    return LT.lint_board(b, resolved=styles or {}, with_handles=False, **kw)


def only(L, net):
    return L.nets[net]


def kinds(nl):
    return {k for k, q, p, w in nl.items}


class TestDetourVias(unittest.TestCase):
    def test_straight_net_scores_zero(self):
        b = board(two_pads("N", (5, 10), (25, 10)), [track((5, 10), (25, 10), "N")])
        n = only(lint(b), "N")
        self.assertAlmostEqual(n.m["detour"], 1.0, 3)
        self.assertEqual(n.score, 0.0)
        self.assertEqual(n.m["vias"], 0)

    def test_detour_ratio_and_free_limit(self):
        # direct 20 mm, routed 20 up + 20 over... : (5,10)->(5,20)->(25,20)->(25,10) = 40 mm = ratio 2.0
        b = board(two_pads("N", (5, 10), (25, 10)), chain([(5, 10), (5, 20), (25, 20), (25, 10)], "N"))
        n = only(lint(b), "N")
        self.assertAlmostEqual(n.m["detour"], 2.0, 3)
        self.assertIn("detour", kinds(n))
        pts = [p for k, q, p, w in n.items if k == "detour"][0]
        self.assertAlmostEqual(pts, (2.0 - LT.DEFAULTS["detour_free"]) * LT.DEFAULTS["weights"]["detour"], 3)
        # a style with a looser budget forgives it, a tighter one costs more
        loose = ST.resolve(b.nets, ST.parse({"s": {"match": ["N"], "detour_max": 2.5}}))[0]
        self.assertNotIn("detour", kinds(only(lint(b, loose), "N")))
        tight = ST.resolve(b.nets, ST.parse({"s": {"match": ["N"], "detour_max": 1.1}}))[0]
        self.assertGreater(only(lint(b, tight), "N").score, n.score)

    def test_short_nets_are_not_judged_on_ratio(self):
        b = board(two_pads("N", (5, 10), (7, 10)), chain([(5, 10), (5, 12), (7, 12), (7, 10)], "N"))
        self.assertNotIn("detour", kinds(only(lint(b), "N")))

    def test_signal_vias_counted_plane_and_stitching_excluded(self):
        parts = two_pads("N", (5, 10), (25, 10)) + two_pads("G", (5, 30), (25, 30)) + two_pads("S", (5, 40), (25, 40))
        tr = chain([(5, 10), (15, 10)], "N") + chain([(15, 10), (25, 10)], "N", "B.Cu")
        tr += chain([(5, 40), (25, 40)], "S")
        vias = [via((15, 10), "N"),                 # signal via: a track of N ends on it
                via((60, 40), "N"),                 # stitching-like via of a signal net: no copper on it
                via((15, 30), "G"), via((16, 30), "G")]   # plane net vias
        zones = [zone("G", "In1.Cu")]
        b = board(parts, tr, vias, zones, layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), size=(100, 50))
        L = lint(b)
        self.assertEqual(only(L, "N").m["vias"], 1)
        self.assertNotIn("G", L.nets)               # plane nets are never linted
        self.assertEqual(only(L, "S").m["vias"], 0)

    def test_via_weight_follows_the_style_and_free_vias_scale_with_pads(self):
        parts = two_pads("N", (5, 10), (25, 10))
        tr = chain([(5, 10), (10, 10)], "N") + chain([(10, 10), (25, 10)], "N", "B.Cu")
        b = board(parts, tr, [via((10, 10), "N")])
        n = only(lint(b), "N")
        self.assertEqual(n.m["vias"], 1)
        self.assertAlmostEqual(n.score, LT.DEFAULTS["weights"]["via"], 3)
        st = ST.resolve(b.nets, ST.parse({"s": {"match": ["N"], "via_penalty": 6}}))[0]
        self.assertAlmostEqual(only(lint(b, st), "N").score, 2 * LT.DEFAULTS["weights"]["via"], 3)
        # a 6-pad net may keep floor(0.5 * 5) = 2 vias for free
        pads6 = singles([("P%d" % i, (5 + 3 * i, 10), "M") for i in range(6)])
        tr6 = chain([(5, 10), (20, 10)], "M") + chain([(20, 10), (20, 11)], "M", "B.Cu") + chain([(5, 12), (20, 12)], "M", "B.Cu")
        b6 = board(pads6, tr6, [via((20, 10), "M"), via((20, 11), "M")])
        self.assertEqual(only(lint(b6), "M").m["vias"], 2)
        self.assertNotIn("via", kinds(only(lint(b6), "M")))


class TestShape(unittest.TestCase):
    def poly(self, pts, net="N"):
        return board(two_pads(net, pts[0], pts[-1]), chain(pts, net))

    def test_octilinear_bend_is_not_a_corner(self):
        n = only(lint(self.poly([(5, 10), (15, 10), (20, 15), (30, 15)])), "N")
        self.assertEqual((n.m["acute"], n.m["c90"], n.m["bends"]), (0, 0, 2))

    def test_right_angle_corner(self):
        n = only(lint(self.poly([(5, 10), (15, 10), (15, 20)])), "N")
        self.assertEqual((n.m["acute"], n.m["c90"]), (0, 1))
        self.assertIn("corner90", kinds(n))

    def test_acute_corner(self):
        n = only(lint(self.poly([(5, 10), (15, 10), (7, 14)])), "N")
        self.assertEqual(n.m["acute"], 1)
        self.assertIn("acute", kinds(n))
        self.assertGreater(n.score, only(lint(self.poly([(5, 10), (15, 10), (15, 20)])), "N").score)

    def test_collinear_split_is_no_bend(self):
        n = only(lint(self.poly([(5, 10), (10, 10), (15, 10), (25, 10)])), "N")
        self.assertEqual(n.m["bends"], 0)

    def test_jog_is_a_short_segment_between_two_bends(self):
        # 0.4 mm wide steps: 0.2 mm track -> jog if < 0.6 mm
        n = only(lint(self.poly([(5, 10), (15, 10), (15.2, 10.2), (30, 10.2)])), "N")
        self.assertEqual(n.m["jogs"], 1)
        self.assertIn("jog", kinds(n))
        # a long diagonal between the same bends is not a jog
        n2 = only(lint(self.poly([(5, 10), (15, 10), (18, 13), (30, 13)])), "N")
        self.assertEqual(n2.m["jogs"], 0)

    def test_bends_per_10mm_above_free_level_cost(self):
        pts = [(5, 10)]
        for i in range(8):
            pts.append((pts[-1][0] + 1.5, pts[-1][1] + (1.5 if i % 2 == 0 else -1.5)))     # zig-zag: a bend every 2 mm
        b = self.poly(pts)
        n = only(lint(b), "N")
        self.assertGreater(n.m["bends10"], LT.DEFAULTS["bend_free_per10"])
        self.assertIn("bend", kinds(n))

    def test_pad_junction_is_not_a_bend(self):
        # two tracks meeting at the pad of the net's third pad: not a corner
        parts = singles([("A", (5, 10), "N"), ("B", (15, 10), "N"), ("C", (15, 20), "N")])
        b = board(parts, chain([(5, 10), (15, 10)], "N") + chain([(15, 10), (15, 20)], "N"))
        self.assertEqual(only(lint(b), "N").m["c90"], 0)


class TestDirection(unittest.TestCase):
    def test_wrong_way_segment_on_a_horizontal_layer(self):
        parts = two_pads("N", (5, 10), (5, 20)) + two_pads("H1", (5, 30), (45, 30)) + two_pads("H2", (5, 35), (45, 35))
        tr = chain([(5, 10), (5, 20)], "N") + chain([(5, 30), (45, 30)], "H1") + chain([(5, 35), (45, 35)], "H2")
        b = board(parts, tr)
        L = lint(b)
        self.assertEqual(only(L, "N").m["dirbreaks"], 1)
        self.assertEqual(only(L, "H1").m["dirbreaks"], 0)
        self.assertIn("dir", kinds(only(L, "N")))

    def test_no_dominant_direction_no_breaks(self):
        parts = two_pads("N", (5, 10), (5, 20)) + two_pads("M", (10, 30), (40, 30))
        tr = chain([(5, 10), (5, 20)], "N") + chain([(10, 30), (40, 30)], "M")   # H length 30 vs V 10: ratio 3 -> H dominates
        self.assertEqual(only(lint(board(parts, tr)), "N").m["dirbreaks"], 1)
        tr2 = chain([(5, 10), (5, 40)], "N") + chain([(10, 30), (40, 30)], "M")  # 30 vs 30: no dominant direction
        self.assertEqual(only(lint(board(parts, tr2)), "N").m["dirbreaks"], 0)

    def test_short_segments_never_count(self):
        parts = two_pads("N", (5, 10), (5, 11)) + two_pads("H1", (5, 30), (45, 30))
        tr = chain([(5, 10), (5, 11)], "N") + chain([(5, 30), (45, 30)], "H1")
        self.assertEqual(only(lint(board(parts, tr)), "N").m["dirbreaks"], 0)


def zone_box(net, layer, x0, x1, y0=0, y1=50, prio=0):
    return {"layers": [layer], "net": net, "priority": prio, "polys": [{"outer": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "holes": []}]}


L4 = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


class TestPlaneSplit(unittest.TestCase):
    def sig(self, zones, a=(20, 10), b_=(80, 10), layer="F.Cu"):
        parts = two_pads("S", a, b_)
        if layer != "F.Cu":
            parts = [dict(p, pads=[dict(q, layers=[layer]) for q in p["pads"]]) for p in parts]
        return board(parts, chain([a, b_], "S", layer), zones=zones, layers=L4, size=(100, 50))

    def test_track_over_one_plane_is_clean(self):
        n = only(lint(self.sig([zone("G", "In1.Cu", (100, 50))])), "S")
        self.assertEqual((n.m["split_mm"], n.m["split_x"]), (0.0, 0))

    def test_track_over_a_gap_in_the_reference_plane(self):
        z = [zone_box("G", "In1.Cu", 0, 40), zone_box("G", "In1.Cu", 60, 100)]
        n = only(lint(self.sig(z)), "S")
        self.assertGreater(n.m["split_mm"], 15.0)
        self.assertLess(n.m["split_mm"], 25.0)
        self.assertIn("split_mm", kinds(n))

    def test_track_crossing_between_zones_of_different_nets(self):
        z = [zone_box("V1", "In1.Cu", 0, 50), zone_box("V2", "In1.Cu", 50, 100)]
        n = only(lint(self.sig(z)), "S")
        self.assertEqual(n.m["split_x"], 1)
        self.assertAlmostEqual(n.m["split_mm"], 0.0)
        # same net on both sides (two zones, one net): no crossing
        z2 = [zone_box("V1", "In1.Cu", 0, 50), zone_box("V1", "In1.Cu", 50, 100)]
        self.assertEqual(only(lint(self.sig(z2)), "S").m["split_x"], 0)

    def test_overlap_resolved_by_priority(self):
        # V2 (higher priority) overlaps V1 in x 30..70: the track still hops V1 -> V2 -> ... only one change at x=30 then stays on V2
        z = [zone_box("V1", "In1.Cu", 0, 100, prio=0), zone_box("V2", "In1.Cu", 30, 100, prio=1)]
        self.assertEqual(only(lint(self.sig(z)), "S").m["split_x"], 1)

    def test_keep_ref_plane_triples_the_weight(self):
        z = [zone_box("V1", "In1.Cu", 0, 50), zone_box("V2", "In1.Cu", 50, 100)]
        b = self.sig(z)
        base = only(lint(b), "S").score
        st = ST.resolve(b.nets, ST.parse({"s": {"match": ["S"], "keep_ref_plane": True}}))[0]
        self.assertAlmostEqual(only(lint(b, st), "S").score, base * LT.DEFAULTS["ref_plane_scale"], 3)

    def test_bottom_layer_looks_at_the_plane_above_it(self):
        z = [zone_box("V1", "In2.Cu", 0, 50), zone_box("V2", "In2.Cu", 50, 100), zone("G", "In1.Cu", (100, 50))]
        n = only(lint(self.sig(z, layer="B.Cu")), "S")
        self.assertEqual(n.m["split_x"], 1)           # B.Cu is over In2 (split), not In1 (whole)

    def test_inner_layer_without_zones_is_skipped(self):
        b = self.sig([])
        n = only(lint(b), "S")
        self.assertEqual((n.m["split_mm"], n.m["split_x"]), (0.0, 0))


class TestPair(unittest.TestCase):
    def pair_board(self, p_pts, n_pts):
        parts = singles([("P1", p_pts[0], "DP"), ("P2", p_pts[-1], "DP"), ("N1", n_pts[0], "DN"), ("N2", n_pts[-1], "DN")])
        return board(parts, chain(p_pts, "DP") + chain(n_pts, "DN"), size=(100, 50))

    def styles(self, b, **kw):
        d = {"match": ["DP", "DN"], "pair": "DP/DN"}
        d.update(kw)
        return ST.resolve(b.nets, ST.parse({"usb": d}))[0]

    def test_tight_parallel_pair_is_clean(self):
        b = self.pair_board([(5, 10), (45, 10)], [(5, 10.5), (45, 10.5)])
        n = only(lint(b, self.styles(b)), "DP")
        self.assertAlmostEqual(n.m["skew"], 0.0, 3)
        self.assertAlmostEqual(n.m["gap_var"], 0.0, 3)
        self.assertEqual(n.score, 0.0)

    def test_skew_over_the_limit_costs_and_is_shared(self):
        b = self.pair_board([(5, 10), (45, 10)], [(5, 10.5), (40, 10.5)])
        st = self.styles(b, skew_mm=1.0)
        L = lint(b, st)
        self.assertAlmostEqual(only(L, "DP").m["skew"], 5.0, 2)
        self.assertIn("skew", kinds(only(L, "DP")))
        sk = lambda nl: sum(p for k, q, p, w in nl.items if k == "skew")
        self.assertAlmostEqual(sk(only(L, "DP")), sk(only(L, "DN")), 3)
        self.assertGreater(sk(only(L, "DP")), 0)
        # a generous skew budget removes it
        self.assertNotIn("skew", kinds(only(lint(b, self.styles(b, skew_mm=6.0)), "DP")))

    def test_gap_variation(self):
        b = self.pair_board([(5, 10), (45, 10)], [(5, 10.5), (25, 10.5), (45, 11.4)])
        n = only(lint(b, self.styles(b)), "DP")
        self.assertGreater(n.m["gap_var"], 0.5)
        self.assertIn("gap", kinds(n))

    def test_uncoupled_run(self):
        b = self.pair_board([(5, 10), (45, 10)], [(5, 10.5), (15, 10.5), (15, 25), (45, 25)])
        n = only(lint(b, self.styles(b)), "DP")
        self.assertGreater(n.m["uncoupled"], 0.5)
        self.assertIn("uncoupled", kinds(n))

    def test_pair_needs_a_pair_style(self):
        b = self.pair_board([(5, 10), (45, 10)], [(5, 10.5), (40, 10.5)])
        self.assertNotIn("pair", only(lint(b), "DP").m)


def qfn(ref, pos=(50, 20), n=16, pitch=0.4):
    """A tiny fine-pitch part: n/4 pads per side, pads named after nets Q<i>."""
    pads, k = [], 0
    per = n // 4
    half = (per - 1) * pitch / 2
    for side in range(4):
        for i in range(per):
            t = -half + i * pitch
            x, y = {0: (t, -2.0), 1: (2.0, t), 2: (t, 2.0), 3: (-2.0, t)}[side]
            pads.append(pad(str(k + 1), (pos[0] + x, pos[1] + y), "Q%d" % k, size=(0.2, 0.5) if side in (0, 2) else (0.5, 0.2)))
            k += 1
    return part(ref, pads, pos=pos, bbox=[pos[0] - 2.5, pos[1] - 2.5, pos[0] + 2.5, pos[1] + 2.5])


class TestEscapeZone(unittest.TestCase):
    def board(self, tracks):
        parts = [qfn("U1")] + two_pads("X", (30, 22.5), (70, 22.5))
        return board(parts, tracks, size=(100, 50))

    def test_foreign_copper_in_the_escape_zone(self):
        # south side pads at y = 22; zone runs from y ~22.25 outward ~2-3 mm: a foreign track at y=23.5 sits inside
        n = only(lint(self.board(chain([(30, 23.5), (70, 23.5)], "X"))), "X")
        self.assertGreater(n.m["esc_mm"], 1.0)
        self.assertTrue(n.m["esc_zone"].startswith("U1."))
        self.assertIn("esc_mm", kinds(n))

    def test_clear_of_the_zone_is_free(self):
        n = only(lint(self.board(chain([(30, 30), (70, 30)], "X"))), "X")
        self.assertEqual(n.m["esc_mm"], 0.0)

    def test_the_parts_own_nets_may_use_it(self):
        parts = [qfn("U1")] + singles([("T", (30, 23.5), "Q9"), ("T2", (70, 23.5), "Q9")])
        n = only(lint(board(parts, chain([(30, 23.5), (70, 23.5)], "Q9"), size=(100, 50))), "Q9")
        self.assertEqual(n.m["esc_mm"], 0.0)


class TestAvoid(unittest.TestCase):
    def test_length_close_to_a_net_to_avoid_costs(self):
        parts = two_pads("X", (5, 10), (45, 10)) + two_pads("A", (5, 10.6), (45, 10.6)) + two_pads("B", (5, 30), (45, 30))
        tr = chain([(5, 10), (45, 10)], "X") + chain([(5, 10.6), (45, 10.6)], "A") + chain([(5, 30), (45, 30)], "B")
        b = board(parts, tr)
        st = ST.resolve(b.nets, ST.parse({"s": {"match": ["X"], "avoid": ["A*", "B"]}}))[0]
        n = only(lint(b, st), "X")
        self.assertGreater(n.m["avoid_mm"], 35.0)
        self.assertIn("avoid_mm", kinds(n))
        far = ST.resolve(b.nets, ST.parse({"s": {"match": ["X"], "avoid": ["B"]}}))[0]
        self.assertEqual(only(lint(b, far), "X").m["avoid_mm"], 0.0)


class TestScoreAndTable(unittest.TestCase):
    def ugly_board(self, n_bad=1):
        parts, tr = [], []
        for i in range(n_bad):
            y = 5 + 17 * i
            parts += two_pads("U%d" % i, (5, y), (35, y))
            tr += chain([(5, y), (5, y + 15), (35, y + 15), (35, y)], "U%d" % i)     # detour 2.0 + two right angles + two wrong-way runs
        parts += two_pads("OK", (5, 190), (35, 190))
        tr += chain([(5, 190), (35, 190)], "OK")
        return board(parts, tr, size=(60, 200))

    def test_ranking_worst_first_and_total(self):
        L = lint(self.ugly_board(2))
        r = L.ranked()
        self.assertEqual(r[-1].net, "OK")
        self.assertEqual(r[-1].score, 0.0)
        self.assertAlmostEqual(L.total, sum(n.score for n in L.nets.values()), 6)
        self.assertEqual(len(L.bad()), 2)

    def test_weights_from_config(self):
        b = self.ugly_board(1)
        base = only(lint(b), "U0").score
        config.use({"lint": {"weights": {"corner90": 0.0}}})
        self.assertLess(only(LT.lint_board(b, resolved={}, with_handles=False), "U0").score, base)
        config.use({"lint": {"weights": {"bogus": 1}}})
        with self.assertRaises(SystemExit):
            LT.params()
        config.use({"lint": {"bogus": 1}})
        with self.assertRaises(SystemExit):
            LT.params()
        config.use({})

    def test_priority_scales_the_whole_net(self):
        b = self.ugly_board(1)
        st = ST.resolve(b.nets, ST.parse({"s": {"match": ["U0"], "priority": True}}))[0]
        self.assertAlmostEqual(only(lint(b, st), "U0").score, only(lint(b), "U0").score * LT.DEFAULTS["priority_scale"], 3)

    def test_table_and_json(self):
        L = lint(self.ugly_board(2))
        txt = "\n".join(LT.table(L, top=5, name="t"))
        self.assertIn("LINT t: 3 signal nets scored", txt)
        self.assertIn("U0", txt)
        self.assertIn("polish", txt)                          # few bad nets -> polish suggestion
        d = [n.as_dict() for n in L.ranked()]
        json.dumps(d)
        self.assertEqual(d[0]["net"], L.ranked()[0].net)

    def test_many_bad_nets_escalate_to_placement(self):
        L = lint(self.ugly_board(10))
        pp = L.placement_problem()
        self.assertTrue(pp)
        txt = "\n".join(LT.table(L))
        self.assertIn("placement problem", txt)
        self.assertNotIn("candidates for `badpcb droute", txt)

    def test_open_net_is_marked_and_not_judged_on_detour(self):
        parts = singles([("A", (5, 10), "N"), ("B", (45, 10), "N"), ("C", (45, 30), "N")])
        b = board(parts, chain([(5, 10), (5, 20), (45, 20), (45, 10)], "N"))     # C is not connected: net is open
        n = only(lint(b), "N")
        self.assertTrue(n.open)
        self.assertNotIn("detour", kinds(n))

    def test_nets_without_copper_or_single_pad_are_skipped(self):
        parts = two_pads("N", (5, 10), (25, 10)) + singles([("Z", (50, 40), "Z")])
        L = lint(board(parts, chain([(5, 10), (25, 10)], "N") + chain([(50, 40), (55, 40)], "Z")))
        self.assertEqual(sorted(L.nets), ["N"])

    def test_handle_uses_region_or_cut_names(self):
        b = self.ugly_board(1)
        config.use({})
        L = LT.lint_board(b, resolved={})
        h = only(L, "U0").handle
        self.assertRegex(h, r"^(R\d+|C\d+|\S+-in)$")


class TestCli(unittest.TestCase):
    FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "kicad10_mini.kicad_pcb")

    def run_cli(self, *argv):
        import contextlib
        import io
        import tempfile
        from badpcb import cli
        buf = io.StringIO()
        old = os.environ.get("BADPCB_HOME")
        with tempfile.TemporaryDirectory() as td:
            os.environ["BADPCB_HOME"] = td
            try:
                with contextlib.redirect_stdout(buf):
                    cli.main(list(argv))
            finally:
                config.use({})
                if old is None:
                    os.environ.pop("BADPCB_HOME", None)
                else:
                    os.environ["BADPCB_HOME"] = old
        return buf.getvalue()

    def test_lint_command_text_and_json(self):
        self.assertIn("LINT", self.run_cli("lint", self.FIX))
        d = json.loads(self.run_cli("lint", self.FIX, "--json"))
        self.assertIn("nets", d)
        self.assertIn("total", d)

    def test_styles_command(self):
        self.assertIn("unchanged behaviour", self.run_cli("styles", self.FIX))
        self.assertIn("nets", json.loads(self.run_cli("styles", self.FIX, "--json")))

    def test_report_has_a_lint_section(self):
        self.assertIn("== lint ==", self.run_cli("report", self.FIX, "--section", "lint"))


if __name__ == "__main__":
    unittest.main()
