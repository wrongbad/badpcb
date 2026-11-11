"""plane_layers(): a layer is a plane layer only when its netted zones cover a significant share of the board."""
import unittest

from badpcb import config
from tests import synth


def small_zone(net, layer, x0, y0, w, h):
    return {"layers": [layer], "net": net, "priority": 0,
            "polys": [{"outer": [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]], "holes": []}]}


def toy(zones, layers=("F.Cu", "In1.Cu", "B.Cu")):
    parts = [synth.part("R1", [synth.pad("1", (10, 10), "+1V1"), synth.pad("2", (12, 10), "GND")], pos=(11, 10))]
    return synth.board(parts, zones=zones, layers=layers)


class PlaneLayers(unittest.TestCase):
    def tearDown(self):
        config.use({})

    def test_small_outer_pour_is_not_a_plane_layer(self):
        b = toy([synth.zone("GND", "In1.Cu"), small_zone("+1V1", "F.Cu", 8, 8, 2, 2.5)])        # 5 mm2 of 5000
        self.assertEqual(b.plane_layers(), ["In1.Cu"])
        self.assertEqual(b.signal_layers(), ["F.Cu", "B.Cu"])
        self.assertEqual(b.plane_nets(), ["GND"])                  # the +1V1 pour does not make +1V1 a plane net
        rep = "\n".join(b.plane_layer_report())
        self.assertIn("F.Cu: not a plane layer", rep)
        self.assertIn("+1V1 5mm2", rep)
        self.assertIn("In1.Cu: PLANE layer", rep)

    def test_big_outer_pour_is_a_plane_layer(self):
        b = toy([synth.zone("GND", "F.Cu"), synth.zone("GND", "In1.Cu")])
        self.assertEqual(b.plane_layers(), ["F.Cu", "In1.Cu"])
        self.assertEqual(b.signal_layers(), ["B.Cu"])

    def test_split_plane_counts_all_its_zones(self):
        b = toy([small_zone("+3V3", "In1.Cu", 0, 0, 100, 20), small_zone("+3V3A", "In1.Cu", 0, 20, 100, 30)])
        self.assertEqual(b.plane_layers(), ["In1.Cu"])
        self.assertEqual(b.plane_nets(), ["+3V3", "+3V3A"])

    def test_threshold_is_configurable(self):
        zs = [small_zone("+1V1", "F.Cu", 8, 8, 2, 2.5)]
        self.assertEqual(toy(zs).plane_layers(), [])
        config.use({"planes": {"min_coverage": 0}})
        self.assertEqual(toy(zs).plane_layers(), ["F.Cu"])         # old behaviour: any netted zone
        config.use({"planes": {"min_coverage": 0.0001}})
        self.assertEqual(toy(zs).plane_layers(), ["F.Cu"])
        config.use({"planes": {"min_coverage": 0.5}})
        self.assertEqual(toy([small_zone("GND", "F.Cu", 0, 0, 100, 20)]).plane_layers(), [])      # 40% < 50%

    def test_unfilled_zone_outline_counts(self):
        b = toy([synth.zone("GND", "In1.Cu")])                      # synthetic zones have outlines only, no fill data
        self.assertEqual(b.plane_layers(), ["In1.Cu"])


if __name__ == "__main__":
    unittest.main()


class AuditHeader(unittest.TestCase):
    def test_audit_prints_board_path_mtime_and_zone_count(self):
        from badpcb import audit, state
        b = toy([synth.zone("GND", "In1.Cu"), small_zone("+1V1", "F.Cu", 8, 8, 2, 2.5)])
        st = state.State("t", "/nonexistent/board.kicad_pcb", b, [], 21.0)
        rep = audit.run(st, ["PLANE"])
        txt = "\n".join(audit.text(rep))
        self.assertIn("board: /nonexistent/board.kicad_pcb (file not found), 2 zones (2 netted", txt)
        self.assertIn("planes: GND", txt)
        self.assertIn("board", audit.as_json(rep))
        st0 = state.State("t", __file__, toy([]), [], 21.0)
        txt = "\n".join(audit.text(audit.run(st0, ["PLANE"])))
        self.assertIn("modified ", txt)
        self.assertIn("0 zones", txt)
        self.assertIn("no zones on this board", txt)
