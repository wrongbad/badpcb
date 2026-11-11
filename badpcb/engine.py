"""Incremental metrics engine: ratsnest + crossing structures that can be updated per net and undone.

    eng = Engine(world, skeleton)          # skeleton = topo.Topo(base_board, rn, light=True): frozen regions/cuts/capacities
    eng.metrics()                          # dict (see METRIC_KEYS)
    tok = eng.refresh({"ADC3", "GND"})     # after the board changed: re-ratsnest only these nets, update crossings
    eng.restore(tok)                       # undo that refresh exactly

The skeleton (grid, cut capacities, escape rings) is frozen when a session is created, so metrics of any
two states are comparable and `Engine(world_after, skeleton).metrics()` equals the incrementally updated one.
Definitions match topo.Topo.metrics() on the same board (tests check this).
"""
import math
from collections import defaultdict

from . import geom, ratsnest, swaps

METRIC_KEYS = ["X", "Xcu", "wirelength", "overflow_sum", "overflow_max", "inversions", "pin_inv", "ang_deg", "stubs", "unrouted", "intent"]
LOWER_IS_BETTER = set(METRIC_KEYS)
# weights of the scalar "score" used to rank candidates (lower = better). The agent should read the Δ columns, not the score.
WEIGHTS = {"X": 1.0, "Xcu": 0.3, "wirelength": 0.02, "overflow_sum": 2.0, "overflow_max": 1.0, "inversions": 2.0,
           "pin_inv": 1.0, "ang_deg": 0.05, "stubs": 3.0, "unrouted": 0.0, "intent": 1.0}
CELL = 8.0


def score(m):
    return sum(WEIGHTS[k] * m.get(k, 0) for k in WEIGHTS)


def _cells(bb, cs=CELL):
    for gx in range(int(bb[0] // cs), int(bb[2] // cs) + 1):
        for gy in range(int(bb[1] // cs), int(bb[3] // cs) + 1):
            yield (gx, gy)


class _Rec:
    __slots__ = ("cid", "conn", "bb", "cuts", "x", "cu", "cul")


class RNView:
    """What swaps.arrivals / pin_vs_arrival need from a Ratsnest."""

    def __init__(self, conns, islands):
        self.conns, self.islands = conns, islands


class Engine:
    def __init__(self, world, skeleton, assume=()):
        self.world, self.board, self.T = world, world.board, skeleton
        self.assume = tuple(assume)
        self.planes = set(self.board.plane_nets()) | set(self.assume)
        self._next = 0
        self.recs = {}                       # cid -> _Rec
        self.nets = {}                       # net -> {"cids": [...], "islands": [...], "stubs": n, "plane": (s,u)|None}
        self.cgrid = defaultdict(set)        # cell -> {cid}   (conns)
        self.cut_cross = {cid: {} for cid in skeleton.cuts}
        self.X = 0
        self.Xcu = 0
        self.Xcu_locked = 0                  # part of Xcu that is locked copper (not rippable by KRT)
        self.dirty = set()
        self.cutlist = [(cid, c.p0, c.p1, (min(c.p0[0], c.p1[0]), min(c.p0[1], c.p1[1]), max(c.p0[0], c.p1[0]), max(c.p0[1], c.p1[1])))
                        for cid, c in skeleton.cuts.items()]
        self.pairs = skeleton._adjacent_pairs()
        self.pair_inv = {}
        self.cut_pairs = defaultdict(list)
        for p in self.pairs:
            self.cut_pairs[p[0]].append(p)
            self.cut_pairs[p[1]].append(p)
        self.dirty = set(skeleton.cuts)
        self._ntracks = -1
        self._index_tracks()
        rn = ratsnest.build(self.board, self.assume)
        by = defaultdict(list)
        for c in rn.conns:
            by[c.net].append(c)
        for net, isl in rn.islands.items():
            self._install(net, isl, by.get(net, []), rn.plane_nets.get(net))
        for net in set(self.nets):
            pass

    # ------------------------------------------------------------------ static indexes
    def _index_tracks(self):
        sig = set(self.board.signal_layers())
        self.tgrid = defaultdict(list)
        self._ntracks = (len(self.board.tracks), getattr(self.board, "lock_ver", 0))
        for t in self.board.tracks:
            if t.net not in self.planes and t.layer in sig:
                bb = (min(t.a[0], t.b[0]), min(t.a[1], t.b[1]), max(t.a[0], t.b[0]), max(t.a[1], t.b[1]))
                for c in _cells(bb):
                    self.tgrid[c].append(t)

    def _sync_tracks(self):
        if (len(self.board.tracks), getattr(self.board, "lock_ver", 0)) != self._ntracks:
            self._index_tracks()
            for rec in self.recs.values():
                old, oldl = rec.cu, rec.cul
                rec.cu, rec.cul = self._cu_count(rec.conn, rec.bb)
                self.Xcu += rec.cu - old
                self.Xcu_locked += rec.cul - oldl

    # ------------------------------------------------------------------ record management
    def _cu_count(self, conn, bb):
        seen, n, nl = set(), 0, 0
        for cell in _cells(bb):
            for t in self.tgrid.get(cell, ()):
                if id(t) in seen:
                    continue
                seen.add(id(t))
                if t.net != conn.net and geom.seg_intersect(conn.pa, conn.pb, t.a, t.b):
                    n += 1
                    nl += bool(t.locked)
        return n, nl

    def _add_conn(self, conn, cid=None):
        if cid is None:
            cid = self._next
            self._next += 1
        r = _Rec()
        r.cid, r.conn = cid, conn
        pa, pb = conn.pa, conn.pb
        r.bb = bb = (min(pa[0], pb[0]), min(pa[1], pb[1]), max(pa[0], pb[0]), max(pa[1], pb[1]))
        r.x = set()
        cand = set()
        cells = list(_cells(bb))
        for c in cells:
            cand |= self.cgrid[c]
        for o in cand:
            ro = self.recs[o]
            if ro.conn.net == conn.net:
                continue
            ob = ro.bb
            if ob[0] > bb[2] or ob[2] < bb[0] or ob[1] > bb[3] or ob[3] < bb[1]:
                continue
            if geom.seg_intersect(pa, pb, ro.conn.pa, ro.conn.pb):
                r.x.add(o)
                ro.x.add(cid)
        self.X += len(r.x)
        r.cuts = {}
        for ccid, p0, p1, cb in self.cutlist:
            if cb[0] > bb[2] or cb[2] < bb[0] or cb[1] > bb[3] or cb[3] < bb[1]:
                continue
            p = geom.seg_intersect(pa, pb, p0, p1)
            if p:
                r.cuts[ccid] = p
                self.cut_cross[ccid][cid] = p
                self.dirty.add(ccid)
        r.cu, r.cul = self._cu_count(conn, bb)
        self.Xcu += r.cu
        self.Xcu_locked += r.cul
        self.recs[cid] = r
        for c in cells:
            self.cgrid[c].add(cid)
        return cid

    def _remove_conn(self, cid):
        r = self.recs.pop(cid)
        for o in r.x:
            self.recs[o].x.discard(cid)
        self.X -= len(r.x)
        self.Xcu -= r.cu
        self.Xcu_locked -= r.cul
        for ccid in r.cuts:
            del self.cut_cross[ccid][cid]
            self.dirty.add(ccid)
        for c in _cells(r.bb):
            self.cgrid[c].discard(cid)

    def _install(self, net, islands, conns, plane, cids=None):
        ids = []
        for i, c in enumerate(conns):
            ids.append(self._add_conn(c, cids[i] if cids else None))
        self.nets[net] = {"cids": ids, "islands": islands, "plane": plane,
                          "stubs": sum(1 for I in islands if I.pads and I.served is False)}

    def _uninstall(self, net):
        st = self.nets.pop(net, None)
        if st is None:
            return None
        saved = {"islands": st["islands"], "plane": st["plane"], "conns": [self.recs[c].conn for c in st["cids"]], "cids": list(st["cids"])}
        for c in st["cids"]:
            self._remove_conn(c)
        return saved

    # ------------------------------------------------------------------ update / undo
    def refresh(self, nets):
        """The board changed for `nets`: rebuild their ratsnest and update all derived data. Returns an undo token."""
        nets = sorted(set(nets))
        tok = {n: self._uninstall(n) for n in nets}
        rn = ratsnest.build(self.board, self.assume, nets)
        by = defaultdict(list)
        for c in rn.conns:
            by[c.net].append(c)
        for n in nets:
            if n in rn.islands:
                self._install(n, rn.islands[n], by.get(n, []), rn.plane_nets.get(n))
        self._sync_tracks()
        return tok

    def restore(self, tok):
        for n in tok:
            self._uninstall(n)
        for n, s in tok.items():
            if s is not None:
                self._install(n, s["islands"], s["conns"], s["plane"], s["cids"])
        self._sync_tracks()

    # ------------------------------------------------------------------ views
    def conns(self):
        return [self.recs[c].conn for n in sorted(self.nets) for c in self.nets[n]["cids"]]

    def rn_view(self):
        return RNView(self.conns(), {n: st["islands"] for n, st in self.nets.items()})

    def stub_islands(self):
        out = []
        for n in sorted(self.nets):
            out += [I for I in self.nets[n]["islands"] if I.pads and I.served is False]
        return out

    def crossings_by_net(self):
        """{net: X count involving that net} (each crossing counted for both nets)."""
        out = defaultdict(int)
        for r in self.recs.values():
            out[r.conn.net] += len(r.x)
        return dict(out)

    def crossing_pairs(self):
        M = defaultdict(int)
        for cid, r in self.recs.items():
            for o in r.x:
                if cid < o:
                    M[tuple(sorted((r.conn.net, self.recs[o].conn.net)))] += 1
        return dict(M)

    def region_of_crossings(self):
        out = defaultdict(int)
        for cid, r in self.recs.items():
            for o in r.x:
                if cid < o:
                    p = geom.seg_intersect(r.conn.pa, r.conn.pb, self.recs[o].conn.pa, self.recs[o].conn.pb)
                    if p:
                        out[self.T.region_at(p).id] += 1
        return dict(out)

    # ------------------------------------------------------------------ inversions (mirrors Topo.inversion_pairs)
    def _inv_count(self, ca, cb):
        T = self.T
        A, B = self.cut_cross[ca], self.cut_cross[cb]
        common = sorted(set(A) & set(B))
        if len(common) < 2:
            return 0
        cu, cv = T.cuts[ca], T.cuts[cb]
        if cu.orient == cv.orient:
            sa = {i: T._coord(cu, A[i]) for i in common}
            sb = {i: T._coord(cv, B[i]) for i in common}
            flip = lambda i, j: (sa[i] - sa[j]) * (sb[i] - sb[j]) < 0
        else:
            shared = {cu.a, cu.b} & {cv.a, cv.b}
            if not shared:
                return 0
            R = sorted(shared)[0]
            pa = {i: T._perim(R, A[i]) for i in common}
            pb = {i: T._perim(R, B[i]) for i in common}

            def flip(i, j):
                lo, hi = sorted((pa[i], pb[i]))
                return (lo < pa[j] < hi) != (lo < pb[j] < hi)
        n = 0
        net = lambda i: self.recs[i].conn.net
        for x in range(len(common)):
            for y in range(x + 1, len(common)):
                i, j = common[x], common[y]
                if net(i) != net(j) and flip(i, j):
                    n += 1
        return n

    def _inversions(self):
        if self.dirty:
            todo = set()
            for c in self.dirty:
                todo.update(self.cut_pairs.get(c, ()))
            for p in todo:
                self.pair_inv[p] = self._inv_count(*p)
            self.dirty = set()
        return sum(self.pair_inv.values())

    # ------------------------------------------------------------------ metrics
    def pin_metrics(self):
        """(U1 pin-order inversions, angular mismatch in degrees) summed over the pin groups."""
        w = self.world
        if w.cfg["mcu"] not in self.board.parts:
            return 0, 0.0
        rn = self.rn_view()
        inv, ang = 0, 0.0
        gs = w.groups()
        part = self.board.parts[w.cfg["mcu"]]
        pos = {}
        for pd in part.pads:
            pos.setdefault(pd.num, pd.pos)
        for gid, G in gs.items():
            if G.kind not in ("pin_permutation", "pin_assignment"):
                continue
            v = swaps.pin_vs_arrival(self.board, rn, G, w.cfg)
            inv += v["inversions"]
            arr = swaps.arrivals(self.board, rn, w.cfg["mcu"], G.nets)
            for n, (pt, _) in arr.items():
                if n in G.current:
                    d = abs(swaps._ang(part.pos, pt) - swaps._ang(part.pos, pos[G.current[n]])) % (2 * math.pi)
                    ang += math.degrees(min(d, 2 * math.pi - d))
        return inv, ang

    def metrics(self):
        dem = [(len(self.cut_cross[c]) - self.T.cuts[c].cap_total) for c in self.T.cuts]
        ov = [max(0, d) for d in dem]
        pin_inv, ang = self.pin_metrics()
        from . import intent                  # soft design intent: weighted penalties, a pure function of the part poses (0 when none declared)
        ipen, inum = intent.penalty(self.world)
        return {"intent": round(ipen, 3), "intent_n": inum, "X": self.X, "Xcu": self.Xcu, "Xcu_locked": self.Xcu_locked,
                "wirelength": round(sum(r.conn.length for r in self.recs.values()), 3),
                "overflow_sum": sum(ov), "overflow_max": max(ov) if ov else 0,
                "inversions": self._inversions(), "pin_inv": pin_inv, "ang_deg": round(ang, 1),
                "stubs": sum(st["stubs"] for st in self.nets.values()), "unrouted": len(self.recs)}
