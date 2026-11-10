"""Project config: discovery, defaults, paths, state directory, tool lookup (no KiCad needed)."""
import json
import os
import shutil
import tempfile
import unittest

from badpcb import config
from tests import synth


def qfn_part(ref, n=20, pitch=0.4):
    pads = [synth.pad(str(i + 1), ((i % (n // 2)) * pitch, 0.0 if i < n // 2 else 3.0), "") for i in range(n)]
    return synth.part(ref, pads, pos=(0, 0))


class TestProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("BADPCB_PROJECT", "BADPCB_HOME", "KRT_DIR", "KRT_PY", "KIPY", "KICAD_CLI")}
        for k in self._env:
            os.environ.pop(k, None)
        self._cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._cwd)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.use({})
        shutil.rmtree(self.tmp)

    def test_defaults_without_project(self):
        P = config.use({}, root=self.tmp)
        self.assertEqual(P.swaps(), {"mcu": "", "pin_names": {}, "groups": []})
        self.assertEqual(P.lock_classes(), {})
        self.assertEqual(P.plane_hints(), [])
        self.assertIsNone(P.rules_pro)
        self.assertEqual(P.rules_pro_for("/x/b.kicad_pcb"), "/x/b.kicad_pro")
        self.assertEqual(P.home, os.path.join(self.tmp, ".badpcb"))

    def test_discovery_walks_up_and_resolves_relative_paths(self):
        sub = os.path.join(self.tmp, "a", "b")
        os.makedirs(sub)
        json.dump({"board": "x.kicad_pcb", "rules": {"project": "x.kicad_pro"}, "mcu": "U7",
                   "swaps": {"mcu": "U7", "pin_names": {"1": "A"}, "groups": []},
                   "locks": {"classes": {"lane": "lanes.json", "mimic": "m.json:copied"}}}, open(os.path.join(self.tmp, "badpcb.json"), "w"))
        os.chdir(sub)
        P = config.load()
        self.assertEqual(os.path.realpath(P.root), os.path.realpath(self.tmp))
        self.assertTrue(P.board.endswith("x.kicad_pcb") and os.path.isabs(P.board))
        self.assertEqual(P.mcu, "U7")
        self.assertEqual(P.swaps()["pin_names"], {"1": "A"})
        lc = P.lock_classes()
        self.assertEqual(list(lc), ["lane", "mimic"])
        self.assertEqual(lc["mimic"][1], "copied")
        self.assertEqual(P.rules_pro_for("whatever"), P.rules_pro)

    def test_rk_home_env_overrides_state_dir(self):
        P = config.use({}, root=self.tmp)
        os.environ["BADPCB_HOME"] = os.path.join(self.tmp, "elsewhere")
        self.assertEqual(P.sessions_dir, os.path.join(self.tmp, "elsewhere", "sessions"))
        self.assertEqual(P.routes_dir, os.path.join(self.tmp, "elsewhere", "routes"))
        self.assertEqual(P.cache_dir, os.path.join(self.tmp, "elsewhere", "cache"))

    def test_env_beats_config_for_tools(self):
        P = config.use({"krt": {"dir": "tools/krt", "python": "tools/py"}}, root=self.tmp)
        self.assertEqual(P.krt_dir(), os.path.join(self.tmp, "tools", "krt"))
        os.environ["KRT_DIR"] = "/opt/krt"
        os.environ["KRT_PY"] = "/opt/py"
        self.assertEqual((P.krt_dir(), P.krt_py()), ("/opt/krt", "/opt/py"))
        os.environ["KIPY"] = "/opt/kipy"
        os.environ["KICAD_CLI"] = "/opt/cli"
        self.assertEqual((config.kipy(), config.kicad_cli()), ("/opt/kipy", "/opt/cli"))

    def test_auto_fine_pitch_detection(self):
        b = synth.board([qfn_part("U5"), synth.part("R1", [synth.pad("1", (50, 5), "N"), synth.pad("2", (51, 5), "M")], pos=(50, 5))])
        self.assertEqual(config.auto_fine_parts(b), ["U5"])
        self.assertEqual(config.use({}).fine_pitch_parts(b), ["U5"])
        self.assertEqual(config.use({"fine_pitch_parts": ["R1"]}).fine_pitch_parts(b), ["R1"])

    def test_short_paths_only_inside_a_project_file(self):
        P = config.use({}, root=self.tmp)
        self.assertEqual(P.short(os.path.join(self.tmp, "b.kicad_pcb")), os.path.join(self.tmp, "b.kicad_pcb"))   # no badpcb.json: stay absolute
        P2 = config.Project(os.path.join(self.tmp, "badpcb.json"), {})
        self.assertEqual(P2.short(os.path.join(self.tmp, "sub", "b.kicad_pcb")), os.path.join("sub", "b.kicad_pcb"))
        self.assertEqual(P2.short("/elsewhere/b.kicad_pcb"), "/elsewhere/b.kicad_pcb")


if __name__ == "__main__":
    unittest.main()
