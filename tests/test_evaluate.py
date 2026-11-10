"""evaluate: incremental == recompute from scratch, nothing committed, speed, suggest."""
import copy
import os
import random
import shutil
import tempfile
import time
import unittest

from rk import actions as A
from rk import engine as E
from rk import ratsnest, state, topo, evaluate
from tests import synth

PLACED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "key_board_placed.model.json")


def scratch_metrics(st):
    """Metrics of the current board from nothing (same frozen skeleton)."""
    w = A.World(copy.deepcopy(st.world.board))
    return E.Engine(w, st.skel, st.assume).metrics()


def close(a, b):
    for k in E.METRIC_KEYS:
        if abs(a[k] - b[k]) > 1e-3:
            return False, k
    return True, None




class TestToy(unittest.TestCase):
    def test_toy_crossing_delta(self):
        # two nets crossing; moving one pad pair removes the crossing
        parts = synth.singles([("A1", (0, 5), "A"), ("A2", (10, 5), "A"), ("B1", (5, 0), "B"), ("B2", (5, 10), "B")])
        b = synth.board(parts, size=(20, 20))
        for p in b.parts.values():
            p.bbox = (p.pos[0] - .5, p.pos[1] - .5, p.pos[0] + .5, p.pos[1] + .5)
        w = A.World(b, {"mcu": "U9", "groups": [], "pin_names": {}})
        skel = topo.Topo(b, ratsnest.Ratsnest(), 10.0, light=True)
        eng = E.Engine(w, skel)
        self.assertEqual(eng.metrics()["X"], 1)
        inv, nets = A.apply(w, A.parse("move B2 15 10")[0])
        tok = eng.refresh(nets)
        self.assertEqual(eng.metrics()["X"], 0)
        eng.restore(tok)
        self.assertEqual(eng.metrics()["X"], 1)


if __name__ == "__main__":
    unittest.main()
