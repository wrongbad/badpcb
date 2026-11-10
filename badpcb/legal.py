"""Pad-level placement legality (courtyard boxes are only a first filter).

`pad_conflicts(world, ref, pos, rot, side)` -> list of strings for the moved part's pads that would be closer than the
board clearance to
  * pads of other parts (SMD on a shared copper layer, and through-hole pads, which sit on every layer),
  * foreign-net tracks / vias on the pad's layers (copper that stays where it is when a part moves),
  * mounting / NPTH holes,
  * the board edge (copper edge clearance).
Same-net neighbours are ignored (KiCad does not flag them). Only conflicts that are NEW relative to the part's current
position count (`new_pad_conflicts`), so a board that already has a violation can still be improved step by step.
Pads are rectangles (rect/roundrect/trapezoid: polygon) or capsules (circle/oval); roundrect corners are not rounded
(slightly conservative).
"""
import math
from collections import defaultdict

from . import geom

CELL = 4.0


def _to_world(lx, ly, rot):
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    return lx * c + ly * s, -lx * s + ly * c


class Shape:
    """kind 'poly' (convex, pts) or 'cap' (segment p0-p1 inflated by r); bbox for quick rejection."""
    __slots__ = ("kind", "pts", "r", "bbox")

    def __init__(self, kind, pts, r=0.0):
        self.kind, self.pts, self.r = kind, pts, r
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self.bbox = (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)


def pad_shape(pos, rot, size, shape):
    w, h = size
    if shape in ("circle", "oval"):
        r = min(w, h) / 2
        ext = (max(w, h) - min(w, h)) / 2
        a, b = ((-ext, 0.0), (ext, 0.0)) if w >= h else ((0.0, -ext), (0.0, ext))
        pa = _to_world(a[0], a[1], rot)
        pb = _to_world(b[0], b[1], rot)
        return Shape("cap", [(pos[0] + pa[0], pos[1] + pa[1]), (pos[0] + pb[0], pos[1] + pb[1])], r)
    pts = []
    for lx, ly in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        x, y = _to_world(lx, ly, rot)
        pts.append((pos[0] + x, pos[1] + y))
    return Shape("poly", pts)


def _poly_edges(P):
    return [(P[i], P[(i + 1) % len(P)]) for i in range(len(P))]


def _inside(p, P):
    return geom.point_in_poly(p, P)


def _poly_poly(P, Q):
    for a, b in _poly_edges(P):
        for c, d in _poly_edges(Q):
            if geom.seg_intersect(a, b, c, d):
                return 0.0
    if _inside(P[0], Q) or _inside(Q[0], P):
        return 0.0
    return min(min(min(geom.pt_seg_dist(p, c, d) for c, d in _poly_edges(Q)) for p in P),
               min(min(geom.pt_seg_dist(q, a, b) for a, b in _poly_edges(P)) for q in Q))


def _seg_poly(a, b, P):
    if _inside(a, P) or _inside(b, P):
        return 0.0
    return min(geom.seg_seg_dist(a, b, c, d) for c, d in _poly_edges(P))


def dist(s1, s2):
    """Gap between two shapes (0 = touching/overlapping)."""
    if s1.kind == "poly" and s2.kind == "poly":
        return _poly_poly(s1.pts, s2.pts)
    if s1.kind == "cap" and s2.kind == "cap":
        return max(0.0, geom.seg_seg_dist(s1.pts[0], s1.pts[1], s2.pts[0], s2.pts[1]) - s1.r - s2.r)
    c, p = (s1, s2) if s1.kind == "cap" else (s2, s1)
    return max(0.0, _seg_poly(c.pts[0], c.pts[1], p.pts) - c.r)


def seg_shape(a, b, w):
    return Shape("cap", [tuple(a), tuple(b)], w / 2)


def circ_shape(p, d):
    return Shape("cap", [tuple(p), tuple(p)], d / 2)


# --------------------------------------------------------------------------- indexes (cached on the world)
def _cells(bb):
    for i in range(int(bb[0] // CELL), int(bb[2] // CELL) + 1):
        for j in range(int(bb[1] // CELL), int(bb[3] // CELL) + 1):
            yield (i, j)


def _pad_index(world):
    """cell -> [(ref, pad, Shape)]; rebuilt when any part moved (world._pidx_ver)."""
    ver = getattr(world, "place_ver", 0)
    idx = getattr(world, "_pidx", None)
    if idx is not None and idx[0] == ver:
        return idx[1]
    g = defaultdict(list)
    for ref, p in world.board.parts.items():
        for pd in p.pads:
            sh = pad_shape(pd.pos, pd.rot, pd.size, pd.shape)
            e = (ref, pd, sh)
            for c in _cells(sh.bbox):
                g[c].append(e)
    world._pidx = (ver, g)
    return g


def _copper_index(world):
    """cell -> [(Shape, layers set, net, label)] for tracks, vias (cached on track count / lock version)."""
    b = world.board
    key = (len(b.tracks), len(b.vias), getattr(b, "lock_ver", 0))
    idx = getattr(world, "_cidx", None)
    if idx is not None and idx[0] == key:
        return idx[1]
    g = defaultdict(list)
    for t in b.tracks:
        sh = seg_shape(t.a, t.b, t.w)
        e = (sh, {t.layer}, t.net, "track %s %s%s" % (t.net or "-", t.layer, " [lane]" if getattr(t, "cls", "") == "lane" else ""))
        for c in _cells(sh.bbox):
            g[c].append(e)
    for v in b.vias:
        sh = circ_shape(v.pos, v.dia)
        e = (sh, set(b.layers), v.net, "via %s" % (v.net or "-"))
        for c in _cells(sh.bbox):
            g[c].append(e)
    world._cidx = (key, g)
    return g


# --------------------------------------------------------------------------- the check
def pad_conflicts(world, ref, pos, rot, side, clr=None):
    """Strings describing every pad-level conflict of part `ref` placed at (pos, rot, side)."""
    from . import actions as A
    b = world.board
    clr = b.rules.clearance if clr is None else clr
    part = b.parts[ref]
    _, pads = A.place_geom(world, ref, pos, rot, side)
    pidx, cidx = _pad_index(world), _copper_index(world)
    out = []
    bx = b.bbox
    ec = b.rules.edge_clearance
    for pd, (pp, pr, pl) in zip(part.pads, pads):
        sh = pad_shape(pp, pr, pd.size, pd.shape)
        pb = (sh.bbox[0] - clr, sh.bbox[1] - clr, sh.bbox[2] + clr, sh.bbox[3] + clr)
        pl = set(pl)
        seen = set()
        for c in _cells(pb):
            for oref, od, osh in pidx.get(c, ()):
                if oref == ref or id(od) in seen:
                    continue
                seen.add(id(od))
                if not (pl & set(od.layers)) and not (pd.through or od.through):
                    continue
                if not geom.bbox_overlap(pb, osh.bbox):
                    continue
                if pd.net and pd.net == od.net:
                    continue
                d = dist(sh, osh)
                if d < clr - 1e-6:
                    out.append("pad %s.%s%s %.2f mm from pad %s.%s%s (< clearance %.2f)%s" % (
                        ref, pd.num, "(%s)" % pd.net if pd.net else "", d, oref, od.num, "(%s)" % od.net if od.net else "", clr,
                        " [through-hole]" if od.through else ""))
            for csh, lay, net, label in cidx.get(c, ()):
                if id(csh) in seen:
                    continue
                seen.add(id(csh))
                if not (pl & lay) or (pd.net and net == pd.net) or not geom.bbox_overlap(pb, csh.bbox):
                    continue
                d = dist(sh, csh)
                if d < clr - 1e-6:
                    if label.endswith("[lane]"):
                        out.append("pad %s.%s %.2f mm from foreign %s (< clearance %.2f; lanes are kept by apply)" % (ref, pd.num, d, label, clr))
                    else:
                        out.append("WARN pad %s.%s %.2f mm from foreign %s: apply will rip that copper up" % (ref, pd.num, d, label))
        for h in b.holes:
            if h.ref != ref and pb[0] - h.drill <= h.pos[0] <= pb[2] + h.drill and pb[1] - h.drill <= h.pos[1] <= pb[3] + h.drill:
                d = dist(sh, circ_shape(h.pos, h.drill))
                if d < clr - 1e-6:
                    out.append("pad %s.%s %.2f mm from hole of %s (< %.2f)" % (ref, pd.num, d, h.ref, clr))
        if sh.bbox[0] < bx[0] + ec - 1e-6 or sh.bbox[1] < bx[1] + ec - 1e-6 or sh.bbox[2] > bx[2] - ec + 1e-6 or sh.bbox[3] > bx[3] - ec + 1e-6:
            out.append("pad %s.%s closer than %.2f mm to the board edge" % (ref, pd.num, ec))
    return out


def new_pad_conflicts(world, ref, pos, rot, side):
    """Conflicts at the new placement that are not already present at the current one (matched by the neighbour, not the number)."""
    p = world.board.parts[ref]
    new = list(dict.fromkeys(pad_conflicts(world, ref, pos, rot, side)))
    if not new:
        return []
    strip = lambda s: s.split(" mm from ", 1)[-1] if " mm from " in s else s
    was = {strip(s) for s in pad_conflicts(world, ref, p.pos, p.rot, p.side)}
    return [s for s in new if strip(s) not in was]


def errors(msgs):
    return [m for m in msgs if not m.startswith("WARN ")]


def warnings(msgs):
    return [m[5:] for m in msgs if m.startswith("WARN ")]
