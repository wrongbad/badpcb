"""Session state: base board + action log (+ hints, later routes), with snapshots, branches, revert and diff.

    st = State.new("s1", "key_board_placed.kicad_pcb")      # writes .badpcb/sessions/s1.json
    st = State.load("s1")                                    # replays the log on the base board (~0.5 s)
    r = st.act("move R17 152 40.5")                          # atomic; returns {"before","after","delta","warnings",...}
    st.undo(); st.snapshot("a"); st.revert("a"); st.revert(3); st.branch("alt"); st.switch("main")
    st.diff("base", "current")

The log is a list of entries = one `act` call each: {"acts": [action dicts], "m": metrics after, "warn": [...]}.
`step n` means "after the first n entries". The session file holds everything needed to continue in another process:
board path, planes assumption, log, snapshots, branches. The board itself is never written (apply.py is Phase 4).
"""
import json
import os
import re
import time

from . import actions as A
from . import engine as E
from . import ratsnest, topo

from . import config

SESSION_DIR = None          # override (tests); default: <badpcb home>/sessions


def session_dir():
    return SESSION_DIR or config.current().sessions_dir


def session_path(name):
    if name.endswith(".json") and os.sep in name:
        return name
    return os.path.join(session_dir(), name + ".json")


def _delta(m0, m1):
    return {k: round(m1.get(k, 0) - m0.get(k, 0), 3) for k in E.METRIC_KEYS}


def fmt_metrics(m):
    return ("X=%d Xcu=%d(%d locked) wl=%.0fmm ovf=%d/%d inv=%d pin_inv=%d ang=%.0f stubs=%d unrouted=%d" % (
        m["X"], m["Xcu"], m.get("Xcu_locked", 0), m["wirelength"], m["overflow_sum"], m["overflow_max"], m["inversions"], m["pin_inv"],
        m["ang_deg"], m["stubs"], m["unrouted"]) + (" intent=%.1f" % m["intent"] if m.get("intent_n") else ""))


def fmt_delta(d):
    """Compact signed deltas, nonzero only: 'X -12 Xcu +3 wl -33.1mm ...' (or 'no change')."""
    names = [("X", "X", "%+d"), ("Xcu", "Xcu", "%+d"), ("wirelength", "wl", "%+.1fmm"), ("overflow_sum", "ovf", "%+d"),
             ("overflow_max", "ovfmax", "%+d"), ("inversions", "inv", "%+d"), ("pin_inv", "pin_inv", "%+d"),
             ("ang_deg", "ang", "%+.0f"), ("stubs", "stubs", "%+d"), ("unrouted", "unrouted", "%+d"),
             ("intent", "intent", "%+.1f")]
    out = []
    for k, n, f in names:
        v = d.get(k, 0)
        if abs(v) > (0.05 if k in ("wirelength", "intent") else 0.5 if k == "ang_deg" else 1e-9):
            out.append("%s %s" % (n, f % v))
    return " ".join(out) or "no change"


class State:
    def __init__(self, name, board_path, base_board, assume=(), cell=21.0):
        self.name, self.board_path, self.assume, self.cell = name, board_path, list(assume), cell
        import copy
        self.base = base_board
        board = copy.deepcopy(base_board)
        self.world = A.World(board)
        rn0 = ratsnest.Ratsnest()
        self.skel = topo.Topo(base_board, rn0, cell, light=True)
        self.world.cuts, self.world.regions = set(self.skel.cuts), set(self.skel.regions)
        self.world.region_boxes = {rid: r.bbox for rid, r in self.skel.regions.items()}
        self.eng = E.Engine(self.world, self.skel, self.assume)
        self.base_m = self.eng.metrics()
        self.log = []             # [{"acts": [...], "invs": [...] (not persisted), "m": {...}, "warn": [...]}]
        self.snapshots = {}       # name -> {"log": [...], "m": ..., "t": ...}
        self.branches = {}        # name -> log (inactive branches; the active one is self.log)
        self.branch_name = "main"
        self.last = []            # labels of the rows of the last eval/suggest table: `act S @3` applies row 3
        self.path = session_path(name)

    # ------------------------------------------------------------------ construction / persistence
    @classmethod
    def new(cls, name, board_path, assume=(), cell=21.0, overwrite=False):
        from . import cli
        path = session_path(name)
        if os.path.exists(path) and not overwrite:
            raise A.ActionError("session '%s' exists (use another name or --force)" % name)
        board = cli.load_board(board_path)
        assume = list(assume)
        if not board.plane_nets() and not assume:
            counts = {}
            for pd in board.pads():
                if pd.net:
                    counts[pd.net] = counts.get(pd.net, 0) + 1
            assume = sorted(n for n, k in counts.items() if k >= 25)
        st = cls(name, os.path.abspath(board_path), board, assume, cell)
        st.save()
        return st

    @classmethod
    def load(cls, name):
        path = session_path(name)
        if not os.path.exists(path):
            raise A.ActionError("no session '%s' (looked for %s). Create one: badpcb session new %s <board>" % (name, path, name))
        with open(path) as fh:
            d = json.load(fh)
        from . import cli
        bp = config.current().rel(d["board"])          # sessions store the board path relative to the project when inside it
        board = cli.load_board(bp)
        st = cls(d["name"] if os.sep not in name else os.path.basename(name)[:-5], bp, board, d["assume"], d["cell"])
        st.path = path
        st.snapshots = d.get("snapshots", {})
        st.branches = d.get("branches", {})
        st.branch_name = d.get("branch", "main")
        st.last = d.get("last", [])
        st._replay(d["log"])
        return st

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        d = {"version": 1, "name": self.name, "board": config.current().short(self.board_path), "assume": self.assume, "cell": self.cell,
             "branch": self.branch_name, "log": [self._ser(e) for e in self.log],
             "snapshots": self.snapshots, "branches": {k: v for k, v in self.branches.items() if k != self.branch_name},
             "hints": self.world.hints, "last": self.last}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def _ser(e):
        return {"acts": e["acts"], "m": e["m"], "warn": e.get("warn", []), "t": e.get("t")}

    def _replay(self, entries):
        """Apply logged entries without legality checks (they were legal when logged); engine refreshed once."""
        nets = set()
        for e in entries:
            invs = []
            for d in e["acts"]:
                inv, n = A.apply(self.world, A.Action.from_dict(d), check_first=False)
                invs.append(inv.to_dict())
                nets |= n
            self.log.append({"acts": e["acts"], "invs": invs[::-1], "m": e["m"], "warn": e.get("warn", []), "t": e.get("t")})
        if nets:
            self.eng.refresh(nets)

    # ------------------------------------------------------------------ metrics
    def metrics(self):
        return self.eng.metrics()

    def step(self):
        return len(self.log)

    # ------------------------------------------------------------------ act / undo
    def act(self, spec, force=False):
        """Apply one or more actions atomically. spec: str (compact/JSON), Action, or list of Action/str.
        Returns {"before","after","delta","warnings","applied","step"}; raises ActionError (nothing applied)."""
        if isinstance(spec, str) and re.match(r"^\s*[@#]\d+\s*$", spec):
            i = int(spec.strip()[1:])
            if not 1 <= i <= len(self.last):
                raise A.ActionError("no row %d in the last eval/suggest table (%d rows)" % (i, len(self.last)))
            spec = self.last[i - 1]
        acts = parse_specs(spec)
        if force:
            for a in acts:
                a.force = True
        m0 = self.metrics()
        done, nets, warns = [], set(), []
        try:
            for a in acts:
                errs, w = A.check(self.world, a)
                if errs:
                    raise A.ActionError(["%s: %s" % (a.compact(), e) for e in errs])
                warns += w
                inv, n = A.apply(self.world, a, check_first=False)
                done.append((a, inv))
                nets |= n
        except A.ActionError:
            for a, inv in reversed(done):
                A.apply(self.world, inv, check_first=False)
            raise
        if nets:
            self.eng.refresh(nets)
        m1 = self.metrics()
        self.log.append({"acts": [a.to_dict() for a, _ in done], "invs": [inv.to_dict() for _, inv in reversed(done)],
                         "m": m1, "warn": warns, "t": time.time()})
        self.last = []            # rows of the old table no longer describe this state
        self.save()
        return {"before": m0, "after": m1, "delta": _delta(m0, m1), "warnings": warns,
                "applied": [a.compact() for a, _ in done], "step": self.step()}

    def _undo_entry(self):
        e = self.log.pop()
        nets = set()
        for d in e["invs"]:
            _, n = A.apply(self.world, A.Action.from_dict(d), check_first=False)
            nets |= n
        return nets, e

    def _keep_prev(self):
        """Safety net: the tip before an undo/revert stays reachable as snapshot '_prev' (`badpcb revert S _prev` = redo)."""
        if self.log:
            self.snapshots["_prev"] = {"log": [self._ser(e) for e in self.log], "m": self.metrics(), "t": time.time(), "step": self.step()}

    def undo(self, n=1):
        if n > len(self.log):
            raise A.ActionError("only %d step(s) to undo" % len(self.log))
        self._keep_prev()
        m0 = self.metrics()
        nets, undone = set(), []
        for _ in range(n):
            ns, e = self._undo_entry()
            nets |= ns
            undone += [A.Action.from_dict(d).compact() for d in e["acts"]]
        if nets:
            self.eng.refresh(nets)
        self.save()
        m1 = self.metrics()
        return {"before": m0, "after": m1, "delta": _delta(m0, m1), "undone": undone, "step": self.step()}

    # ------------------------------------------------------------------ snapshots / branches / revert
    def snapshot(self, name):
        self.snapshots[name] = {"log": [self._ser(e) for e in self.log], "m": self.metrics(), "t": time.time(), "step": self.step()}
        self.save()
        return self.snapshots[name]

    def _goto(self, entries):
        """Make the active log equal to `entries` (list of serialised entries) via inverses + replay."""
        m0 = self.metrics()
        k = 0
        while k < len(entries) and k < len(self.log) and entries[k]["acts"] == self.log[k]["acts"]:
            k += 1
        nets = set()
        while len(self.log) > k:
            ns, _ = self._undo_entry()
            nets |= ns
        if nets:
            self.eng.refresh(nets)
        self._replay(entries[k:])
        self.save()
        m1 = self.metrics()
        return {"before": m0, "after": m1, "delta": _delta(m0, m1), "step": self.step()}

    def revert(self, target):
        """target: int step count, or a snapshot / branch name (branch: its tip)."""
        if isinstance(target, str) and target.lstrip("-").isdigit():
            target = int(target)
        if target != "_prev":
            self._keep_prev()
        if isinstance(target, int):
            if target < 0 or target > len(self.log):
                raise A.ActionError("step %d out of range 0..%d" % (target, len(self.log)))
            return self._goto([self._ser(e) for e in self.log[:target]])
        if target in self.snapshots:
            return self._goto(self.snapshots[target]["log"])
        if target == "base":
            return self._goto([])
        raise A.ActionError("unknown snapshot '%s' (have: %s)" % (target, ", ".join(self.snapshots) or "none"))

    def branch(self, name):
        """Fork: the current log continues under `name`; the old branch is kept and can be switched back to."""
        if name == self.branch_name or name in self.branches:
            raise A.ActionError("branch '%s' exists" % name)
        self.branches[self.branch_name] = [self._ser(e) for e in self.log]
        self.branch_name = name
        self.save()
        return name

    def switch(self, name):
        if name == self.branch_name:
            return self.metrics()
        if name not in self.branches:
            raise A.ActionError("no branch '%s' (have: main, %s)" % (name, ", ".join(self.branches)))
        self.branches[self.branch_name] = [self._ser(e) for e in self.log]
        entries = self.branches.pop(name)
        self.branch_name = name
        return self._goto(entries)

    def _resolve(self, ref):
        """-> (entries, metrics)."""
        if ref in ("", "current", "head", None):
            return [self._ser(e) for e in self.log], self.metrics()
        if ref == "base":
            return [], self.base_m
        if str(ref).isdigit():
            n = int(ref)
            if n > len(self.log):
                raise A.ActionError("step %d out of range" % n)
            return [self._ser(e) for e in self.log[:n]], (self.log[n - 1]["m"] if n else self.base_m)
        if ref in self.snapshots:
            s = self.snapshots[ref]
            return s["log"], s["m"]
        if ref in self.branches:
            L = self.branches[ref]
            return L, (L[-1]["m"] if L else self.base_m)
        raise A.ActionError("unknown ref '%s' (base, current, N, snapshot or branch name)" % ref)

    def diff(self, a="base", b="current"):
        ea, ma = self._resolve(a)
        eb, mb = self._resolve(b)
        k = 0
        while k < min(len(ea), len(eb)) and ea[k]["acts"] == eb[k]["acts"]:
            k += 1
        fmt = lambda es: [A.Action.from_dict(d).compact() for e in es for d in e["acts"]]
        return {"common_steps": k, "only_a": fmt(ea[k:]), "only_b": fmt(eb[k:]), "a": ma, "b": mb, "delta": _delta(ma, mb)}

    # ------------------------------------------------------------------ misc views
    def history(self):
        out = [(0, "base", self.base_m)]
        for i, e in enumerate(self.log, 1):
            out.append((i, "; ".join(A.Action.from_dict(d).compact() for d in e["acts"]), e["m"]))
        return out

    def trajectory(self):
        """List of (step, label, metrics) including the base."""
        return self.history()


def parse_specs(spec):
    if isinstance(spec, A.Action):
        return [spec]
    if isinstance(spec, str):
        return A.parse(spec)
    out = []
    for s in spec:
        out += parse_specs(s)
    return out
