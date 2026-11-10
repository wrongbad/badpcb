"""Small 2D geometry helpers (pure stdlib). Points are (x, y) tuples/lists in mm."""
import math

EPS = 1e-9


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def seg_intersect(p1, p2, p3, p4):
    """Proper crossing of segments p1p2 and p3p4 (touching at endpoints / collinear overlap
    does NOT count). Returns the intersection point or None."""
    d1, d2 = cross(p3, p4, p1), cross(p3, p4, p2)
    d3, d4 = cross(p1, p2, p3), cross(p1, p2, p4)
    if ((d1 > EPS and d2 < -EPS) or (d1 < -EPS and d2 > EPS)) and \
       ((d3 > EPS and d4 < -EPS) or (d3 < -EPS and d4 > EPS)):
        t = d1 / (d1 - d2)
        return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))
    return None


def pt_seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 < EPS:
        return dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def closest_on_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 < EPS:
        return (a[0], a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return (a[0] + t * dx, a[1] + t * dy)


def seg_seg_dist(a, b, c, d):
    if seg_intersect(a, b, c, d):
        return 0.0
    return min(pt_seg_dist(a, c, d), pt_seg_dist(b, c, d), pt_seg_dist(c, a, b), pt_seg_dist(d, a, b))


def point_in_poly(p, poly):
    """Even-odd point-in-polygon; poly = [(x, y), ...] (implicitly closed)."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def poly_area(poly):
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def bbox_of(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def to_local(p, center, rot_deg):
    """World point -> frame of a body centred at `center`, rotated `rot_deg` (KiCad, CCW on screen, +Y down)."""
    a = math.radians(rot_deg)
    dx, dy = p[0] - center[0], p[1] - center[1]
    c, s = math.cos(a), math.sin(a)
    return (dx * c - dy * s, dx * s + dy * c)   # y-down frame: rotating by +rot_deg undoes the body's rotation


def clip_seg_rect(a, b, center, half, rot_deg=0.0, grow=0.0):
    """Parameter interval (t0, t1) of segment a->b inside a rotated rectangle (half extents `half`,
    grown by `grow`), or None if they do not touch."""
    la, lb = to_local(a, center, rot_deg), to_local(b, center, rot_deg)
    hx, hy = half[0] + grow, half[1] + grow
    t0, t1 = 0.0, 1.0
    dx, dy = lb[0] - la[0], lb[1] - la[1]
    for p, q in ((-dx, la[0] + hx), (dx, hx - la[0]), (-dy, la[1] + hy), (dy, hy - la[1])):
        if abs(p) < EPS:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return None
    return (t0, t1)


def seg_hits_rect(a, b, center, half, rot_deg=0.0, grow=0.0):
    """Does segment ab touch an (optionally rotated) rectangle with half extents `half`, grown by `grow`?"""
    return clip_seg_rect(a, b, center, half, rot_deg, grow) is not None


def seg_circle_interval(a, b, c, r):
    """Parameter interval (t0, t1) in [0, 1] of segment a->b inside circle (c, r), or None."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    fx, fy = a[0] - c[0], a[1] - c[1]
    A = dx * dx + dy * dy
    if A < EPS:
        return (0.0, 1.0) if fx * fx + fy * fy <= r * r else None
    B = 2 * (fx * dx + fy * dy)
    C = fx * fx + fy * fy - r * r
    D = B * B - 4 * A * C
    if D < 0:
        return None
    sq = math.sqrt(D)
    t0, t1 = (-B - sq) / (2 * A), (-B + sq) / (2 * A)
    t0, t1 = max(0.0, t0), min(1.0, t1)
    return (t0, t1) if t0 <= t1 else None


def mst(n, w):
    """Prim MST over nodes 0..n-1 with weight function w(i, j). Returns [(i, j)] edges."""
    if n < 2:
        return []
    inf = float("inf")
    best = [inf] * n
    par = [-1] * n
    used = [False] * n
    best[0] = 0.0
    edges = []
    for _ in range(n):
        u = -1
        for i in range(n):
            if not used[i] and (u < 0 or best[i] < best[u]):
                u = i
        used[u] = True
        if par[u] >= 0:
            edges.append((par[u], u))
        for v in range(n):
            if not used[v]:
                d = w(u, v)
                if d < best[v]:
                    best[v], par[v] = d, u
    return edges


def inversions(seq_a, seq_b):
    """Discordant pairs between two orderings of (mostly) the same items. Items present in both
    are compared. Returns list of (x, y) pairs with x before y in seq_a but after y in seq_b."""
    common = [x for x in seq_a if x in set(seq_b)]
    pos = {x: i for i, x in enumerate(seq_b)}
    out = []
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            if pos[common[i]] > pos[common[j]]:
                out.append((common[i], common[j]))
    return out


# ----------------------------------------------------------------------------- polygon x axis-aligned box (zone edits)
def _clip_half(poly, axis, val, keep_le):
    """Sutherland-Hodgman against the half-plane p[axis] <= val (keep_le) or >= val."""
    inside = (lambda p: p[axis] <= val + 1e-12) if keep_le else (lambda p: p[axis] >= val - 1e-12)
    out = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        ia, ib = inside(a), inside(b)
        if ia:
            out.append(a)
        if ia != ib:
            t = (val - a[axis]) / (b[axis] - a[axis])
            out.append([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])])
    return out


def box_poly(box):
    x0, y0, x1, y1 = box
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def poly_minus_box(poly, box):
    """Polygon (list of [x,y]) minus an axis-aligned box -> list of polygons (up to 4 pieces; exact for convex / rectilinear input)."""
    x0, y0, x1, y1 = box
    pieces = [_clip_half(poly, 0, x0, True), _clip_half(poly, 0, x1, False)]
    mid = _clip_half(_clip_half(poly, 0, x1, True), 0, x0, False)
    pieces.append(_clip_half(mid, 1, y0, True))
    pieces.append(_clip_half(mid, 1, y1, False))
    return [[[round(x, 4), round(y, 4)] for x, y in pc] for pc in pieces if len(pc) >= 3 and abs(poly_area(pc)) > 1e-6]
