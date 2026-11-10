"""Topological views of the ratsnest: crossings, conflict clusters, regions, cuts, inversions.

    t = build(board, ratsnest)           # Topo
    t.matrix()      -> (nets, {(n1, n2): count})    ratsnest-vs-ratsnest crossings
    t.clusters()    -> [{'id': 'K1', 'nets': [...], 'internal': n, 'external': n}]
    t.regions       -> {'R1': Region}      cells of a coarse grid whose lines avoid big parts
    t.cuts          -> {'C1': Cut}         shared edges between adjacent regions, with per-layer capacity
    t.cut_nets(cid) -> [(net, conn_idx, coord)] ordered along the cut (V cuts: top->bottom, H: left->right)
    t.inversions()  -> worst pairs of cuts whose common nets change order between them

Handles are deterministic for the same geometry: regions are numbered row-major (R1 = top-left),
cuts are numbered vertical-first (cuts between left/right neighbours, row-major), then horizontal.
Capacity of a cut = number of default-width tracks (width + clearance pitch) that fit between the
obstacles (pads, vias, holes, tracks, keepouts) on the cut line, summed per signal layer
(plane layers excluded). Demand = ratsnest connections whose straight line crosses the cut.
Known limits: rectangular board only; grid cells rather than obstacle-aware merging; connections
are straight lines (layer-agnostic), demand is not split per layer.
"""
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List

from . import geom


@dataclass
class Region:
    id: str
    col: int
    row: int
    bbox: tuple
    parts: List[str] = field(default_factory=list)   # major parts (big, with pads) inside
    n_pads: int = 0
    n_keys: int = 0                                  # switch footprints (SW*) inside


@dataclass
class Cut:
    id: str
    orient: str          # "V" vertical line (between left/right regions) or "H"
    a: str               # region left/above
    b: str               # region right/below
    p0: tuple
    p1: tuple
    label: str = ""      # e.g. "U1 ring N" for the escape-ring cuts around big parts
    cap: Dict[str, int] = field(default_factory=dict)       # per signal layer
    free: Dict[str, float] = field(default_factory=dict)    # free centre-line length per layer (mm)

    @property
    def length(self):
        return geom.dist(self.p0, self.p1)

    @property
    def cap_total(self):
        return sum(self.cap.values())

    @property
    def mid(self):
        return ((self.p0[0] + self.p1[0]) / 2, (self.p0[1] + self.p1[1]) / 2)


# --------------------------------------------------------------------------- grid / regions
def _snap_lines(lo, hi, n, majors, axis):
    """n-1 interior grid lines at equal spacing, nudged out of major-part boxes along `axis`."""
    lines = [lo + (hi - lo) * k / n for k in range(1, n)]
    out = []
    prev = lo
    for k, x in enumerate(lines):
        for _ in range(4):
            hit = [m for m in majors if m[axis] < x < m[axis + 2]]
            if not hit:
                break
            cands = [m[axis] - 0.5 for m in hit] + [m[axis + 2] + 0.5 for m in hit]
            x = min(cands, key=lambda c: abs(c - x))
        x = max(x, prev + 3.0)
        out.append(x)
        prev = x
    return [lo] + out + [hi]


def _major_boxes(board, min_area=25.0):
    return [p.bbox for p in board.parts.values() if p.pads and p.area >= min_area]


class Topo:
    def __init__(self, board, rn, cell=21.0, ring_margin=2.0, ring_min_pads=20, light=False):
        """light=True builds only the skeleton (regions, cuts, rings, capacities), no crossings (see rk.engine)."""
        self.board, self.rn = board, rn
        self.conns = rn.conns
        x0, y0, x1, y1 = board.bbox
        nx, ny = max(1, round((x1 - x0) / cell)), max(1, round((y1 - y0) / cell))
        majors = _major_boxes(board)
        self.xs = _snap_lines(x0, x1, nx, majors, 0)
        self.ys = _snap_lines(y0, y1, ny, majors, 1)
        self.nx, self.ny = nx, ny
        self.regions: Dict[str, Region] = {}
        self.cuts: Dict[str, Cut] = {}
        self._make_regions()
        self._make_cuts()
        self.rings = {}          # ref -> (x0, y0, x1, y1) escape ring around big multi-pin parts
        self._make_rings(ring_margin, ring_min_pads)
        if light:
            self.crossings, self.cu_crossings, self.cut_cross = [], [], {cid: {} for cid in self.cuts}
            self.cu_cross_tracks = []
        else:
            self._cross()
        for c in self.cuts.values():
            self._capacity(c)

    # ---- regions
    def rid(self, col, row):
        return "R%d" % (row * self.nx + col + 1)

    def _make_regions(self):
        for r in range(self.ny):
            for c in range(self.nx):
                R = Region(self.rid(c, r), c, r, (self.xs[c], self.ys[r], self.xs[c + 1], self.ys[r + 1]))
                self.regions[R.id] = R
        for part in self.board.parts.values():
            R = self.region_at(part.pos)
            if part.ref.startswith("SW"):
                R.n_keys += 1
            elif part.pads and part.area >= 25.0:
                R.parts.append(part.ref)
            for pd in part.pads:
                self.region_at(pd.pos).n_pads += 1

    def region_at(self, pt):
        c = min(self.nx - 1, max(0, sum(1 for x in self.xs[1:-1] if pt[0] >= x)))
        r = min(self.ny - 1, max(0, sum(1 for y in self.ys[1:-1] if pt[1] >= y)))
        return self.regions[self.rid(c, r)]

    # ---- cuts
    def _make_cuts(self):
        n = 0
        for r in range(self.ny):
            for c in range(self.nx - 1):
                n += 1
                self.cuts["C%d" % n] = Cut("C%d" % n, "V", self.rid(c, r), self.rid(c + 1, r),
                                           (self.xs[c + 1], self.ys[r]), (self.xs[c + 1], self.ys[r + 1]))
        for r in range(self.ny - 1):
            for c in range(self.nx):
                n += 1
                self.cuts["C%d" % n] = Cut("C%d" % n, "H", self.rid(c, r), self.rid(c, r + 1),
                                           (self.xs[c], self.ys[r + 1]), (self.xs[c + 1], self.ys[r + 1]))

    def _make_rings(self, margin, min_pads):
        """Escape rings: 4 extra cuts (N, E, S, W) around each part with >= min_pads pads, so that
        demand/capacity/order of the nets leaving e.g. U1 are visible even though U1 sits inside one region."""
        n = len(self.cuts)
        for ref, part in sorted(self.board.parts.items()):
            if len(part.pads) < min_pads:
                continue
            x0, y0, x1, y1 = part.bbox
            x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin
            self.rings[ref] = (x0, y0, x1, y1)
            inside = self.region_at(part.pos).id
            for side, orient, p0, p1, out in (("N", "H", (x0, y0), (x1, y0), (part.pos[0], y0 - 1)),
                                              ("E", "V", (x1, y0), (x1, y1), (x1 + 1, part.pos[1])),
                                              ("S", "H", (x0, y1), (x1, y1), (part.pos[0], y1 + 1)),
                                              ("W", "V", (x0, y0), (x0, y1), (x0 - 1, part.pos[1]))):
                n += 1
                outside = self.region_at(out).id
                a, b = (outside, ref) if side in "NW" else (ref, outside)
                self.cuts["C%d" % n] = Cut("C%d" % n, orient, a, b, p0, p1, label="%s ring %s" % (ref, side))

    def _coord(self, cut, pt):
        return pt[1] if cut.orient == "V" else pt[0]

    def _cross(self):
        """Ratsnest crossings (conn i, conn j, point) and per-cut crossing points."""
        cs = self.conns
        self.crossings = []
        for i in range(len(cs)):
            ai, bi = cs[i].pa, cs[i].pb
            for j in range(i + 1, len(cs)):
                if cs[i].net == cs[j].net:
                    continue
                p = geom.seg_intersect(ai, bi, cs[j].pa, cs[j].pb)
                if p:
                    self.crossings.append((i, j, p))
        self.cut_cross = {cid: {} for cid in self.cuts}
        for cid, cut in self.cuts.items():
            for i, c in enumerate(cs):
                p = geom.seg_intersect(c.pa, c.pb, cut.p0, cut.p1)
                if p:
                    self.cut_cross[cid][i] = p
        # crossings of ratsnest lines with existing copper of other nets (signal layers)
        sig = set(self.board.signal_layers())
        planes = set(self.board.plane_nets())      # plane-net stubs are tiny; ignore them
        self.cu_crossings = []                     # (conn idx, crossed net, layer)
        self.cu_cross_tracks = []                  # the crossed Track, parallel to cu_crossings (lock class / rippable?)
        for i, c in enumerate(cs):
            for t in self.board.tracks:
                if t.net != c.net and t.net not in planes and t.layer in sig and geom.seg_intersect(c.pa, c.pb, t.a, t.b):
                    self.cu_crossings.append((i, t.net, t.layer))
                    self.cu_cross_tracks.append(t)

    # ---- capacity
    def _obstacles(self, layer):
        """Obstacle shapes on `layer` as ('R', centre, half, rot, grow) / ('C', centre, radius)."""
        b, rl = self.board, self.board.rules
        g = rl.clearance + rl.track_width / 2
        sh = []
        for pd in b.pads():
            if layer in pd.layers:
                if pd.shape == "circle":
                    sh.append(("C", pd.pos, max(pd.size) / 2 + g))
                else:
                    sh.append(("R", pd.pos, (pd.size[0] / 2, pd.size[1] / 2), pd.rot, g))
        for v in b.vias:
            sh.append(("C", v.pos, v.dia / 2 + g))
        for h in b.holes:
            sh.append(("C", h.pos, h.drill / 2 + rl.clearance + rl.track_width / 2))
        for t in b.tracks:
            if t.layer == layer:
                r = t.w / 2 + g
                L = geom.dist(t.a, t.b)
                ang = math.degrees(math.atan2(t.b[1] - t.a[1], t.b[0] - t.a[0]))
                sh.append(("R", ((t.a[0] + t.b[0]) / 2, (t.a[1] + t.b[1]) / 2), (L / 2, t.w / 2), ang, g))
                sh += [("C", t.a, r), ("C", t.b, r)]
        for k in b.keepouts:
            if k.keepout and k.keepout.get("no_tracks") and (layer in k.layers or not k.layers):
                for poly in k.polys:
                    bb = geom.bbox_of(poly["outer"])
                    sh.append(("R", ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2), ((bb[2] - bb[0]) / 2, (bb[3] - bb[1]) / 2), 0.0, g))
        out = []
        for s in sh:   # attach a bbox for quick rejection
            if s[0] == "C":
                r = s[2]
                bb = (s[1][0] - r, s[1][1] - r, s[1][0] + r, s[1][1] + r)
            else:
                r = math.hypot(s[2][0] + s[4], s[2][1] + s[4])
                bb = (s[1][0] - r, s[1][1] - r, s[1][0] + r, s[1][1] + r)
            out.append((bb, s))
        return out

    def _capacity(self, cut):
        rl = self.board.rules
        if not hasattr(self, "_obs"):
            self._obs = {l: self._obstacles(l) for l in self.board.signal_layers()}
        L = cut.length
        bx = self.board.bbox
        e0 = e1 = 0.0
        shrink = rl.edge_clearance + rl.track_width / 2
        for end, name in ((cut.p0, "e0"), (cut.p1, "e1")):
            on_edge = end[0] <= bx[0] + 1e-6 or end[0] >= bx[2] - 1e-6 or end[1] <= bx[1] + 1e-6 or end[1] >= bx[3] - 1e-6
            if on_edge:
                if name == "e0":
                    e0 = shrink
                else:
                    e1 = shrink
        cb = (min(cut.p0[0], cut.p1[0]), min(cut.p0[1], cut.p1[1]), max(cut.p0[0], cut.p1[0]), max(cut.p0[1], cut.p1[1]))
        for layer, obs in self._obs.items():
            blocked = [(0.0, e0), (L - e1, L)]
            for bb, s in obs:
                if not geom.bbox_overlap(bb, cb):
                    continue
                if s[0] == "C":
                    iv = geom.seg_circle_interval(cut.p0, cut.p1, s[1], s[2])
                else:
                    iv = geom.clip_seg_rect(cut.p0, cut.p1, s[1], s[2], s[3], s[4])
                if iv:
                    blocked.append((iv[0] * L, iv[1] * L))
            blocked.sort()
            free, cur, total, n = [], 0.0, 0.0, 0
            for a, b in blocked:
                if a > cur:
                    free.append((cur, a))
                cur = max(cur, b)
            for a, b in free:
                total += b - a
                n += int((b - a) / rl.pitch + 1e-9) + 1
            cut.cap[layer] = n
            cut.free[layer] = round(total, 2)

    # ---- queries
    def cut_nets(self, cid):
        cut = self.cuts[cid]
        items = [(self._coord(cut, p), i) for i, p in self.cut_cross[cid].items()]
        return [(self.conns[i].net, i, round(t, 2)) for t, i in sorted(items)]

    def demand(self, cid):
        return len(self.cut_cross[cid])

    def overflow(self, cid):
        return max(0, self.demand(cid) - self.cuts[cid].cap_total)

    def matrix(self):
        M = defaultdict(int)
        for i, j, _ in self.crossings:
            a, b = sorted((self.conns[i].net, self.conns[j].net))
            M[(a, b)] += 1
        nets = sorted({n for k in M for n in k})
        return nets, dict(M)

    def clusters(self, max_size=8, min_w=2):
        """Greedy agglomeration of the crossing graph: repeatedly merge the two clusters with
        the largest mutual crossing count (>= min_w) while the union stays <= max_size nets."""
        nets, M = self.matrix()
        cl = {n: {n} for n in nets}
        def w(A, B):
            return sum(M.get(tuple(sorted((a, b))), 0) for a in A for b in B)
        while True:
            best, bp = 0, None
            keys = sorted(cl)
            for i, ka in enumerate(keys):
                for kb in keys[i + 1:]:
                    if len(cl[ka]) + len(cl[kb]) > max_size:
                        continue
                    x = w(cl[ka], cl[kb])
                    if x > best:
                        best, bp = x, (ka, kb)
            if best < min_w:
                break
            ka, kb = bp
            cl[ka] |= cl.pop(kb)
        out = []
        for S in cl.values():
            if len(S) < 2:
                continue
            inter = sum(M.get(tuple(sorted((a, b))), 0) for a in S for b in S if a < b)
            ext = sum(v for (a, b), v in M.items() if (a in S) != (b in S))
            out.append({"nets": sorted(S), "internal": inter, "external": ext})
        out.sort(key=lambda c: (-c["internal"], c["nets"]))
        for k, c in enumerate(out):
            c["id"] = "K%d" % (k + 1)
        return out

    def _perim(self, rid, p):
        x0, y0, x1, y1 = self.regions[rid].bbox
        w, h = x1 - x0, y1 - y0
        d = {"t": abs(p[1] - y0), "r": abs(p[0] - x1), "b": abs(p[1] - y1), "l": abs(p[0] - x0)}
        e = min(d, key=d.get)
        return {"t": p[0] - x0, "r": w + p[1] - y0, "b": w + h + x1 - p[0], "l": 2 * w + h + y1 - p[1]}[e]

    def inversion_pairs(self, ca, cb):
        """Conn pairs crossing both cuts whose order flips between them, i.e. that cross somewhere
        between the cuts. Parallel cuts: compare positions along the cut. Perpendicular cuts that
        bound a common region: chords interleave on the region boundary. Otherwise: none."""
        A, B = self.cut_cross[ca], self.cut_cross[cb]
        common = sorted(set(A) & set(B))
        if len(common) < 2:
            return []
        cu, cv = self.cuts[ca], self.cuts[cb]
        out = []
        if cu.orient == cv.orient:
            sa = {i: self._coord(cu, A[i]) for i in common}
            sb = {i: self._coord(cv, B[i]) for i in common}
            flip = lambda i, j: (sa[i] - sa[j]) * (sb[i] - sb[j]) < 0
        else:
            shared = ({cu.a, cu.b} & {cv.a, cv.b})
            if not shared:
                return []
            R = sorted(shared)[0]
            pa = {i: self._perim(R, A[i]) for i in common}
            pb = {i: self._perim(R, B[i]) for i in common}
            def flip(i, j):
                lo, hi = sorted((pa[i], pb[i]))
                return (lo < pa[j] < hi) != (lo < pb[j] < hi)
        for x in range(len(common)):
            for y in range(x + 1, len(common)):
                i, j = common[x], common[y]
                if self.conns[i].net != self.conns[j].net and flip(i, j):
                    out.append((i, j))
        return out

    def _channel_pairs(self):
        """Cut pairs worth comparing: any two cuts of the same orientation in one grid row (V) /
        column (H), plus pairs bounding a common region."""
        pairs = set()
        by_region = defaultdict(list)
        for cid, c in self.cuts.items():
            if c.label:
                continue
            by_region[c.a].append(cid)
            by_region[c.b].append(cid)
        for lst in by_region.values():
            for i in range(len(lst)):
                for j in range(i + 1, len(lst)):
                    pairs.add((lst[i], lst[j]))
        for orient, key in (("V", lambda c: self.regions[c.a].row), ("H", lambda c: self.regions[c.a].col)):
            grp = defaultdict(list)
            for cid, c in self.cuts.items():
                if c.orient == orient and not c.label:
                    grp[key(c)].append(cid)
            for lst in grp.values():
                for i in range(len(lst)):
                    for j in range(i + 1, len(lst)):
                        pairs.add((lst[i], lst[j]))
        return sorted(pairs)

    def inversions(self, top=8):
        """Cut pairs ranked by inversion count; each result lists the crossing net pairs.
        Pairs whose inversions are already contained in a bigger kept pair are dropped."""
        res = []
        for ca, cb in self._channel_pairs():
            inv = self.inversion_pairs(ca, cb)
            if inv:
                res.append((len(inv), ca, cb, inv))
        res.sort(key=lambda r: (-r[0], r[1], r[2]))
        kept, seen = [], []
        for n, ca, cb, inv in res:
            s = frozenset(inv)
            if any(s <= k for k in seen):
                continue
            seen.append(s)
            common = len(set(self.cut_cross[ca]) & set(self.cut_cross[cb]))
            names = sorted({tuple(sorted((self.conns[i].net, self.conns[j].net))) for i, j in inv})
            kept.append({"cuts": (ca, cb), "regions": (self.cuts[ca].a + "|" + self.cuts[ca].b, self.cuts[cb].a + "|" + self.cuts[cb].b),
                         "n_common": common, "inversions": n, "pairs": names})
            if len(kept) >= top:
                break
        return kept

    def region_load(self):
        """Per region: conns touching it, crossings inside it."""
        load = {r: {"conns": 0, "cross": 0, "len": 0.0} for r in self.regions}
        for c in self.conns:
            seen = set()
            for t in [i / 24 for i in range(25)]:
                p = (c.pa[0] + t * (c.pb[0] - c.pa[0]), c.pa[1] + t * (c.pb[1] - c.pa[1]))
                R = self.region_at(p).id
                if R not in seen:
                    seen.add(R)
                    load[R]["conns"] += 1
        for _, _, p in self.crossings:
            load[self.region_at(p).id]["cross"] += 1
        for c in self.cuts.values():
            pass
        return load

    def metrics(self):
        ov = [self.overflow(c) for c in self.cuts]
        return {"unrouted": len(self.conns), "wirelength": round(self.rn.wirelength, 1),
                "crossings": len(self.crossings), "cu_crossings": len(self.cu_crossings), "cu_locked": sum(1 for t in self.cu_cross_tracks if t.locked),
                "cut_overflow_sum": sum(ov), "cut_overflow_max": max(ov) if ov else 0,
                "inversions": sum(len(self.inversion_pairs(a, b)) for a, b in self._adjacent_pairs()),
                "plane_stubs": len(self.rn.stubs)}

    def _adjacent_pairs(self):
        return [p for p in self._channel_pairs() if {self.cuts[p[0]].a, self.cuts[p[0]].b} & {self.cuts[p[1]].a, self.cuts[p[1]].b}]


def build(board, rn, cell=21.0, light=False) -> Topo:
    return Topo(board, rn, cell, light=light)
