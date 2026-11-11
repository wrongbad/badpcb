"""Polish accept/reject logic with mocked routing, lint, measure and DRC (no KRT, no kicad-cli)."""
import os
import tempfile
import unittest

from badpcb import polish as PO


class FakeNet:
    def __init__(self, net, score, open_=False, handle="R1"):
        self.net, self.score, self.open, self.handle = net, score, open_, handle
        self.m = {"vias": 3, "jogs": 1, "acute": 0, "c90": 0, "split_mm": 0.0, "split_x": 0, "detour": 1.2}


class FakeLint:
    def __init__(self, scores, open_=()):
        self.nets = {n: FakeNet(n, s, n in open_) for n, s in scores.items()}
        self.total = sum(scores.values())

    def ranked(self, only=None):
        return sorted(self.nets.values(), key=lambda n: (-n.score, n.net))


class Scenario:
    """Boards are dicts keyed by path: {"scores", "unrouted", "drc", "unconnected", "frozen_ok"}. reroute(path, group, out) derives `out` from
    the plan for that group."""

    def __init__(self, start, plan):
        self.boards = {"start": start}
        self.plan = plan                     # group tuple -> dict(overrides) or None (reroute fails)
        self.calls = []
        self.drc_runs = 0

    def env(self):
        def lint(path):
            return FakeLint(self.boards[path]["scores"], self.boards[path].get("open", ()))

        def measure(path, drc):
            b = self.boards[path]
            if drc:
                self.drc_runs += 1
            m = {"unrouted_conns": b["unrouted"]}
            if drc:
                m.update(drc_errors=b["drc"], drc_unconnected=b["unconnected"])
            return m

        def reroute(path, group, out, cap):
            self.calls.append((tuple(group), cap))
            p = self.plan.get(tuple(group))
            if p is None:
                return False
            nb = dict(self.boards[path])
            nb["scores"] = dict(nb["scores"])
            nb["scores"].update(p.get("scores", {}))
            for k in ("unrouted", "drc", "unconnected", "frozen_ok"):
                if k in p:
                    nb[k] = p[k]
            nb.setdefault("frozen_ok", True)
            self.boards[out] = nb
            return True

        def frozen(a, b, group):
            return self.boards[b].get("frozen_ok", True)
        return PO.Env(lint, measure, reroute, frozen)


START = {"scores": {"A": 40.0, "B": 25.0, "C": 12.0, "D": 3.0, "E": 0.0}, "unrouted": 0, "drc": 5, "unconnected": 0}


def run(plan, n=3, start=START, **kw):
    sc = Scenario(dict(start), plan)
    out = []
    with tempfile.TemporaryDirectory() as td:
        sc.boards[os.path.join(td, "polish1.kicad_pcb")] = None
        res = PO.polish("start", n, sc.env(), td, log=out.append, **kw)
    return sc, res, out


class TestPick(unittest.TestCase):
    def test_worst_first_skips_open_and_zero(self):
        L = FakeLint({"A": 40, "B": 25, "C": 0, "D": 12}, open_=("B",))
        self.assertEqual([g for _, g in PO.groups(L, 5, min_score=0.0)], [["A"], ["D"]])
        self.assertEqual([g for _, g in PO.groups(L, 1)], [["A"]])

    def test_pairs_are_one_group(self):
        L = FakeLint({"P": 30, "N": 20, "X": 10})
        partner = lambda n: {"P": "N", "N": "P"}.get(n)
        g = PO.groups(L, 5, partner)
        self.assertEqual([x for _, x in g], [["P", "N"], ["X"]])
        self.assertEqual(g[0][0], 50)


class TestAcceptReject(unittest.TestCase):
    def test_accept_when_better_and_nothing_worse(self):
        sc, res, log = run({("A",): {"scores": {"A": 10.0}}}, n=1)
        self.assertEqual(res["after"], 10 + 25 + 12 + 3 + 0)
        self.assertTrue(res["steps"][0]["accepted"])
        self.assertNotEqual(res["board"], "start")
        self.assertTrue(any("ACCEPT" in l for l in log))

    def test_reject_unrouted_increase(self):
        sc, res, log = run({("A",): {"scores": {"A": 10.0}, "unrouted": 1}}, n=1)
        self.assertFalse(res["steps"][0]["accepted"])
        self.assertIn("unrouted 0 -> 1", res["steps"][0]["why"])
        self.assertEqual(res["board"], "start")
        self.assertEqual(sc.drc_runs, 1)               # only the baseline DRC: the cheap checks reject before the slow one

    def test_reject_no_lint_gain(self):
        sc, res, _ = run({("A",): {"scores": {"A": 39.8}}}, n=1)
        self.assertFalse(res["steps"][0]["accepted"])
        self.assertIn("no gain", res["steps"][0]["why"])
        sc, res, _ = run({("A",): {"scores": {"A": 55.0}}}, n=1)
        self.assertFalse(res["steps"][0]["accepted"])

    def test_reject_drc_errors_or_unconnected_up(self):
        for k in ({"drc": 6}, {"unconnected": 1}):
            plan = {("A",): dict({"scores": {"A": 5.0}}, **k)}
            sc, res, _ = run(plan, n=1)
            self.assertFalse(res["steps"][0]["accepted"], k)
            self.assertIn("DRC", res["steps"][0]["why"])
            self.assertEqual(sc.drc_runs, 2)

    def test_equal_drc_is_fine_and_fewer_is_better(self):
        sc, res, _ = run({("A",): {"scores": {"A": 5.0}, "drc": 5}}, n=1)
        self.assertTrue(res["steps"][0]["accepted"])
        sc, res, _ = run({("A",): {"scores": {"A": 5.0}, "drc": 3}}, n=1)
        self.assertTrue(res["steps"][0]["accepted"])
        self.assertEqual(res["metrics"]["drc_errors"], 3)

    def test_reject_when_another_nets_copper_moved(self):
        sc, res, _ = run({("A",): {"scores": {"A": 5.0}, "frozen_ok": False}}, n=1)
        self.assertFalse(res["steps"][0]["accepted"])
        self.assertIn("other nets", res["steps"][0]["why"])

    def test_failed_reroute_and_crash_are_rejections_not_errors(self):
        sc, res, _ = run({("A",): None}, n=1)
        self.assertIn("no board", res["steps"][0]["why"])
        sc = Scenario(dict(START), {})
        env = sc.env()
        env.reroute = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        with tempfile.TemporaryDirectory() as td:
            res = PO.polish("start", 1, env, td, log=lambda s: None)
        self.assertIn("boom", res["steps"][0]["why"])
        self.assertEqual(res["board"], "start")

    def test_chain_baseline_moves_with_accepts(self):
        # A accepted lowers drc to 3; B then may not exceed 3 (it was allowed 5 at the start)
        plan = {("A",): {"scores": {"A": 5.0}, "drc": 3}, ("B",): {"scores": {"B": 5.0}, "drc": 4}, ("C",): {"scores": {"C": 5.0}, "drc": 3}}
        sc, res, _ = run(plan, n=3)
        self.assertEqual([s["accepted"] for s in res["steps"]], [True, False, True])
        self.assertEqual(res["metrics"]["drc_errors"], 3)
        self.assertAlmostEqual(res["after"], 5 + 25 + 5 + 3)

    def test_time_box_stops_the_loop(self):
        t = [0.0]

        def clock():
            t[0] += 100.0
            return t[0]
        plan = {("A",): {"scores": {"A": 5.0}}, ("B",): {"scores": {"B": 5.0}}, ("C",): {"scores": {"C": 5.0}}}
        sc, res, log = run(plan, n=3, budget_s=250.0, clock=clock)
        self.assertLess(len(res["steps"]), 3)
        self.assertTrue(any("time box" in l for l in log))

    def test_cap_is_shared_over_the_remaining_groups(self):
        plan = {("A",): {"scores": {"A": 5.0}}, ("B",): {"scores": {"B": 5.0}}}
        sc, res, _ = run(plan, n=2, budget_s=100.0, cap_s=120.0)
        self.assertLessEqual(sc.calls[0][1], 50.0 + 1e-9)
        self.assertGreaterEqual(sc.calls[0][1], 20.0)


class TestStyleArgs(unittest.TestCase):
    def test_via_cost_and_keep_away_for_a_scope(self):
        from badpcb import droute, styles as ST
        st = ST.parse({"a": {"match": ["ADC*"], "via_penalty": 3, "avoid": ["XIN", "XOUT"]}, "b": {"match": ["VREG_LX"], "via_penalty": 9, "priority": True}})
        r, _ = ST.resolve(["ADC0", "ADC1", "VREG_LX", "XIN", "XOUT", "FOO"], st)
        a, notes = droute.style_krt_args(["ADC0", "ADC1"], r)
        self.assertEqual(a[:2], ["--via-cost", "105"])
        self.assertIn("--keep-away", a)
        self.assertEqual(a[a.index("--keep-away") + 1], "ADC0,ADC1:XIN,XOUT:1")
        # a scope with an unstyled net keeps KRT's default via cost; the priority pass uses the strictest
        a2, _ = droute.style_krt_args(["ADC0", "FOO"], r)
        self.assertNotIn("--via-cost", a2)
        a3, _ = droute.style_krt_args(["ADC0", "VREG_LX"], r, priority=True)
        self.assertEqual(a3[:2], ["--via-cost", "165"])
        self.assertIn("--turn-cost", a3)
        self.assertEqual(droute.style_krt_args(["FOO"], r)[0], [])
        self.assertEqual(droute.style_krt_args(["ADC0"], {})[0], [])


if __name__ == "__main__":
    unittest.main()
