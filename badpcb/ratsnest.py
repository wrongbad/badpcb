"""Ratsnest: which pads of each net still need connecting, given the copper that exists.

    rn = build(board)                    # Ratsnest
    rn.conns   -> [Conn(net, a, b, pa, pb, length, ia, ib)]   MST edges between *islands*
    rn.stubs   -> islands of plane nets with no via/through-pad reaching the plane (need a via)
    rn.islands -> every island (connected pads+copper of one net)

Islands: pads of one net merged through existing tracks/vias (locked or not) and through
same-numbered pad pieces (e.g. a via pad stacked on an SMD pad). A net with a zone is a *plane
net*: any island holding a via / through pad that reaches the zone layer inside the zone outline
is "served" and all served islands count as one connected piece, so plane nets only produce
`stubs`, never conns. Everything else gets an MST over islands (closest-point distances, so a
pad-less copper island such as a scripted lane acts as a relay; dangling pad-less leaves are
pruned). Plane nets can be forced for boards without zones via assume_planes=("GND", ...).
"""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional

from . import geom


@dataclass
class Island:
    net: str
    idx: int
    pads: List[str]
    pts: list                     # [(x, y)] pad centres + via positions
    segs: list                    # [((x,y),(x,y))] copper
    n_tracks: int = 0
    n_vias: int = 0
    served: Optional[bool] = None  # plane nets only

    @property
    def label(self):
        return self.pads[0] if self.pads else "%s~%d" % (self.net, self.idx)


@dataclass
class Conn:
    net: str
    a: str          # island label (pad id such as "U1.40", or "SENSE3~2" for pad-less copper)
    b: str
    pa: tuple       # attach point on island a
    pb: tuple
    length: float
    ia: int         # island indices (Island.idx within net)
    ib: int

    def as_dict(self):
        return {"net": self.net, "a": self.a, "b": self.b, "pa": [round(v, 3) for v in self.pa],
                "pb": [round(v, 3) for v in self.pb], "length": round(self.length, 2)}


@dataclass
class Ratsnest:
    conns: List[Conn] = field(default_factory=list)
    islands: dict = field(default_factory=dict)      # net -> [Island]
    stubs: List[Island] = field(default_factory=list)
    plane_nets: dict = field(default_factory=dict)   # net -> (n_served_islands, n_unserved)

    @property
    def wirelength(self):
        return sum(c.length for c in self.conns)


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _layer_span(board, via):
    ix = [board.layers.index(l) for l in via.layers if l in board.layers]
    return set(board.layers[min(ix):max(ix) + 1]) if ix else set(board.layers)


def _pad_half(pd):
    hx, hy = pd.size[0] / 2, pd.size[1] / 2
    return (hx, hy)


def _pt_in_pad(pt, pd, grow=0.0):
    lx, ly = geom.to_local(pt, pd.pos, pd.rot)
    hx, hy = _pad_half(pd)
    return abs(lx) <= hx + grow and abs(ly) <= hy + grow


def _items(board, net, pads, tracks, vias):
    """Return items = list of dicts with kind, layers, bbox, geometry."""
    it = []
    for pd in pads:
        r = max(pd.size) / 2 * 1.5
        it.append({"k": "pad", "o": pd, "layers": set(pd.layers), "bb": (pd.pos[0] - r, pd.pos[1] - r, pd.pos[0] + r, pd.pos[1] + r)})
    for t in tracks:
        h = t.w / 2
        it.append({"k": "trk", "o": t, "layers": {t.layer}, "bb": (min(t.a[0], t.b[0]) - h, min(t.a[1], t.b[1]) - h,
                                                                   max(t.a[0], t.b[0]) + h, max(t.a[1], t.b[1]) + h)})
    for v in vias:
        h = v.dia / 2
        it.append({"k": "via", "o": v, "layers": _layer_span(board, v), "bb": (v.pos[0] - h, v.pos[1] - h, v.pos[0] + h, v.pos[1] + h)})
    return it


def _touch(a, b):
    ka, kb = a["k"], b["k"]
    if not (a["layers"] & b["layers"]):
        return False
    if ka > kb:
        a, b, ka, kb = b, a, kb, ka
    oa, ob = a["o"], b["o"]
    if ka == "pad" and kb == "pad":
        return oa.ref == ob.ref and oa.num == ob.num
    if ka == "pad" and kb == "trk":
        return geom.seg_hits_rect(ob.a, ob.b, oa.pos, _pad_half(oa), oa.rot, ob.w / 2)
    if ka == "pad" and kb == "via":
        return _pt_in_pad(ob.pos, oa, ob.dia / 4)
    if ka == "trk" and kb == "trk":
        return geom.seg_seg_dist(oa.a, oa.b, ob.a, ob.b) <= 0.5 * min(oa.w, ob.w) + 1e-3
    if ka == "trk" and kb == "via":
        return geom.pt_seg_dist(ob.pos, oa.a, oa.b) <= ob.dia / 2
    if ka == "via" and kb == "via":
        return geom.dist(oa.pos, ob.pos) <= min(oa.dia, ob.dia) / 2
    return False


def _islands_of(board, net, pads, tracks, vias):
    items = _items(board, net, pads, tracks, vias)
    uf = _UF(len(items))
    grid = defaultdict(list)
    cs = 2.0
    for i, it in enumerate(items):
        x0, y0, x1, y1 = it["bb"]
        for gx in range(int(x0 // cs), int(x1 // cs) + 1):
            for gy in range(int(y0 // cs), int(y1 // cs) + 1):
                grid[(gx, gy)].append(i)
    seen = set()
    for cell in grid.values():
        for ii in range(len(cell)):
            for jj in range(ii + 1, len(cell)):
                i, j = cell[ii], cell[jj]
                if (i, j) in seen or uf.find(i) == uf.find(j):
                    continue
                seen.add((i, j))
                if geom.bbox_overlap(items[i]["bb"], items[j]["bb"]) and _touch(items[i], items[j]):
                    uf.union(i, j)
    groups = defaultdict(list)
    for i in range(len(items)):
        groups[uf.find(i)].append(items[i])
    return list(groups.values())


def _served(board, group, net, assume):
    """Does this island touch a zone of `net` (a via / through pad barrel reaching the zone layer,
    or copper on the zone's own layer) inside the zone outline?"""
    zones = [z for z in board.zones if z.net == net]
    for it in group:
        o = it["o"]
        pts = [o.a, o.b] if it["k"] == "trk" else [o.pos]
        through = it["k"] == "via" or (it["k"] == "pad" and o.through)
        if not zones and net in assume and through:
            return True
        for z in zones:
            if not (it["layers"] & set(z.layers)):
                continue
            for pt in pts:
                for zp in z.polys:
                    if geom.point_in_poly(pt, zp["outer"]) and not any(geom.point_in_poly(pt, h) for h in zp["holes"]):
                        return True
    return False


def _island_dist(A, B):
    best = (float("inf"), None, None)
    for P, Q in ((A, B), (B, A)):
        for p in P.pts:
            for q in Q.pts:
                d = geom.dist(p, q)
                if d < best[0]:
                    best = (d, p, q) if P is A else (d, q, p)
            for s in Q.segs:
                d = geom.pt_seg_dist(p, *s)
                if d < best[0]:
                    c = geom.closest_on_seg(p, *s)
                    best = (d, p, c) if P is A else (d, c, p)
    return best


def build(board, assume_planes=(), nets=None) -> Ratsnest:
    """nets: optional iterable of net names; only those nets are (re)built (used for incremental what-if)."""
    only = None if nets is None else set(nets)
    assume = set(assume_planes)
    plane_nets = set(board.plane_nets()) | assume
    pads_by, tr_by, via_by = defaultdict(list), defaultdict(list), defaultdict(list)
    for pd in board.pads():
        if pd.net and (only is None or pd.net in only):
            pads_by[pd.net].append(pd)
    for t in board.tracks:
        if t.net and (only is None or t.net in only):
            tr_by[t.net].append(t)
    for v in board.vias:
        if v.net and (only is None or v.net in only):
            via_by[v.net].append(v)
    rn = Ratsnest()
    for net in sorted(pads_by):
        groups = _islands_of(board, net, pads_by[net], tr_by[net], via_by[net])
        isl = []
        for g in groups:
            pads = sorted({it["o"].id for it in g if it["k"] == "pad"})
            pts = [it["o"].pos for it in g if it["k"] == "pad"] + [it["o"].pos for it in g if it["k"] == "via"]
            segs = [(it["o"].a, it["o"].b) for it in g if it["k"] == "trk"]
            I = Island(net, 0, pads, pts, segs, sum(it["k"] == "trk" for it in g), sum(it["k"] == "via" for it in g))
            if net in plane_nets:
                I.served = _served(board, g, net, assume)
            isl.append(I)
        # deterministic order: by (first pad id natural, or position)
        isl.sort(key=lambda I: (0, _nat(I.pads[0])) if I.pads else (1, (round(I.segs[0][0][0], 2), round(I.segs[0][0][1], 2)) if I.segs else (round(I.pts[0][0], 2), round(I.pts[0][1], 2))))
        for k, I in enumerate(isl):
            I.idx = k
        rn.islands[net] = isl
        if net in plane_nets:
            rn.plane_nets[net] = (sum(1 for I in isl if I.served), sum(1 for I in isl if not I.served))
            rn.stubs += [I for I in isl if not I.served and I.pads]
            continue
        if len(isl) < 2:
            continue
        n = len(isl)
        cache = {}

        def w(i, j):
            key = (min(i, j), max(i, j))
            if key not in cache:
                cache[key] = _island_dist(isl[key[0]], isl[key[1]])
            return cache[key][0]
        edges = geom.mst(n, w)
        # prune pad-less leaves
        while True:
            deg = defaultdict(int)
            for a, b in edges:
                deg[a] += 1
                deg[b] += 1
            drop = [i for i in deg if deg[i] == 1 and not isl[i].pads]
            if not drop:
                break
            edges = [(a, b) for a, b in edges if a not in drop and b not in drop]
        for a, b in edges:
            a, b = min(a, b), max(a, b)
            d, pa, pb = cache[(a, b)]      # pa lies on island a (the lower index)
            rn.conns.append(Conn(net, isl[a].label, isl[b].label, pa, pb, d, a, b))
    return rn


def _nat(s):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
