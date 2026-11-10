"""Sessions + CLI end to end on a synthetic board written as a model.json (no KiCad): state lives under $BADPCB_HOME, not the package."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from badpcb import apply as AP, cli, config, state
from tests import synth


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def write_model(path):
    parts = [synth.part("R1", [synth.pad("1", (10, 10), "N1"), synth.pad("2", (11, 10), "N2")], pos=(10.5, 10), bbox=[9, 9, 12, 11]),
             synth.part("R2", [synth.pad("1", (30, 10), "N1"), synth.pad("2", (31, 10), "N2")], pos=(30.5, 10), bbox=[29, 9, 32, 11]),
             synth.part("R3", [synth.pad("1", (20, 20), "N3"), synth.pad("2", (21, 20), "N3")], pos=(20.5, 20), bbox=[19, 19, 22, 21])]
    b = synth.board(parts, size=(60, 40))
    from badpcb import model
    import dataclasses
    d = {"source": "synthetic", "bbox": list(b.bbox), "edge": [], "layers": b.layers, "rules": dataclasses.asdict(b.rules), "nets": b.nets,
         "parts": [{"ref": p.ref, "value": p.value, "fpid": p.fpid, "pos": list(p.pos), "rot": p.rot, "side": p.side, "locked": p.locked,
                    "bbox": list(p.bbox), "pads": [{"num": q.num, "pos": list(q.pos), "layers": q.layers, "shape": q.shape, "size": list(q.size),
                                                    "rot": q.rot, "net": q.net, "drill": q.drill, "npth": False, "smd": q.smd} for q in p.pads]}
                   for p in b.parts.values()],
         "tracks": [], "vias": [], "zones": [], "keepouts": [], "holes": []}
    json.dump(d, open(path, "w"))


class TestSessionsAndCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)
        json.dump({"mcu": ""}, open(os.path.join(self.proj, "badpcb.json"), "w"))
        self.model = os.path.join(self.proj, "toy.model.json")
        write_model(self.model)
        self._home = os.environ.get("BADPCB_HOME")
        os.environ.pop("BADPCB_HOME", None)
        self._sd = state.SESSION_DIR
        state.SESSION_DIR = None
        self._proj_env = os.environ.get("BADPCB_PROJECT")
        os.environ["BADPCB_PROJECT"] = self.proj
        config.load(self.proj)

    def tearDown(self):
        os.environ.pop("BADPCB_PROJECT", None)
        if self._proj_env:
            os.environ["BADPCB_PROJECT"] = self._proj_env
        state.SESSION_DIR = self._sd
        if self._home is not None:
            os.environ["BADPCB_HOME"] = self._home
        config.use({})
        shutil.rmtree(self.tmp)

    def test_session_files_live_in_project_dot_rk(self):
        code, out, err = run("session", "new", "s1", self.model)
        self.assertEqual(code, 0, err)
        sf = os.path.join(self.proj, ".badpcb", "sessions", "s1.json")
        self.assertTrue(os.path.exists(sf), out)
        d = json.load(open(sf))
        self.assertEqual(d["board"], "toy.model.json")               # stored relative to the project: portable
        code, out, _ = run("act", "s1", "move R1 15 10")
        self.assertEqual(code, 0)
        code, out, _ = run("session", "ls")
        self.assertIn("s1", out)
        st = state.State.load("s1")                                  # replay from the relative path
        self.assertEqual(st.step(), 1)
        self.assertEqual(st.world.board.parts["R1"].pos[0], 15)
        run("undo", "s1")
        self.assertEqual(state.State.load("s1").step(), 0)

    def test_nothing_written_into_the_package(self):
        pkg = os.path.dirname(os.path.abspath(config.__file__))
        before = {f for f in os.listdir(pkg)}
        run("session", "new", "s2", self.model)
        run("report", self.model, "--section", "summary")
        self.assertEqual({f for f in os.listdir(pkg) if not f.startswith("__pycache__")}, {f for f in before if not f.startswith("__pycache__")})

    def test_rk_home_redirects_sessions(self):
        os.environ["BADPCB_HOME"] = os.path.join(self.tmp, "home")
        try:
            run("session", "new", "s3", self.model)
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "home", "sessions", "s3.json")))
        finally:
            os.environ.pop("BADPCB_HOME")

    def test_report_runs_without_mcu_or_swaps(self):
        code, out, err = run("report", self.model)
        self.assertEqual(code, 0, err)
        self.assertIn("== summary ==", out)
        self.assertIn("METRICS unrouted=", out)
        self.assertIn("== mcu ==", out)

    def test_project_command(self):
        code, out, _ = run("project")
        self.assertEqual(code, 0)
        self.assertIn("state dir", out)
        self.assertIn(os.path.join(self.proj, ".badpcb"), out)

    def test_keys_action_needs_a_configured_group(self):
        run("session", "new", "s4", self.model)
        code, out, err = run("act", "s4", "keys R1 R2")
        self.assertEqual(code, 2)
        self.assertIn("key_assignment", err)

    def test_swaps_md_is_generic(self):
        P = {"session": "s", "pads": [{"ref": "U9", "num": "3", "old": "A", "net": "B"}, {"ref": "U9", "num": "4", "old": "B", "net": "A"}],
             "parts": [{"ref": "C1"}]}
        md = AP.swaps_md(P, {"mcu": "U9", "pin_names": {"3": "GPIO3", "4": "GPIO4"}, "groups": []})
        self.assertIn("| A | 3 | GPIO3 | 4 | GPIO4 |", md)
        self.assertNotIn("key_board", md)
        self.assertIn("C1", md)


if __name__ == "__main__":
    unittest.main()
