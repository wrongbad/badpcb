"""net_styles: parsing, resolution by name / KiCad netclass, stacking, pairs, the `styles` table."""
import json
import os
import shutil
import tempfile
import unittest

from badpcb import config, styles as ST
from tests.synth import board, singles


def pro(tmp, patterns=(), assignments=None):
    p = os.path.join(tmp, "b.kicad_pro")
    with open(p, "w") as fh:
        json.dump({"net_settings": {"classes": [{"name": "Default"}, {"name": "HS"}, {"name": "Power"}],
                                    "netclass_patterns": [{"netclass": c, "pattern": g} for g, c in patterns],
                                    "netclass_assignments": assignments or {}}}, fh)
    return p


class TestParse(unittest.TestCase):
    def test_defaults_and_normalised(self):
        s = ST.parse({"a": {"match": "X*", "via_penalty": 4}})
        self.assertEqual(s["a"]["match"], ["X*"])
        self.assertEqual(s["a"]["via_penalty"], 4.0)
        self.assertIsNone(s["a"]["detour_max"])
        self.assertFalse(s["a"]["priority"])
        self.assertEqual(ST.parse(None), {})

    def test_rejects_unknown_and_bad_values(self):
        for bad in ({"a": {"match": ["X"], "bogus": 1}}, {"a": {"match": ["X"], "detour_max": 0.5}}, {"a": {"via_penalty": 3}},
                    {"a": {"match": ["X"], "via_penalty": -1}}, {"a": {"match": ["X"], "pair": 3}}, {"a": "x"}):
            with self.assertRaises(ST.StyleError, msg=str(bad)):
                ST.parse(bad)

    def test_config_accessor(self):
        P = config.use({"net_styles": {"a": {"match": ["X*"]}}})
        self.assertIn("a", P.net_styles())
        config.use({})
        self.assertEqual(config.current().net_styles(), {})


class TestResolve(unittest.TestCase):
    NETS = ["USB_DP", "USB_DN", "VREG_LX", "XIN", "XOUT", "ADC0", "ADC1", "SENSE0", "GND"]

    def test_match_by_glob_and_exact(self):
        st = ST.parse({"adc": {"match": ["ADC*"], "via_penalty": 3}, "lx": {"match": ["VREG_LX"], "detour_max": 1.2}})
        r, cnt = ST.resolve(self.NETS, st)
        self.assertEqual(sorted(r), ["ADC0", "ADC1", "VREG_LX"])
        self.assertEqual(r["VREG_LX"].detour_max, 1.2)
        self.assertEqual(cnt, {"adc": 2, "lx": 1})

    def test_stacking_takes_the_stricter_value(self):
        st = ST.parse({"a": {"match": ["ADC*"], "via_penalty": 3, "detour_max": 1.5, "avoid": ["XIN"], "skew_mm": 1.0},
                       "b": {"match": ["ADC0"], "via_penalty": 9, "detour_max": 1.2, "avoid": ["XOUT", "XIN"], "skew_mm": 0.3,
                             "keep_ref_plane": True, "priority": True}})
        r, _ = ST.resolve(self.NETS, st)
        s = r["ADC0"]
        self.assertEqual((s.via_penalty, s.detour_max, s.skew_mm), (9.0, 1.2, 0.3))
        self.assertEqual(s.avoid, ["XIN", "XOUT"])
        self.assertTrue(s.keep_ref_plane and s.priority)
        self.assertEqual(s.styles, ["a", "b"])
        s1 = r["ADC1"]                      # matched only a
        self.assertEqual((s1.via_penalty, s1.detour_max, s1.priority), (3.0, 1.5, False))

    def test_pair_members_get_the_style_and_partner(self):
        st = ST.parse({"usb": {"pair": "USB_DP/USB_DN", "skew_mm": 0.5}})
        r, _ = ST.resolve(self.NETS, st)
        self.assertEqual(sorted(r), ["USB_DN", "USB_DP"])
        self.assertEqual(r["USB_DP"].partner(), "USB_DN")
        self.assertEqual(r["USB_DN"].partner(), "USB_DP")
        self.assertEqual(ST.priority_nets(r), ["USB_DN", "USB_DP"])         # a pair is constrained -> priority pass

    def test_split_pair_with_slashes_in_names(self):
        nets = ["/USB_DP", "/USB_DN"]
        self.assertEqual(ST.split_pair("/USB_DP//USB_DN", nets), ("/USB_DP", "/USB_DN"))
        self.assertEqual(ST.split_pair("/USB_DP/USB_DN", nets), ("/USB_DP", "/USB_DN"))
        self.assertEqual(ST.split_pair(["/USB_DP", "/USB_DN"], nets), ("/USB_DP", "/USB_DN"))
        self.assertIsNone(ST.split_pair("A/B", nets))

    def test_priority_by_flag_or_via_penalty(self):
        st = ST.parse({"a": {"match": ["ADC0"], "priority": True}, "b": {"match": ["XIN"], "via_penalty": ST.PRIORITY_VIA},
                       "c": {"match": ["XOUT"], "via_penalty": ST.PRIORITY_VIA - 1}})
        r, _ = ST.resolve(self.NETS, st)
        self.assertEqual(ST.priority_nets(r), ["ADC0", "XIN"])


class TestNetclass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_read_patterns_and_assignments(self):
        p = pro(self.tmp, patterns=[("USB_*", "HS")], assignments={"VREG_LX": "Power", "XIN": ["Power", "HS"]})
        got = ST.netclasses_of(["USB_DP", "VREG_LX", "XIN", "ADC0"], p)
        self.assertEqual(got, {"USB_DP": ["HS"], "VREG_LX": ["Power"], "XIN": ["HS", "Power"]})

    def test_missing_or_empty_pro(self):
        self.assertEqual(ST.netclasses_of(["A"], os.path.join(self.tmp, "none.kicad_pro")), {})
        self.assertEqual(ST.netclasses_of(["A"], None), {})

    def test_style_by_class_or_name(self):
        p = pro(self.tmp, patterns=[("USB_*", "HS")], assignments={"VREG_LX": "Power"})
        st = ST.parse({"hs": {"class": "HS", "via_penalty": 5}, "pw": {"class": "Power", "match": ["XIN"], "via_penalty": 2}})
        r, cnt = ST.resolve(TestResolve.NETS, st, p)
        self.assertEqual(sorted(r), ["USB_DN", "USB_DP", "VREG_LX", "XIN"])
        self.assertEqual(r["USB_DP"].via, "class")
        self.assertEqual(r["USB_DP"].classes, ["HS"])
        self.assertEqual(r["XIN"].via, "name")           # by name: the class is not needed
        self.assertEqual(r["VREG_LX"].via_penalty, 2.0)

    def test_board_pro_is_the_boards_sibling(self):
        p = pro(self.tmp)
        b = board(singles([("A", (1, 1), "N"), ("B", (2, 2), "N")]))
        b.source = os.path.join(self.tmp, "b.kicad_pcb")
        self.assertEqual(ST.board_pro(b), p)


class TestTable(unittest.TestCase):
    def test_table_lists_styled_nets_and_dead_styles(self):
        b = board(singles([("A", (1, 1), "ADC0"), ("B", (9, 1), "ADC0"), ("C", (3, 3), "X")]))
        st = ST.parse({"adc": {"match": ["ADC*"], "via_penalty": 3}, "none": {"match": ["ZZZ"]}})
        r, cnt = ST.resolve(b.nets, st)
        txt = "\n".join(ST.table(b, r, cnt, st, None))
        self.assertIn("ADC0", txt)
        self.assertNotIn("\nX ", txt)
        self.assertIn("styles matching no net: none", txt)

    def test_no_styles_message(self):
        b = board(singles([("A", (1, 1), "N"), ("B", (2, 2), "N")]))
        txt = "\n".join(ST.table(b, {}, {}, {}, None))
        self.assertIn("unchanged behaviour", txt)


if __name__ == "__main__":
    unittest.main()
