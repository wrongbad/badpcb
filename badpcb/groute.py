"""groute: PathFinder (McMurchie & Ebeling 1995) negotiated-congestion global router + explanations.

    python3 -m badpcb.groute <board.kicad_pcb|model.json> [--cell 1.0] [--iters 30] [--time 30]
                         [--hints h.json] [--nets EN*,SENSE*] [--region R8|C12] [--json out.json]
    groute.main(argv) -> int          (same arguments; for wiring into cli.py)
    groute.run(board, rn, **opts) -> Result   (library entry; Result.text(), Result.to_json())

MODEL
  Grid: cells of --cell mm (default 1.0) over the board bbox, signal layers only (planes are not routed).
  Node = (layer, cx, cy).  Resources, each with a capacity measured in *tracks*:
    * planar edge between 4-neighbour cells: cap = how many width+clearance tracks fit through the free
      part of the shared cell boundary.  Free = not covered by pads/tracks/vias/holes of OTHER nets,
      keepouts or the board edge (obstacles are rasterised at --cell/10 mm, inflated by clearance + w/2).
      Same-net copper is free (it is also source/sink), so capacity is per net (cap_for_net).
      Locked lanes of other nets therefore reduce capacity exactly where they sit.
    * via (layer step in a cell): cap = number of via sites (0.8 mm pitch) free of foreign copper,
      through-pads, holes, keepouts.
    * crossing ("X"): in one cell on one layer, a net going straight (E-W or N-S) conflicts with another net
      going straight the perpendicular way or turning a corner there (they would have to cross); each such
      pair is 1 overflow on the cell.  This is what forces layer changes at crossings.
  Connections: island-to-island MST edges from ratsnest.py.  Source/sink = all cells of the island's copper
  (pads, tracks, vias): same-net copper is free.  Cells within 1 cell of a terminal get cap >= --esc-cap
  (pin escapes are denser than the grid can resolve; shown as "escape" in explanations).
  Cost = (base + history) * (1 + pf * overuse) per resource, base = cell length (mm) x layer multiplier,
  via = --via-cost mm.  Iteration 0 routes everything, then only connections touching an overused
  resource are ripped and rerouted; pf grows x1.5 per iteration, history accumulates the overuse.
  Always returns within --time / --iters with the best state (least overflow) seen.

HINTS (--hints file.json: a JSON list; unknown entries are reported, not fatal)
  {"type":"layer","net":"SENSE3","layer":"B.Cu","strength":3.0}   other layers cost x strength (default 3)
  {"type":"corridor","net":"EN3","via":["C12","C14","R8"]}        edges crossing those cuts / inside those
                                                                   regions cost x0.35 (preferred route)
  {"type":"priority","net":"nRESET","level":2}                    routed first; congestion penalty /(1+level)
  {"type":"freeze","region":"R8" | "bbox":[x0,y0,x1,y1], "layers":["F.Cu"], "net":"X"}
                                                                   cells reserved: nobody (except net X
                                                                   if given) may route through them
  net may be a glob ("EN*").  Region / cut ids are topo.py's (R<n>, C<n>).

OUTPUT (text): summary, convergence curve, hot spots (cut/region + competing nets + accumulated history),
  thrash table (rip counts + fight pairs), blocked report per overflowing connection
  (bottleneck, who else wants it, cheapest alternatives, rip history), ASCII history heat map per layer.
  --json: full geometry per connection (cell-centre points with layer, via locations, cuts crossed).

Known limits: 4-neighbour moves (Manhattan staircase; Phase 4 must smooth), 2 signal layers assumed to be
adjacent for vias (generalises to a stack), crossing model is conservative for corner turns (ignored),
plane stubs (pads needing a plane via) are not routed, no incremental import of earlier routes.
"""
import argparse
import fnmatch
import heapq
import json
import math
import os
import re
import subprocess
import sys
import time
from array import array
from collections import defaultdict

from . import config, geom, model, ratsnest

INF = float("inf")


# ----------------------------------------------------------------------------------------- loading
def load_board(path):
    """.json -> model.load; .kicad_pcb -> extract (same cache as cli.py)."""
    from . import cli
    return cli.load_board(path)


# ----------------------------------------------------------------------------------------- names
class Names:
    """Region / cut handles. Uses topo.py (read-only) when it builds, else a plain 20 mm grid."""

    def __init__(self, board, rn):
        self.T = None
        self.board = board
        try:
            from . import topo
            self.T = topo.build(board, rn)
        except Exception as ex:          # pragma: no cover - other agent may be editing topo.py
            self.err = repr(ex)
        self._cache = {}
        if self.T:
            self.cuts = sorted(self.T.cuts.values(), key=lambda c: (0 if c.label else 1, c.id))
        else:
            self.cuts = []
        bx = board.bbox
        self._ng = max(1, round((bx[2] - bx[0]) / 20.0))

    def region(self, pt):
        if self.T:
            return self.T.region_at(pt).id
        bx = self.board.bbox
        return "G%d_%d" % (int((pt[0] - bx[0]) // 20), int((pt[1] - bx[1]) // 20))

    def region_bbox(self, rid):
        if self.T and rid in self.T.regions:
            return self.T.regions[rid].bbox
        if self.T and rid.endswith("-in") and rid[:-3] in self.T.rings:
            return self.T.rings[rid[:-3]]
        return None

    def ring_of(self, pt):
        """'U1-in' when pt lies inside the escape ring of a big part (inside its ring cuts)."""
        if self.T:
            for ref, bb in self.T.rings.items():
                if bb[0] <= pt[0] <= bb[2] and bb[1] <= pt[1] <= bb[3]:
                    return ref + "-in"
        return None

    def cut_label(self, cid):
        c = self.T.cuts.get(cid) if self.T else None
        return c.label if c else ""

    def handle(self, a, b):
        """Handle of the segment a-b: a cut id if it crosses one (ring cuts first), else its region."""
        key = (a, b)
        r = self._cache.get(key)
        if r is None:
            r = None
            for c in self.cuts:
                if geom.seg_intersect(a, b, c.p0, c.p1):
                    r = c.id
                    break
            if r is None:
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                r = self.ring_of(mid) or self.region(mid)
            self._cache[key] = r
        return r

    def cuts_crossed(self, a, b):
        return [c.id for c in self.cuts if geom.seg_intersect(a, b, c.p0, c.p1)]


# ----------------------------------------------------------------------------------------- hints
def load_hints(path):
    if not path:
        return []
    with open(path) as fh:
        h = json.load(fh)
    if isinstance(h, dict):
        h = h.get("hints", [])
    return h


def _glob_nets(pat, nets):
    return [n for n in nets if fnmatch.fnmatchcase(n, pat)]


# ----------------------------------------------------------------------------------------- router
class GRouter:
    def __init__(self, board, rn, conns=None, cell=1.0, res=0.1, via_cost=6.0, esc_cap=2, hints=(), names=None,
                 verbose=False, hist_inc=0.6, pf0=0.5, pf_grow=1.5, bend=0.12, styles=None):
        self.t0 = time.time()
        self.b = board
        self.rn = rn
        self.names = names or Names(board, rn)
        self.s = float(cell)
        self.k = max(1, int(round(self.s / res)))
        self.res = self.s / self.k
        self.via_cost, self.esc_cap, self.hist_inc, self.pf0, self.pf_grow, self.bend_c = via_cost, esc_cap, hist_inc, pf0, pf_grow, bend
        self.layers = board.signal_layers()
        self.L = len(self.layers)
        self.lidx = {n: i for i, n in enumerate(self.layers)}
        bx = board.bbox
        self.gx0, self.gy0 = bx[0], bx[1]
        self.nx = int(math.ceil((bx[2] - bx[0]) / self.s - 1e-9))
        self.ny = int(math.ceil((bx[3] - bx[1]) / self.s - 1e-9))
        self.N = self.nx * self.ny
        self.LN = self.L * self.N
        self.nid = {n: i + 1 for i, n in enumerate(board.nets)}
        self.nname = {v: k for k, v in self.nid.items()}
        self._init_styles(styles)
        rl = board.rules
        self.rl = rl
        self.conns = list(conns if conns is not None else rn.conns)
        self.verbose = verbose
        self.hints = list(hints)
        self.hint_notes = []
        self._log("grid %dx%d x %d layers, cell %.2f mm, raster %.3f mm" % (self.nx, self.ny, self.L, self.s, self.res))
        if not self._cache_load():
            self._rasterise()
            self._capacities()
            self._cache_save()
        self._terminals()
        self._relax_escapes()
        self._init_state()
        self._apply_hints()

    # --- per-net costs from net styles (badpcb/styles.py): empty when no styles -> identical to the global-cost router ----------
    AVOID_COST = 0.8          # extra cost of a cell next to a net to avoid, in cells of path (1.0 = one more cell)

    def _init_styles(self, styles):
        self.net_viapen, self.net_lenw, self.net_avoid = {}, {}, {}
        for n, st in (styles or {}).items():
            i = self.nid.get(n)
            if i is None:
                continue
            if st.via_penalty:
                self.net_viapen[i] = float(st.via_penalty)
            if st.detour_max:
                # tight detour budget -> path length counts more against congestion/history terms (1.5 = default budget, no change)
                w = 1.0 + 2.0 * max(0.0, 1.5 - st.detour_max) / 1.5
                if w != 1.0:
                    self.net_lenw[i] = w
            for pat in st.avoid:
                for m in self.b.nets:
                    if m != n and (m == pat or fnmatch.fnmatchcase(m, pat)) and m in self.nid:
                        self.net_avoid.setdefault(i, set()).add(self.nid[m])
                        self.net_avoid.setdefault(self.nid[m], set()).add(i)      # keeping apart is mutual

    def _avoid_near(self, N_id):
        """Node ids (layer*N + cell) within one cell of the copper of the nets N_id avoids (None when it avoids nothing)."""
        av = self.net_avoid.get(N_id)
        if not av:
            return None
        near, nx, LN2 = set(), self.nx, 2 * self.LN
        for o in av:
            for r in self.netres.get(o, ()):
                if r < LN2:
                    u = r // 2
                    v = u + (1 if r % 2 == 0 else nx)
                    for w in (u, v):
                        near.update((w, w - 1, w + 1, w - nx, w + nx))
        return near

    def _log(self, msg):
        if self.verbose:
            print("[groute %.1fs] %s" % (time.time() - self.t0, msg), file=sys.stderr)

    # --- disk cache of the (expensive, conn-independent) rasters and capacities --------------------
    def _fingerprint(self):
        import hashlib
        b = self.b
        h = hashlib.sha1()
        h.update(repr((b.bbox, b.layers, vars(b.rules), self.s, self.k)).encode())
        for pd in b.pads():
            h.update(repr((pd.ref, pd.num, pd.pos, pd.layers, pd.size, pd.rot, pd.net, pd.shape, pd.drill)).encode())
        for t in b.tracks:
            h.update(repr((t.a, t.b, t.w, t.layer, t.net)).encode())
        for v in b.vias:
            h.update(repr((v.pos, v.dia, v.net)).encode())
        for x in b.holes:
            h.update(repr((x.pos, x.drill)).encode())
        for z in b.keepouts:
            h.update(repr((z.layers, z.polys, z.keepout)).encode())
        return h.hexdigest()[:16]

    def _cache_path(self):
        return os.path.join(config.current().cache_dir, "groute_%s.pkl" % self._fingerprint())

    def _cache_load(self):
        if os.environ.get("BADPCB_NO_CACHE"):
            return False
        try:
            import pickle
            with open(self._cache_path(), "rb") as fh:
                d = pickle.load(fh)
            self.ras, self.cap, self.ovr, self.RW, self.RH = d["ras"], d["cap"], d["ovr"], d["RW"], d["RH"]
            self._log("loaded raster/capacity cache")
            return True
        except Exception:
            return False

    def _cache_save(self):
        if os.environ.get("BADPCB_NO_CACHE"):
            return
        try:
            import pickle
            os.makedirs(config.current().cache_dir, exist_ok=True)
            tmp = self._cache_path() + ".tmp%d" % os.getpid()
            with open(tmp, "wb") as fh:
                pickle.dump({"ras": self.ras, "cap": self.cap, "ovr": self.ovr, "RW": self.RW, "RH": self.RH}, fh, protocol=4)
            os.replace(tmp, self._cache_path())
        except Exception:
            pass

    # --- geometry helpers -------------------------------------------------------------------
    def cell_of(self, p):
        return (min(self.nx - 1, max(0, int((p[0] - self.gx0) / self.s))),
                min(self.ny - 1, max(0, int((p[1] - self.gy0) / self.s))))

    def center(self, cx, cy):
        return (self.gx0 + (cx + 0.5) * self.s, self.gy0 + (cy + 0.5) * self.s)

    def node_xy(self, n):
        l, r = divmod(n, self.N)
        y, x = divmod(r, self.nx)
        return l, x, y

    def res_info(self, r):
        """Resource -> ('E'|'V'|'X', layer, (cx,cy), (cx2,cy2))."""
        if r >= 2 * self.LN:
            l, c = divmod(r - 2 * self.LN, self.N)
            y, x = divmod(c, self.nx)
            return "V", l, (x, y), (x, y)
        n, ax = divmod(r, 2)
        l, x, y = self.node_xy(n)
        return "E", l, (x, y), ((x + 1, y) if ax == 0 else (x, y + 1))

    def res_pts(self, r):
        _, l, a, b = self.res_info(r)
        return self.center(*a), self.center(*b)

    # --- rasterise obstacles ------------------------------------------------------------------
    def _pix(self, bb, test=None):
        """Raster indices inside bbox (mm) passing test(px, py)."""
        res, k = self.res, self.k
        ox, oy, RW, RH = self.gx0, self.gy0, self.RW, self.RH
        ix0, ix1 = max(0, int((bb[0] - ox) / res)), min(RW - 1, int((bb[2] - ox) / res))
        iy0, iy1 = max(0, int((bb[1] - oy) / res)), min(RH - 1, int((bb[3] - oy) / res))
        out = []
        for iy in range(iy0, iy1 + 1):
            base = iy * RW
            if test is None:
                out.extend(range(base + ix0, base + ix1 + 1))
                continue
            py = oy + (iy + 0.5) * res
            for ix in range(ix0, ix1 + 1):
                if test(ox + (ix + 0.5) * res, py):
                    out.append(base + ix)
        return out

    def _paint(self, layer_ids, kval, idxs):
        for l in layer_ids:
            arr = self.ras[l]
            if kval < 0:
                for i in idxs:
                    arr[i] = -1
            else:
                for i in idxs:
                    v = arr[i]
                    if v == 0:
                        arr[i] = kval
                    elif v != kval:
                        arr[i] = -1

    def _shape_idx(self, kind, a, b, c, d, grow):
        """kind 'C': centre a, radius b. 'R': centre a, half b, rot c. 'S': seg a-b radius c."""
        if kind == "C":
            r = b + grow
            cx, cy = a
            return self._pix((cx - r, cy - r, cx + r, cy + r),
                             lambda x, y: (x - cx) * (x - cx) + (y - cy) * (y - cy) <= r * r)
        if kind == "R":
            hx, hy = b
            rot = c % 180.0
            if abs(rot) < 1e-6:
                return self._pix((a[0] - hx - grow, a[1] - hy - grow, a[0] + hx + grow, a[1] + hy + grow))
            if abs(rot - 90.0) < 1e-6:
                return self._pix((a[0] - hy - grow, a[1] - hx - grow, a[0] + hy + grow, a[1] + hx + grow))
            rr = math.hypot(hx, hy) + grow
            return self._pix((a[0] - rr, a[1] - rr, a[0] + rr, a[1] + rr),
                             lambda x, y: self._in_rect(x, y, a, hx + grow, hy + grow, c))
        if kind == "S":
            r = c
            bb = (min(a[0], b[0]) - r, min(a[1], b[1]) - r, max(a[0], b[0]) + r, max(a[1], b[1]) + r)
            return self._pix(bb, lambda x, y: geom.pt_seg_dist((x, y), a, b) <= r)
        raise ValueError(kind)

    @staticmethod
    def _in_rect(x, y, cen, hx, hy, rot):
        lx, ly = geom.to_local((x, y), cen, rot)
        return abs(lx) <= hx and abs(ly) <= hy

    def _rasterise(self):
        b, rl = self.b, self.rl
        self.RW, self.RH = self.nx * self.k, self.ny * self.k
        self.ras = [array("i", bytes(4 * self.RW * self.RH)) for _ in range(self.L)]
        self.hard = bytearray(self.RW * self.RH)             # via-blocking, any net
        g = rl.clearance + rl.track_width / 2                 # track-centre keep-out around foreign copper
        vr = rl.via_dia / 2
        allL = list(range(self.L))
        t0 = time.time()
        # board edge band + outside
        e = rl.edge_clearance + rl.track_width / 2
        bx = b.bbox
        W, H = self.RW, self.RH
        band = []
        res = self.res
        for iy in range(H):
            py = self.gy0 + (iy + 0.5) * res
            for ix in range(W):
                px = self.gx0 + (ix + 0.5) * res
                if px < bx[0] + e or px > bx[2] - e or py < bx[1] + e or py > bx[3] - e:
                    band.append(iy * W + ix)
        self._paint(allL, -1, band)
        for i in band:
            self.hard[i] = 1
        # pads
        for pd in b.pads():
            if not pd.layers:
                continue
            lay = [self.lidx[l] for l in pd.layers if l in self.lidx]
            if not lay:
                continue
            kv = self.nid.get(pd.net, -1) if pd.net else -1
            if pd.shape == "circle":
                shp = ("C", pd.pos, max(pd.size) / 2, None, 0)
            else:
                shp = ("R", pd.pos, (pd.size[0] / 2, pd.size[1] / 2), pd.rot, 0)
            idx = self._shape_idx(*shp, g)
            self._paint(lay, kv, idx)
            for i in self._shape_idx(*shp, vr):       # no via on top of (or touching) any pad
                self.hard[i] = 1
        # tracks
        for t in b.tracks:
            if t.layer not in self.lidx:
                continue
            kv = self.nid.get(t.net, -1) if t.net else -1
            self._paint([self.lidx[t.layer]], kv, self._shape_idx("S", t.a, t.b, t.w / 2 + g, None, 0))
        # vias
        for v in b.vias:
            kv = self.nid.get(v.net, -1) if v.net else -1
            idx = self._shape_idx("C", v.pos, v.dia / 2, None, None, g)
            self._paint(allL, kv, idx)
            for i in self._shape_idx("C", v.pos, v.dia / 2, None, None, vr + rl.clearance):
                self.hard[i] = 1
        # holes (NPTH)
        for h in b.holes:
            self._paint(allL, -1, self._shape_idx("C", h.pos, h.drill / 2, None, None, g))
            for i in self._shape_idx("C", h.pos, h.drill / 2, None, None, vr + rl.clearance):
                self.hard[i] = 1
        # keepouts
        for z in b.keepouts:
            ko = z.keepout or {}
            lay = [self.lidx[l] for l in z.layers if l in self.lidx] or allL
            for poly in z.polys:
                pts = poly["outer"]
                bb = geom.bbox_of(pts)
                bb = (bb[0] - g, bb[1] - g, bb[2] + g, bb[3] + g)
                idx = self._pix(bb, lambda x, y, pts=pts: geom.point_in_poly((x, y), pts))
                if ko.get("no_tracks", True):
                    self._paint(lay, -1, idx)
                if ko.get("no_vias", True):
                    for i in idx:
                        self.hard[i] = 1
        self._log("rasterised in %.1fs" % (time.time() - t0))

    # --- capacities ----------------------------------------------------------------------------
    def _runs_cap(self, mask):
        pitch, res = self.rl.pitch, self.res
        n, run = 0, 0
        for m in mask:
            if m:
                run += 1
            else:
                if run:
                    n += int((run - 1) * res / pitch + 1e-9) + 1
                run = 0
        if run:
            n += int((run - 1) * res / pitch + 1e-9) + 1
        return n

    def _capacities(self):
        t0 = time.time()
        nx, ny, N, LN, k, RW = self.nx, self.ny, self.N, self.LN, self.k, self.RW
        R = 3 * LN
        self.cap = [0] * R
        self.ovr = {}                       # resource -> {nid: cap} where it differs (own-net copper is free)
        for l in range(self.L):
            ras = self.ras[l]
            for cy in range(ny):
                for cx in range(nx):
                    n = l * N + cy * nx + cx
                    h2 = k // 2
                    if cx < nx - 1:       # east edge: k horizontal lanes from centre to centre
                        lanes = []
                        for j in range(k):
                            row = (cy * k + j) * RW
                            lanes.append(ras[row + cx * k + h2:row + (cx + 1) * k + h2])
                        self._edge_cap(2 * n, lanes)
                    if cy < ny - 1:       # south edge: k vertical lanes (strided column slices)
                        lanes = []
                        for j in range(k):
                            st = (cy * k + h2) * RW + cx * k + j
                            lanes.append(ras[st:st + k * RW:RW])
                        self._edge_cap(2 * n + 1, lanes)
        # via sites
        vr = self.rl.via_dia / 2
        ex = max(0.0, vr - self.rl.track_width / 2)
        rp = int(math.ceil(ex / self.res - 1e-9))
        disc = [(dx, dy) for dx in range(-rp, rp + 1) for dy in range(-rp, rp + 1) if (dx * dx + dy * dy) * self.res ** 2 <= ex * ex + 1e-9]
        vp = self.rl.via_dia + self.rl.clearance
        sub = max(1, int(round(self.s / vp)))
        W, H = self.RW, self.RH
        hard = self.hard
        for cy in range(ny):
            for cx in range(nx):
                c = cy * nx + cx
                caps_all = 0
                per_net = defaultdict(int)
                for sj in range(sub):
                    for si in range(sub):
                        sfree, owners = False, set()
                        # candidate points inside the sub-cell (centre first, then 3x3)
                        x0, y0 = cx * k + si * k // sub, cy * k + sj * k // sub
                        x1, y1 = max(x0 + 1, cx * k + (si + 1) * k // sub), max(y0 + 1, cy * k + (sj + 1) * k // sub)
                        cands = [((x0 + x1) // 2, (y0 + y1) // 2)] + [(x0 + (x1 - x0) * a // 3, y0 + (y1 - y0) * bq // 3)
                                                                       for a in range(3) for bq in range(3)]
                        for (px, py) in cands:
                            ok, own = self._via_site(px, py, disc, hard, W, H)
                            if ok and not own:
                                sfree = True
                                break
                            if ok and len(own) == 1:
                                owners |= own
                        if sfree:
                            caps_all += 1
                        else:
                            for o in owners:
                                per_net[o] += 1
                for l in range(self.L - 1):
                    r = 2 * LN + l * N + c
                    self.cap[r] = caps_all
                    if per_net:
                        self.ovr[r] = {o: caps_all + v for o, v in per_net.items()}
        self._log("capacities in %.1fs" % (time.time() - t0))

    def _via_site(self, px, py, disc, hard, W, H):
        """(ok, owners): ok False if hard-blocked or any foreign static; owners = nets whose copper is in the disc."""
        if not (0 <= px < W and 0 <= py < H) or hard[py * W + px]:
            return False, None
        own = set()
        for l in range(self.L):
            ras = self.ras[l]
            for dx, dy in disc:
                x, y = px + dx, py + dy
                if 0 <= x < W and 0 <= y < H:
                    v = ras[y * W + x]
                    if v:
                        if v < 0:
                            return False, None
                        own.add(v)
        return True, own

    def _edge_cap(self, e, lanes):
        """lanes: k array slices (one per transverse lane position) along the edge's centre-to-centre run.
        A lane is free if no foreign copper touches it; owned by net o if only net o's copper is there."""
        lv = []
        any_own = False
        for sl in lanes:
            if sl.count(0) == len(sl):
                lv.append(0)
            else:
                st = set(sl)
                st.discard(0)
                if len(st) == 1 and -1 not in st:
                    lv.append(next(iter(st)))
                    any_own = True
                else:
                    lv.append(-1)
        c = self._runs_cap([v == 0 for v in lv])
        self.cap[e] = c
        if any_own:
            d = {}
            for o in {v for v in lv if v > 0}:
                co = self._runs_cap([v == 0 or v == o for v in lv])
                if co != c:
                    d[o] = co
            if d:
                self.ovr[e] = d

    # --- terminals -------------------------------------------------------------------------
    def _cells_in_bbox(self, bb):
        x0, y0 = self.cell_of((bb[0], bb[1]))
        x1, y1 = self.cell_of((bb[2], bb[3]))
        return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]

    def _island_nodes(self, net, isl):
        b = self.b
        nodes = set()

        def add(cells, lays):
            for (x, y) in cells:
                for l in lays:
                    nodes.add(l * self.N + y * self.nx + x)
        for pid in isl.pads:
            ref, num = pid.rsplit(".", 1)
            for pd in b.pad(ref, num):
                lays = [self.lidx[l] for l in pd.layers if l in self.lidx] or list(range(self.L))
                if pd.through:
                    lays = list(range(self.L))
                hx, hy = pd.size[0] / 2, pd.size[1] / 2
                a = math.radians(pd.rot)
                ex = abs(hx * math.cos(a)) + abs(hy * math.sin(a))
                ey = abs(hx * math.sin(a)) + abs(hy * math.cos(a))
                add(self._cells_in_bbox((pd.pos[0] - ex, pd.pos[1] - ey, pd.pos[0] + ex, pd.pos[1] + ey)), lays)
        for (p, q) in isl.segs:
            lay = self._track_layer.get(net, {}).get((p, q))
            lays = [self.lidx[lay]] if lay in self.lidx else list(range(self.L))
            n = max(1, int(geom.dist(p, q) / (self.s * 0.5)) + 1)
            cells = {self.cell_of((p[0] + (q[0] - p[0]) * i / n, p[1] + (q[1] - p[1]) * i / n)) for i in range(n + 1)}
            add(cells, lays)
        padpos = {pd.pos for pid in isl.pads for pd in b.pad(*pid.rsplit(".", 1))}
        for v in self._vias_by_net.get(net, []):
            if tuple(v.pos) in {tuple(p) for p in isl.pts} and tuple(v.pos) not in padpos:
                add([self.cell_of(v.pos)], range(self.L))
        return nodes

    def _terminals(self):
        b = self.b
        self._track_layer = defaultdict(dict)
        for t in b.tracks:
            self._track_layer[t.net][(t.a, t.b)] = t.layer
        self._vias_by_net = defaultdict(list)
        for v in b.vias:
            self._vias_by_net[v.net].append(v)
        cache = {}
        self.src, self.dst = [], []
        self.cnet = []
        for c in self.conns:
            ids = []
            for ii in (c.ia, c.ib):
                key = (c.net, ii)
                if key not in cache:
                    cache[key] = self._island_nodes(c.net, self.rn.islands[c.net][ii])
                ids.append(set(cache[key]))
            for side, p in ((0, c.pa), (1, c.pb)):
                if not ids[side]:
                    cx, cy = self.cell_of(p)
                    ids[side] = {l * self.N + cy * self.nx + cx for l in range(self.L)}
            self.src.append(ids[0])
            self.dst.append(ids[1])
            self.cnet.append(self.nid.get(c.net, -1))
        per_net = defaultdict(int)
        self.cname = []
        tot = defaultdict(int)
        for c in self.conns:
            tot[c.net] += 1
        for c in self.conns:
            per_net[c.net] += 1
            self.cname.append(c.net if tot[c.net] == 1 else "%s#%d" % (c.net, per_net[c.net]))

    def _relax_escapes(self):
        """Pin escapes: cells within 1 of a terminal get cap >= esc_cap on that layer; via cap >= 1."""
        nx, ny, N, LN = self.nx, self.ny, self.N, self.LN
        done = set()
        self.escape = set()
        for ci in range(len(self.conns)):
            for nodes in (self.src[ci], self.dst[ci]):
                for n in nodes:
                    if n in done:
                        continue
                    done.add(n)
                    l, x, y = self.node_xy(n)
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            xx, yy = x + dx, y + dy
                            if not (0 <= xx < nx and 0 <= yy < ny):
                                continue
                            m = l * N + yy * nx + xx
                            self.escape.add(m)
                            for e in (2 * m, 2 * m + 1, 2 * (m - 1) if xx > 0 else None, 2 * (m - nx) + 1 if yy > 0 else None):
                                if e is None:
                                    continue
                                if xx == nx - 1 and e == 2 * m:
                                    continue
                                if yy == ny - 1 and e == 2 * m + 1:
                                    continue
                                if self.cap[e] < self.esc_cap:
                                    self.cap[e] = self.esc_cap
                                if e in self.ovr:
                                    self.ovr[e] = {o: max(v, self.esc_cap) for o, v in self.ovr[e].items()}
                            for l2 in range(self.L - 1):
                                r = 2 * LN + l2 * N + yy * nx + xx
                                if self.cap[r] < 1:
                                    self.cap[r] = 1

    # --- state ------------------------------------------------------------------------------
    def _init_state(self):
        R = 3 * self.LN
        self.occ = [0] * R
        self.hist = [0.0] * R
        self.users = {}                              # resource -> set(nid) (only while used)
        self.netres = defaultdict(dict)              # nid -> {resource: refcount}
        self.sH = [0] * self.LN                      # nets going straight E-W through the cell (per layer)
        self.sV = [0] * self.LN                      # nets going straight N-S
        self.sT = [0] * self.LN                      # nets turning (corner) in the cell
        self.sx = [0] * self.LN                      # same-net kind pairs (not conflicts)
        self.nsH = defaultdict(dict)
        self.nsV = defaultdict(dict)
        self.nsT = defaultdict(dict)
        self.xhist = [0.0] * self.LN
        self.touched = set()
        self.contend = defaultdict(set)              # resource -> nets that ever overused it
        self.routes = [None] * len(self.conns)
        self.ovr_by_net = defaultdict(dict)
        for r, d in self.ovr.items():
            for o, cp in d.items():
                self.ovr_by_net[o][r] = cp
        self.lmult = defaultdict(lambda: [1.0] * self.L)
        self.disc = defaultdict(dict)
        self.prio = defaultdict(int)
        self.frozen = {}
        self.rips = defaultdict(int)                 # conn -> times ripped
        self.fights = defaultdict(lambda: defaultdict(int))   # conn -> other net name -> count
        self.pf = self.pf0
        self.curve = []

    # --- hints -------------------------------------------------------------------------------
    def _handle_resources(self, h):
        """All planar resources (all layers) belonging to cut/region handle h."""
        nm = self.names
        out = set()
        if nm.T and h in nm.T.cuts:
            c = nm.T.cuts[h]
            bb = (min(c.p0[0], c.p1[0]) - self.s, min(c.p0[1], c.p1[1]) - self.s,
                  max(c.p0[0], c.p1[0]) + self.s, max(c.p0[1], c.p1[1]) + self.s)
            cells = self._cells_in_bbox(bb)
            for (x, y) in cells:
                for ax in (0, 1):
                    x2, y2 = (x + 1, y) if ax == 0 else (x, y + 1)
                    if x2 >= self.nx or y2 >= self.ny:
                        continue
                    if geom.seg_intersect(self.center(x, y), self.center(x2, y2), c.p0, c.p1):
                        for l in range(self.L):
                            out.add(2 * (l * self.N + y * self.nx + x) + ax)
        else:
            bb = nm.region_bbox(h)
            if bb:
                for (x, y) in self._cells_in_bbox(bb):
                    for ax in (0, 1):
                        for l in range(self.L):
                            out.add(2 * (l * self.N + y * self.nx + x) + ax)
        return out

    def _apply_hints(self):
        nets = self.b.nets
        for h in self.hints:
            t = h.get("type")
            try:
                if t == "layer":
                    for n in _glob_nets(h["net"], nets):
                        m = [float(h.get("strength", 3.0))] * self.L
                        m[self.lidx[h["layer"]]] = 1.0
                        self.lmult[self.nid[n]] = m
                elif t == "corridor":
                    res = set()
                    for hd in h.get("via", []):
                        res |= self._handle_resources(hd)
                    if not res:
                        self.hint_notes.append("corridor %s: no resources" % h)
                    for n in _glob_nets(h["net"], nets):
                        for r in res:
                            self.disc[self.nid[n]][r] = 0.35
                elif t == "priority":
                    for n in _glob_nets(h["net"], nets):
                        self.prio[self.nid[n]] = int(h.get("level", 1))
                elif t == "freeze":
                    cells = None
                    if "region" in h:
                        bb = self.names.region_bbox(h["region"])
                    else:
                        bb = h.get("bbox")
                    if not bb:
                        self.hint_notes.append("freeze: unknown region/bbox in %s" % h)
                        continue
                    lays = [self.lidx[l] for l in h.get("layers", self.layers)]
                    owner = self.nid.get(h.get("net")) if h.get("net") else None
                    for (x, y) in self._cells_in_bbox(bb):
                        for l in lays:
                            self.frozen[l * self.N + y * self.nx + x] = owner
                else:
                    self.hint_notes.append("unknown hint type: %s" % h)
            except Exception as ex:
                self.hint_notes.append("hint %s failed: %r" % (h, ex))
        # frozen (unowned) cells: no capacity for anyone, so cut/cluster capacities in explanations show it
        for n, owner in self.frozen.items():
            if owner is not None:
                continue
            l, x, y = self.node_xy(n)
            m = n
            for e in (2 * m, 2 * m + 1, 2 * (m - 1) if x > 0 else None, 2 * (m - self.nx) + 1 if y > 0 else None):
                if e is not None:
                    self.cap[e] = 0
                    self.ovr.pop(e, None)
                    for d in self.ovr_by_net.values():
                        d.pop(e, None)
        self.hmin = min([1.0] + [0.35 for d in self.disc.values() if d])

    # --- A* ----------------------------------------------------------------------------------
    def _search(self, ci, mode="cost", block=None, margin_cells=None):
        """Returns (cost, [node ids]) or None.  mode 'cost': negotiated cost; 'legal': no overuse allowed,
        pure length+via cost; 'soft': like cost but with `block` resources forbidden."""
        nx, ny, N, LN, L, s = self.nx, self.ny, self.N, self.LN, self.L, self.s
        N_id = self.cnet[ci]
        src, dst = self.src[ci], self.dst[ci]
        common = src & dst
        if common:
            return 0.0, [min(common)]
        legal = mode == "legal"
        cap, occ, hist, users = self.cap, self.occ, self.hist, self.users
        mine = self.netres[N_id]
        ovr = self.ovr_by_net.get(N_id) or {}
        lm = self.lmult[N_id] if N_id in self.lmult else None
        disc = self.disc.get(N_id) or {}
        pf = self.pf / (1 + self.prio.get(N_id, 0))
        sV, sH, sT, nsV, nsH, nsT, xhist = self.sV, self.sH, self.sT, self.nsV[N_id], self.nsH[N_id], self.nsT[N_id], self.xhist
        frozen = self.frozen
        bendc = self.bend_c * s
        viac = self.via_cost + self.net_viapen.get(N_id, 0.0) if self.net_viapen else self.via_cost
        lenw = self.net_lenw.get(N_id, 1.0) if self.net_lenw else 1.0
        near = self._avoid_near(N_id) if self.net_avoid else None
        hmin = getattr(self, "hmin", 1.0)
        if lm:
            hmin = min(hmin, min(lm))
        # window
        pts = [self.node_xy(n)[1:] for n in src] + [self.node_xy(n)[1:] for n in dst]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        sx0, sx1, sy0, sy1 = min(xs), max(xs), min(ys), max(ys)
        dxs = [self.node_xy(n)[1:] for n in dst]
        gx0, gx1, gy0, gy1 = min(p[0] for p in dxs), max(p[0] for p in dxs), min(p[1] for p in dxs), max(p[1] for p in dxs)
        if margin_cells is None:
            margin_cells = int(12 / s) + int(0.25 * max(sx1 - sx0, sy1 - sy0))
        for attempt in (0, 1):
            m = margin_cells if attempt == 0 else max(nx, ny)
            xlo, xhi, ylo, yhi = max(0, sx0 - m), min(nx - 1, sx1 + m), max(0, sy0 - m), min(ny - 1, sy1 + m)
            res = self._astar(N_id, src, dst, xlo, xhi, ylo, yhi, gx0, gx1, gy0, gy1, legal, block, cap, occ, hist, users,
                              mine, ovr, lm, disc, pf, sV, sH, sT, nsV, nsH, nsT, xhist, frozen, bendc, viac, hmin, lenw, near)
            if res or (xlo == 0 and xhi == nx - 1 and ylo == 0 and yhi == ny - 1):
                return res
        return None

    def _astar(self, N_id, src, dst, xlo, xhi, ylo, yhi, gx0, gx1, gy0, gy1, legal, block, cap, occ, hist, users,
               mine, ovr, lm, disc, pf, sV, sH, sT, nsV, nsH, nsT, xhist, frozen, bendc, viac, hmin, lenw=1.0, near=None):
        nx, N, LN, L, s = self.nx, self.N, self.LN, self.L, self.s
        heappush, heappop = heapq.heappush, heapq.heappop
        best = {}
        par = {}
        heap = []
        hs = s * hmin

        def hfun(x, y):
            dx = gx0 - x if x < gx0 else (x - gx1 if x > gx1 else 0)
            dy = gy0 - y if y < gy0 else (y - gy1 if y > gy1 else 0)
            return (dx + dy) * hs
        for n in src:
            l, r = divmod(n, N)
            y, x = divmod(r, nx)
            if not (xlo <= x <= xhi and ylo <= y <= yhi):
                continue
            best[n * 3 + 2] = 0.0
            heap.append((hfun(x, y), 0.0, n, 2))
        heapq.heapify(heap)
        dirs = ((1, 0, 0), (-1, 0, 0), (0, 1, 1), (0, -1, 1))
        lmv = lm or [1.0] * L
        goal = None
        while heap:
            f, g, u, a = heappop(heap)
            key = u * 3 + a
            if g > best.get(key, INF):
                continue
            if u in dst:
                goal = key
                break
            l, rem = divmod(u, N)
            y, x = divmod(rem, nx)
            base_l = s * lmv[l] * lenw if lenw != 1.0 else s * lmv[l]
            for dx, dy, ax in dirs:
                xx, yy = x + dx, y + dy
                if xx < xlo or xx > xhi or yy < ylo or yy > yhi:
                    continue
                v = u + dx + dy * nx
                if frozen and v in frozen and frozen[v] != N_id:
                    continue
                e = 2 * u + ax if (dx > 0 or dy > 0) else 2 * v + ax
                if block and e in block:
                    continue
                cp = ovr.get(e, cap[e]) if ovr else cap[e]
                oc = len(users[e]) if e in users else 0
                if oc and N_id in users[e]:
                    oc -= 1
                over = oc + 1 - cp if cp else 4 * (oc + 1)     # capacity-0 (hard obstacle) overuse counts x4
                if legal:
                    if over > 0:
                        continue
                    cost = base_l
                else:
                    cost = base_l * (disc.get(e, 1.0) if disc else 1.0) + hist[e]
                    if over > 0:
                        cost *= 1 + pf * over
                if a != 2:
                    # conflicts inside cell u with other nets: straight H x (straight V | turn), straight V x turn
                    if a == ax:
                        if ax == 0:
                            xo = sV[u] - (1 if u in nsV else 0) + sT[u] - (1 if u in nsT else 0)
                        else:
                            xo = sH[u] - (1 if u in nsH else 0) + sT[u] - (1 if u in nsT else 0)
                    else:
                        xo = sH[u] - (1 if u in nsH else 0) + sV[u] - (1 if u in nsV else 0)
                        cost += bendc
                    if xo > 0:
                        if legal:
                            continue
                        cost += (s + xhist[u]) * pf * xo
                if near and not legal and v in near:
                    cost += self.AVOID_COST * s
                g2 = g + cost
                k2 = v * 3 + ax
                if g2 < best.get(k2, INF):
                    best[k2] = g2
                    par[k2] = key
                    heappush(heap, (g2 + hfun(xx, yy), g2, v, ax))
            # vias
            for l2 in (l - 1, l + 1):
                if l2 < 0 or l2 >= L:
                    continue
                r = 2 * LN + min(l, l2) * N + rem
                v = l2 * N + rem
                if frozen and v in frozen and frozen[v] != N_id:
                    continue
                if block and r in block:
                    continue
                cp = ovr.get(r, cap[r]) if ovr else cap[r]
                oc = len(users[r]) if r in users else 0
                if oc and N_id in users[r]:
                    oc -= 1
                over = oc + 1 - cp if cp else 4 * (oc + 1)
                if legal:
                    if over > 0:
                        continue
                    cost = viac
                else:
                    cost = viac + hist[r]
                    if over > 0:
                        cost *= 1 + pf * over
                g2 = g + cost
                k2 = v * 3 + 2
                if g2 < best.get(k2, INF):
                    best[k2] = g2
                    par[k2] = key
                    heappush(heap, (g2 + hfun(x, y), g2, v, 2))
        if goal is None:
            return None
        path = []
        k = goal
        cost = best[goal]
        while True:
            path.append(k // 3)
            if k not in par:
                break
            k = par[k]
        path.reverse()
        return cost, path

    # --- usage bookkeeping ----------------------------------------------------------------------
    def _decode(self, nodes):
        """nodes -> (resources list, straights [(node, axis)], vias [(cell_index, l0, l1)])."""
        N, nx, LN = self.N, self.nx, self.LN
        res, strs = [], []
        for i in range(len(nodes) - 1):
            u, v = nodes[i], nodes[i + 1]
            lu, lv = u // N, v // N
            if lu != lv:
                res.append(2 * LN + min(lu, lv) * N + u % N)
            else:
                d = v - u
                if d == 1:
                    res.append(2 * u)
                elif d == -1:
                    res.append(2 * v)
                elif d == nx:
                    res.append(2 * u + 1)
                else:
                    res.append(2 * v + 1)
        for i in range(1, len(nodes) - 1):
            u, w, v = nodes[i - 1], nodes[i], nodes[i + 1]
            if u // N == w // N == v // N:
                a1 = 0 if abs(w - u) == 1 else 1
                a2 = 0 if abs(v - w) == 1 else 1
                strs.append((w, a1 if a1 == a2 else 2))
        return res, strs

    def _kind(self, nid, k):
        if k == 0:
            return self.nsH[nid], self.sH
        if k == 1:
            return self.nsV[nid], self.sV
        return self.nsT[nid], self.sT

    def _xover(self, w):
        h, v, t = self.sH[w], self.sV[w], self.sT[w]
        return h * v + (h + v) * t - self.sx[w]

    def _selfp(self, nid, w):
        a, b, c = w in self.nsH[nid], w in self.nsV[nid], w in self.nsT[nid]
        return (a and b) + (a and c) + (b and c)

    def _add(self, ci, nodes):
        nid = self.cnet[ci]
        res, strs = self._decode(nodes)
        mine = self.netres[nid]
        for r in res:
            c = mine.get(r, 0)
            mine[r] = c + 1
            if c == 0:
                self.occ[r] += 1
                self.users.setdefault(r, set()).add(nid)
                self.touched.add(r)
        for (w, ax) in strs:
            d, arr = self._kind(nid, ax)
            c = d.get(w, 0)
            if c == 0:
                self.sx[w] -= self._selfp(nid, w)
            d[w] = c + 1
            if c == 0:
                arr[w] += 1
                self.sx[w] += self._selfp(nid, w)
        self.routes[ci] = {"nodes": nodes, "res": res, "strs": strs}

    def _rip(self, ci):
        rt = self.routes[ci]
        if not rt:
            return
        nid = self.cnet[ci]
        mine = self.netres[nid]
        for r in rt["res"]:
            c = mine[r] - 1
            if c:
                mine[r] = c
            else:
                del mine[r]
                self.occ[r] -= 1
                u = self.users[r]
                u.discard(nid)
                if not u:
                    del self.users[r]
        for (w, ax) in rt["strs"]:
            d, arr = self._kind(nid, ax)
            c = d[w] - 1
            if c:
                d[w] = c
            else:
                self.sx[w] -= self._selfp(nid, w)
                del d[w]
                arr[w] -= 1
                self.sx[w] += self._selfp(nid, w)
        self.routes[ci] = None

    def rebuild(self, routes):
        """Reset usage and re-install the given routes (list of route dicts / None)."""
        keep_hist = (self.hist, self.xhist)
        for ci in range(len(self.conns)):
            self._rip(ci)
        for ci, rt in enumerate(routes):
            if rt:
                self._add(ci, rt["nodes"])

    # --- overflow ----------------------------------------------------------------------------
    def cap_eff(self, r):
        c = self.cap[r]
        d = self.ovr.get(r)
        if d and r in self.users:
            for o in self.users[r]:
                if o in d and d[o] > c:
                    c = d[o]
        return c

    def overuse(self):
        """-> ({resource: over}, {node: xover}) for the current state."""
        ov = {}
        for r in list(self.touched):
            u = self.users.get(r)
            if not u:
                self.touched.discard(r)
                continue
            o = len(u) - self.cap_eff(r)
            if o > 0:
                ov[r] = o
        xv = {}
        for ci_rt in self.routes:
            if not ci_rt:
                continue
            for (w, ax) in ci_rt["strs"]:
                if w not in xv:
                    x = self._xover(w)
                    if x > 0:
                        xv[w] = x
        return ov, xv

    def conn_over(self, ci, ov, xv):
        rt = self.routes[ci]
        if not rt:
            return True
        return any(r in ov for r in rt["res"]) or any(w in xv for (w, _) in rt["strs"])

    # --- main loop -----------------------------------------------------------------------------
    def run(self, iters=30, tlimit=30.0, verbose=False, stall=8):
        t0 = time.time()
        self.setup_s = t0 - self.t0
        deadline = t0 + tlimit
        n = len(self.conns)
        order0 = sorted(range(n), key=lambda i: (-self.prio.get(self.cnet[i], 0),
                                                 geom.dist(self.conns[i].pa, self.conns[i].pb)))
        best = None
        self.failed_nopath = set()
        stop = False
        for it in range(iters + 1):
            ts = time.time()
            if it == 0:
                todo = order0
                ov, xv = {}, {}
            else:
                ov, xv = self.overuse()
                todo = [ci for ci in order0 if ci not in self.failed_nopath and self.conn_over(ci, ov, xv)]
                # most overused first
                todo.sort(key=lambda ci: -sum(ov.get(r, 0) for r in self.routes[ci]["res"]) if self.routes[ci] else 0)
                if not todo:
                    break
            nr = 0
            for ci in todo:
                if time.time() > deadline:
                    stop = True
                    break
                if it > 0:
                    ov, xv = self._ov_local(ci)
                    if self.routes[ci] and not (any(r in ov for r in self.routes[ci]["res"]) or any(w in xv for (w, _) in self.routes[ci]["strs"])):
                        continue
                    self._record_rip(ci, ov, xv)
                self._rip(ci)
                res = self._search(ci)
                nr += 1
                if res is None:
                    self.failed_nopath.add(ci)
                    continue
                self.failed_nopath.discard(ci)
                self._add(ci, res[1])
            ov, xv = self.overuse()
            tot = sum(ov.values()) + sum(xv.values())
            cur = (tot, len(ov) + len(xv))
            nconn_over = sum(1 for ci in range(n) if self.conn_over(ci, ov, xv))
            wl = sum(len(r["res"]) for r in self.routes if r) * self.s
            self.curve.append({"iter": it, "overflow": tot, "over_resources": len(ov) + len(xv), "conns_over": nconn_over,
                               "rerouted": nr, "pf": round(self.pf, 3), "t": round(time.time() - t0, 2),
                               "dt": round(time.time() - ts, 2)})
            if verbose:
                print("  it %2d overflow %4d (resources %3d) conns %3d rerouted %3d pf %.2f %.1fs"
                      % (it, tot, len(ov) + len(xv), nconn_over, nr, self.pf, time.time() - t0), file=sys.stderr)
            if best is None or (tot, wl) < (best[0], best[1]):
                best = (tot, wl, list(self.routes), it)
            if tot == 0 or stop:
                break
            if it - best[3] >= stall:
                self.stalled = True
                break
            # history + penalty
            for r, o in ov.items():
                self.hist[r] += self.hist_inc * self.s * o
                self.contend[r] |= set(self.users[r])
            for w, x in xv.items():
                self.xhist[w] += self.hist_inc * self.s * x
            self.pf = min(self.pf * self.pf_grow, 60.0)
        if best and best[2] != self.routes:
            self.rebuild(best[2])
        self.best_iter = best[3] if best else 0
        self.elapsed = time.time() - t0
        return self

    def _ov_local(self, ci):
        """Overuse restricted to resources/straights of this conn (current state)."""
        rt = self.routes[ci]
        ov, xv = {}, {}
        if not rt:
            return ov, xv
        for r in rt["res"]:
            u = self.users.get(r)
            if u:
                o = len(u) - self.cap_eff(r)
                if o > 0:
                    ov[r] = o
        for (w, _) in rt["strs"]:
            x = self._xover(w)
            if x > 0:
                xv[w] = x
        return ov, xv

    def _record_rip(self, ci, ov, xv):
        self.rips[ci] += 1
        me = self.cnet[ci]
        others = set()
        for r in ov:
            others |= set(self.users.get(r, ()))
        for w in xv:
            l, x, y = self.node_xy(w)
            for cj, rt in enumerate(self.routes):
                if rt and cj != ci and self.cnet[cj] != me and any(w == q for q, _ in rt["strs"]):
                    others.add(self.cnet[cj])
        others.discard(me)
        for o in others:
            self.fights[ci][self.nname.get(o, "?")] += 1

    # ------------------------------------------------------------------------------------------
    # results / explanations
    # ------------------------------------------------------------------------------------------
    def stats(self):
        ov, xv = self.overuse()
        over_conn = [ci for ci in range(len(self.conns)) if self.conn_over(ci, ov, xv)]
        n_np = sum(1 for ci in over_conn if not self.routes[ci])
        vias = 0
        wl = 0.0
        for rt in self.routes:
            if not rt:
                continue
            for r in rt["res"]:
                if r >= 2 * self.LN:
                    vias += 1
                else:
                    wl += self.s
        allv = list(ov.values()) + list(xv.values())
        return {"connections": len(self.conns), "overflowing": len(over_conn) - n_np, "routed_ok": len(self.conns) - len(over_conn),
                "total_overflow": sum(allv), "max_overflow": max(allv) if allv else 0, "vias": vias,
                "wirelength_mm": round(wl, 1), "iterations": len(self.curve) - 1, "best_iter": self.best_iter,
                "time_s": round(self.elapsed + self.setup_s, 2), "search_s": round(self.elapsed, 2), "setup_s": round(self.setup_s, 2), "over_resources": len(allv), "no_path": len(self.failed_nopath)}, ov, xv, over_conn

    def handle_of(self, r):
        if r >= 2 * self.LN:
            _, l, a, _ = self.res_info(r)
            p = self.center(*a)
            return self.names.ring_of(p) or self.names.region(p)
        a, b = self.res_pts(r)
        return self.names.handle(a, b)

    def near_pads(self, pt, rad=1.2, n=3):
        if not hasattr(self, "_padbin"):
            self._padbin = defaultdict(list)
            for pd in self.b.pads():
                if pd.layers:
                    self._padbin[(int(pd.pos[0] // 2), int(pd.pos[1] // 2))].append(pd)
        cx, cy = int(pt[0] // 2), int(pt[1] // 2)
        c = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for pd in self._padbin.get((cx + dx, cy + dy), ()):
                    d = geom.dist(pt, pd.pos)
                    if d <= rad:
                        c.append((d, pd.id))
        out = []
        for _, i in sorted(c):
            if i not in out:
                out.append(i)
        return out[:n]

    def res_where(self, r):
        kind, l, a, b = self.res_info(r)
        p = self.center(*a)
        return "%s%s@(%.0f,%.0f)" % ("via" if kind == "V" else "", self.layers[l] if kind != "V" else "", p[0], p[1])

    def cut_resources(self, h):
        cache = getattr(self, "_hres", None)
        if cache is None:
            cache = self._hres = {}
        if h not in cache:
            cache[h] = self._handle_resources(h)
        return cache[h]

    def why_blocked(self, r):
        """Who/what makes resource r (planar edge) have low capacity: nets whose copper is on the boundary."""
        kind, l, a, b = self.res_info(r)
        if kind != "E":
            return set(), False
        ras, k, RW = self.ras[l], self.k, self.RW
        x, y = a
        h2 = k // 2
        vals = []
        if b[0] > a[0]:
            for j in range(k):
                row = (y * k + j) * RW
                vals += ras[row + x * k + h2:row + (x + 1) * k + h2].tolist()
        else:
            for j in range(k):
                st = (y * k + h2) * RW + x * k + j
                vals += ras[st:st + k * RW:RW].tolist()
        return {self.nname.get(v, "?") for v in vals if v > 0}, any(v < 0 for v in vals)

    # ---- explanations
    def path_summary(self, ci, nodes):
        """Cuts crossed (ordered), length mm, vias."""
        res, _ = self._decode(nodes)
        cuts, vias, ln = [], 0, 0.0
        for r in res:
            if r >= 2 * self.LN:
                vias += 1
                continue
            ln += self.s
            a, b = self.res_pts(r)
            for c in self.names.cuts_crossed(a, b):
                if c not in cuts:
                    cuts.append(c)
        return {"cuts": cuts, "len": ln, "vias": vias, "res": res}

    def explain_conn(self, ci, ov, xv, with_alt=True):
        rt = self.routes[ci]
        name = self.cname[ci]
        c = self.conns[ci]
        out = {"conn": name, "net": c.net, "pads": "%s>%s" % (c.a, c.b), "idx": ci}
        if not rt:
            out["status"] = "NOPATH"
            out["line"] = "%s NO PATH: %s" % (name, self._why_nopath(ci))
            return out
        cl = self._clusters(ov, xv)
        mine = defaultdict(int)
        for r in rt["res"]:
            if ("R", r) in cl["of"]:
                mine[cl["of"][("R", r)]] += ov[r]
        for (w, _) in rt["strs"]:
            if ("X", w) in cl["of"]:
                mine[cl["of"][("X", w)]] += xv[w]
        if not mine:
            out["status"] = "OK"
            return out
        top = sorted(mine.items(), key=lambda kv: -kv[1])
        K = cl["list"][top[0][0]]
        h = K["handle"]
        if K["kind"] == "X":
            nodes = K["nodes"]
            l, x, y = self.node_xy(nodes[0])
            p = self.center(x, y)
            crossers = set()
            for cj, rj in enumerate(self.routes):
                if rj and cj != ci and self.cnet[cj] != self.cnet[ci]:
                    if any(w in nodes for (w, _) in rj["strs"]):
                        crossers.add(self.cname[cj])
            need = "needs a layer change near (%.0f,%.0f) %s: straight crossing on %s with [%s]" % (
                p[0], p[1], K["handle"], self.layers[l], ",".join(sorted(crossers)))
            out["bottleneck"] = {"handle": K["handle"], "kind": "crossing"}
        else:
            rs = K["res"]
            cap, dem = K["cap"], K["demand"]
            nets = K["nets"]
            ecap = sum(self.cap_eff(r) for r in rs)
            edem = sum(len(self.users.get(r, ())) for r in rs)
            enets = []
            for r in rs:
                for o in sorted(self.users.get(r, ()), key=lambda o: self.nname.get(o, "")):
                    nm = self.nname.get(o, "?")
                    if nm not in enets:
                        enets.append(nm)
            sect_over = dem > cap
            if not sect_over:
                # the congested edges overflow although the whole cross-section has room: lane-level squeeze
                sec_note = " (section total cap %d demand %d: slack exists beside it)" % (cap, dem)
                cap, dem, nets = ecap, edem, enets
            else:
                sec_note = ""
            lays = sorted({self.layers[self.res_info(r)[1]] for r in rs if r < 2 * self.LN}) or ["via"]
            lab = self.names.cut_label(h)
            where = h + (" (%s)" % lab if lab else "")
            if K["also"]:
                where += "+" + "+".join(K["also"])
            wp = K["where"]
            where += " @(%.0f,%.0f)" % wp
            npd = self.near_pads(wp)
            if npd:
                where += " near " + "/".join(npd)
            via_part = " via-site" if any(r >= 2 * self.LN for r in rs) else ""
            need = "needs %s%s (cap %d, demand %d: [%s]) %s%s" % (where, via_part, cap, dem, ",".join(nets), "/".join(lays), sec_note)
            blk = set()
            st = False
            zero = [r for r in rs if self.cap_eff(r) == 0]
            fz = [r for r in zero if r < 2 * self.LN and (self.res_info(r)[1] * self.N + self.res_info(r)[2][1] * self.nx + self.res_info(r)[2][0] in self.frozen
                                                          or self.res_info(r)[1] * self.N + self.res_info(r)[3][1] * self.nx + self.res_info(r)[3][0] in self.frozen)]
            zero = [r for r in zero if r not in fz]
            for r in zero:
                b_, s_ = self.why_blocked(r)
                blk |= b_
                st = st or s_
            if fz:
                need += " frozen by hint"
            if zero:
                others = blk - set(nets) - {c_.net for c_ in [self.conns[ci]]}
                need += " blocked by " + " + ".join(
                    ([("copper of [" + ",".join(sorted(others)) + "]")] if others else [])
                    + (["overlapping clearances / NPTH / keepout / board edge"] if st or not others else []))
            if h in (self.names.T.cuts if self.names.T else {}):
                allr = [r for r in self.cut_resources(h) if r < 2 * self.LN]
                if allr:
                    capc = sum(self.cap_eff(r) for r in allr)
                    occ = sum(len(self.users.get(r, ())) for r in allr)
                    need += " | whole %s: cap %d used %d" % (h, capc, occ)
            out["bottleneck"] = {"handle": h, "cap": cap, "demand": dem, "nets": nets, "layers": lays}
        if len(top) > 1:
            need += " (+%d more spots: %s)" % (len(top) - 1, ",".join(cl["list"][k]["handle"] for k, _ in top[1:3]))
        cur = self.path_summary(ci, rt["nodes"])
        alts = []
        if with_alt:
            alts = self._alternatives(ci, h, cur, K['kind'] == 'X')
        rip = self.rips.get(ci, 0)
        rp = ""
        if rip:
            fg = sorted(self.fights[ci].items(), key=lambda kv: -kv[1])
            rp = " | ripped %dx" % rip + (" mostly vs %s (%dx)" % fg[0] if fg else "")
        out["status"] = "OVERFLOW"
        out["alts"] = alts
        out["rips"] = rip
        out["line"] = "%s OVERFLOW: %s%s%s" % (name, need, "".join(" | alt: " + a for a in alts) if alts else "", rp)
        return out

    def _clusters(self, ov, xv):
        """Spatial clusters of overflowed resources (planar edges, vias: kind 'R'; straight crossings: kind 'X')."""
        key = (id(ov), len(ov), len(xv))
        if getattr(self, "_clkey", None) == key:
            return self._cl
        items = [("R", r) for r in ov] + [("X", w) for w in xv]
        pos = {}
        for r in ov:
            _, l, a, b = self.res_info(r)
            pos[("R", r)] = a
        for w in xv:
            l, x, y = self.node_xy(w)
            pos[("X", w)] = (x, y)
        rad = max(1, int(math.ceil(1.6 / self.s)))
        par = {it: it for it in items}

        def find(x):
            while par[x] != x:
                par[x] = par[par[x]]
                x = par[x]
            return x
        grid = defaultdict(list)
        for it in items:
            grid[pos[it]].append(it)
        for it in items:
            if it[0] == "X":
                continue
            x, y = pos[it]
            for dx in range(-rad, rad + 1):
                for dy in range(-rad, rad + 1):
                    for jt in grid.get((x + dx, y + dy), ()):
                        if jt[0] == "R":
                            par[find(it)] = find(jt)
        groups = defaultdict(list)
        for it in items:
            groups[find(it)].append(it)
        lst, of = [], {}
        for g in groups.values():
            kind = g[0][0]
            if kind == "X":
                for it in g:
                    K = {"kind": "X", "nodes": [it[1]], "over": xv[it[1]], "handle": "?"}
                    l, x, y = self.node_xy(it[1])
                    K["handle"] = self.names.region(self.center(x, y))
                    of[it] = len(lst)
                    lst.append(K)
                continue
            rs = [it[1] for it in g]
            hc = defaultdict(int)
            for r in rs:
                hc[self.handle_of(r)] += ov[r]
            cuts = sorted([h for h in hc if h.startswith("C")], key=lambda h: -hc[h])
            rest = sorted([h for h in hc if not h.startswith("C")], key=lambda h: -hc[h])
            order = cuts + rest
            nets = []
            for r in rs:
                for o in sorted(self.users.get(r, ()), key=lambda o: self.nname.get(o, "")):
                    nm = self.nname.get(o, "?")
                    if nm not in nets:
                        nets.append(nm)
            worst = max(rs, key=lambda r: ov[r])
            sec = self._section(rs, ov, rad)
            K = {"kind": "R", "res": rs, "over": sum(ov[r] for r in rs), "handle": order[0], "also": order[1:3], "nets": sec["nets"] or nets,
                 "where": self.res_pts(worst)[0], "cap": sec["cap"], "demand": sec["demand"]}
            for it in g:
                of[it] = len(lst)
            lst.append(K)
        self._clkey, self._cl = key, {"list": lst, "of": of}
        return self._cl

    def _section(self, rs, ov, rad):
        """Worst cross-section of an overflow cluster: all edges at the same column (E edges) / row (S edges) /
        via cell, spanning the cluster's transverse extent +-rad, over all layers.  -> cap, demand, nets."""
        groups = defaultdict(list)
        for r in rs:
            kind, l, a, b = self.res_info(r)
            if kind == "V":
                groups[("V", a[0], a[1])].append(r)
            elif b[0] > a[0]:
                groups[("E", a[0])].append(r)
            else:
                groups[("S", a[1])].append(r)
        best = None
        for key, lst in groups.items():
            if key[0] == "V":
                rr = list(lst)
            else:
                pos = [self.res_info(r)[2] for r in lst]
                if key[0] == "E":
                    lo, hi = min(p[1] for p in pos) - rad, max(p[1] for p in pos) + rad
                else:
                    lo, hi = min(p[0] for p in pos) - rad, max(p[0] for p in pos) + rad
                rr = []
                for l in range(self.L):
                    for t in range(max(0, lo), hi + 1):
                        if key[0] == "E":
                            if not (0 <= t < self.ny):
                                continue
                            r = 2 * (l * self.N + t * self.nx + key[1])
                        else:
                            if not (0 <= t < self.nx):
                                continue
                            r = 2 * (l * self.N + key[1] * self.nx + t) + 1
                        if self.cap_eff(r) > 0 or r in self.users:
                            rr.append(r)
            cap = sum(self.cap_eff(r) for r in rr)
            dem = sum(len(self.users.get(r, ())) for r in rr)
            sc = (sum(ov.get(r, 0) for r in lst), dem - cap)
            if best is None or sc > best[0]:
                nets = []
                for r in rr:
                    for o in sorted(self.users.get(r, ()), key=lambda o: self.nname.get(o, "")):
                        nm = self.nname.get(o, "?")
                        if nm not in nets:
                            nets.append(nm)
                best = (sc, {"cap": cap, "demand": dem, "nets": nets})
        return best[1]

    def _why_nopath(self, ci):
        fz = [n for n in (self.src[ci] | self.dst[ci]) if n in self.frozen]
        if fz and len(fz) == len(self.src[ci] | self.dst[ci]):
            l, x, y = self.node_xy(fz[0])
            return "every terminal cell is inside a hint freeze (e.g. %s @(%.0f,%.0f)); unfreeze or move the freeze" % (
                self.layers[l], *self.center(x, y))
        saved, self.frozen = self.frozen, {}
        try:
            r = self._search(ci, mode="soft")
        finally:
            self.frozen = saved
        if r:
            cells = [self.node_xy(n) for n in r[1] if n in saved and saved[n] != self.cnet[ci]]
            if cells:
                l, x, y = cells[0]
                return "freeze hints wall it off (route exists only through frozen cells, first at %s @(%.0f,%.0f))" % (
                    self.layers[l], *self.center(x, y))
        return "search exhausted the grid: terminals enclosed by hard obstacles"

    def _alternatives(self, ci, h, cur, K_is_x=False):
        alts = []
        rt = self.routes[ci]
        saved = rt
        self._rip(ci)
        try:
            lg = self._search(ci, mode="legal")
            if lg:
                s_ = self.path_summary(ci, lg[1])
                alts.append(self._fmt_alt("conflict-free", s_, cur, None))
            else:
                alts.append("no conflict-free route exists without moving others")
            blk = None
            if K_is_x:
                blk = None
            else:
                blk = set(self.cut_resources(h)) if (self.names.T and h in self.names.T.cuts) else None
                if blk is None:
                    # region handle: block overflowed planar resources of the failing path in that region
                    blk = {r for r in saved["res"] if r < 2 * self.LN and self.handle_of(r) == h}
            if blk:
                sf = self._search(ci, mode="soft", block=blk)
                if sf:
                    s_ = self.path_summary(ci, sf[1])
                    over_after = self._would_overflow(ci, sf[1])
                    if not (lg and lg[1] == sf[1]):
                        alts.append(self._fmt_alt("avoiding %s" % h, s_, cur, over_after))
                else:
                    alts.append("avoiding %s: no route" % h)
        finally:
            self._add(ci, saved["nodes"])
        return alts

    def _would_overflow(self, ci, nodes):
        res, _ = self._decode(nodes)
        nid = self.cnet[ci]
        bad = []
        for r in res:
            if r in self.netres[nid]:
                continue
            u = self.users.get(r, set())
            if len(u) + 1 > self.ovr_by_net.get(nid, {}).get(r, self.cap[r]):
                bad.append(r)
        return bad

    def _fmt_alt(self, label, new, cur, bad):
        dl = new["len"] - cur["len"]
        dv = new["vias"] - cur["vias"]
        newcuts = [c for c in new["cuts"] if c not in cur["cuts"]]
        cs = ",".join(newcuts[:4]) + (",+%d" % (len(newcuts) - 4) if len(newcuts) > 4 else "") if newcuts else "same cuts"
        t = "%s via %s %+.0fmm %+d vias" % (label, cs, dl, dv)
        if bad:
            hs = {}
            for r in bad:
                hs.setdefault(self.handle_of(r), []).append(r)
            def who(v):
                nn = sorted({self.nname.get(o, "?") for r in v for o in self.users.get(r, ())})
                return "vs " + ",".join(nn) if nn else "cap 0 there"
            t += " but overflows %s" % ", ".join("%s (%s)" % (k, who(v)) for k, v in list(hs.items())[:3])
        return t

    def hot_spots(self, ov, xv, top=10):
        """Group by handle: current overuse, accumulated history, contenders."""
        H = defaultdict(lambda: {"over": 0, "hist": 0.0, "nets": set(), "n": 0, "cap": 0, "where": None})
        for r in set(list(ov) + [r for r in self.contend]):
            if r >= 2 * self.LN:
                h = "via " + self.handle_of(r)
            else:
                h = self.handle_of(r)
            d = H[h]
            d["over"] += ov.get(r, 0)
            d["hist"] += self.hist[r]
            d["n"] += 1
            d["cap"] += self.cap_eff(r)
            d["nets"] |= {self.nname.get(o, "?") for o in self.contend.get(r, ())}
            d["nets"] |= {self.nname.get(o, "?") for o in self.users.get(r, ()) if ov.get(r)}
            if d["where"] is None or self.hist[r] > d.get("wh", -1):
                d["where"] = self.res_pts(r)[0]
                d["wh"] = self.hist[r]
        for w, x in xv.items():
            l, cx, cy = self.node_xy(w)
            h = "X " + self.names.region(self.center(cx, cy))
            d = H[h]
            d["over"] += x
            d["hist"] += self.xhist[w]
            d["n"] += 1
            for cj, rj in enumerate(self.routes):
                if rj and any(q == w for q, _ in rj["strs"]):
                    d["nets"].add(self.conns[cj].net)
            if d["where"] is None:
                d["where"] = self.center(cx, cy)
        for w in range(self.LN):
            pass
        rows = sorted(H.items(), key=lambda kv: (-(kv[1]["over"] > 0), -kv[1]["hist"]))
        return rows[:top]

    def thrash(self):
        by_net = defaultdict(lambda: {"rips": 0, "conns": 0, "fights": defaultdict(int)})
        for ci, n in self.rips.items():
            net = self.conns[ci].net
            d = by_net[net]
            d["rips"] += n
            d["conns"] += 1
            for o, k in self.fights[ci].items():
                d["fights"][o] += k
        return sorted(by_net.items(), key=lambda kv: -kv[1]["rips"])

    def history_map(self, mm=3.0):
        """ASCII heat map of accumulated history per layer (one char = mm x mm)."""
        bx = self.b.bbox
        cw = int(math.ceil((bx[2] - bx[0]) / mm))
        ch = int(math.ceil((bx[3] - bx[1]) / mm))
        grids = [[[0.0] * cw for _ in range(ch)] for _ in range(self.L)]

        def put(l, p, v):
            i = min(cw - 1, int((p[0] - bx[0]) / mm))
            j = min(ch - 1, int((p[1] - bx[1]) / mm))
            grids[l][j][i] += v
        for r in range(2 * self.LN):
            h = self.hist[r]
            if h > 0:
                _, l, a, b = self.res_info(r)
                put(l, self.center(*a), h)
        for w in range(self.LN):
            h = self.xhist[w]
            if h > 0:
                l, x, y = self.node_xy(w)
                put(l, self.center(x, y), h)
        for r in range(2 * self.LN, 3 * self.LN):
            h = self.hist[r]
            if h > 0:
                _, l, a, _ = self.res_info(r)
                for l2 in range(self.L):
                    put(l2, self.center(*a), h * 0.5)
        mx = max([v for g in grids for row in g for v in row] + [1e-9])
        chars = " .:-=+*#%@"
        parts = [(ref, p.bbox) for ref, p in self.b.parts.items() if p.pads and p.area >= 25.0]
        outs = []
        for l in range(self.L):
            lines = []
            for j in range(ch):
                row = []
                for i in range(cw):
                    v = grids[l][j][i]
                    if v <= 0:
                        c = " "
                    else:
                        c = chars[min(len(chars) - 1, 1 + int((len(chars) - 2) * (v / mx) ** 0.5 + 0.5))]
                    row.append(c)
                lines.append(row)
            for ref, bb in parts:
                i0, i1 = int((bb[0] - bx[0]) / mm), int((bb[2] - bx[0]) / mm)
                j0, j1 = int((bb[1] - bx[1]) / mm), int((bb[3] - bx[1]) / mm)
                for j in range(j0, min(ch, j1 + 1)):
                    for i in range(i0, min(cw, i1 + 1)):
                        if lines[j][i] == " ":
                            lines[j][i] = "="
                lab = (i0 + i1) // 2 - len(ref) // 2
                jj = (j0 + j1) // 2
                for q, chh in enumerate(ref):
                    if 0 <= jj < ch and 0 <= lab + q < cw:
                        lines[jj][lab + q] = chh
            hdr = "".join(str((i * 3 // 10) % 10) if (i * int(mm)) % 30 == 0 else " " for i in range(cw))
            outs.append("history %s (max cell %.1f; ' .:-=+*#%%@' sqrt-scaled; '=' big part; header = x/10mm)\n%s\n%s" % (
                self.layers[l], mx, hdr.rstrip(), "\n".join("".join(r).rstrip() for r in lines)))
        return "\n\n".join(outs)

    # ---- geometry export
    def route_json(self, ci):
        rt = self.routes[ci]
        c = self.conns[ci]
        d = {"idx": ci, "name": self.cname[ci], "net": c.net, "a": c.a, "b": c.b, "pa": [round(v, 3) for v in c.pa],
             "pb": [round(v, 3) for v in c.pb]}
        if not rt:
            d["status"] = "nopath"
            return d
        pts, vias = [], []
        prev = None
        for n in rt["nodes"]:
            l, x, y = self.node_xy(n)
            p = self.center(x, y)
            pts.append([round(p[0], 3), round(p[1], 3), self.layers[l]])
            if prev is not None and prev[0] != l and prev[1:] == (x, y):
                vias.append([round(p[0], 3), round(p[1], 3)])
            prev = (l, x, y)
        # simplified polyline corners
        poly = []
        for i, q in enumerate(pts):
            if 0 < i < len(pts) - 1:
                a, b = pts[i - 1], pts[i + 1]
                if a[2] == q[2] == b[2] and ((q[0] - a[0]) * (b[1] - q[1]) - (q[1] - a[1]) * (b[0] - q[0])) == 0 or (
                        a[2] == q[2] == b[2] and abs((q[0] - a[0]) * (b[1] - q[1]) - (q[1] - a[1]) * (b[0] - q[0])) < 1e-6):
                    continue
            poly.append(q)
        res, _ = self._decode(rt["nodes"])
        cuts = []
        for r in res:
            if r < 2 * self.LN:
                a, b = self.res_pts(r)
                for cc in self.names.cuts_crossed(a, b):
                    if cc not in cuts:
                        cuts.append(cc)
        ov, xv = self.overuse() if not hasattr(self, "_ovc") else self._ovc
        d.update({"status": "overflow" if (any(r in ov for r in rt["res"]) or any(w in xv for (w, _) in rt["strs"])) else "ok",
                  "points": pts, "corners": poly, "vias": vias, "cuts": cuts,
                  "length_mm": round(sum(self.s for r in res if r < 2 * self.LN), 2), "n_vias": len(vias)})
        return d


# ----------------------------------------------------------------------------------------- Result
class Result:
    def __init__(self, gr):
        self.gr = gr
        self.st, self.ov, self.xv, self.over_conn = gr.stats()
        gr._ovc = (self.ov, self.xv)

    def summary_line(self):
        s = self.st
        return ("groute: %d conns: %d routed clean, %d overflowing (total overflow %d, max %d on %d resources), %d no-path | "
                "vias %d, wirelength %.0f mm | %d iterations (best @%d), %.1f s (setup %.1f + search %.1f)" % (
                    s["connections"], s["routed_ok"], s["overflowing"], s["total_overflow"], s["max_overflow"],
                    s["over_resources"], s["no_path"], s["vias"], s["wirelength_mm"], s["iterations"], s["best_iter"], s["time_s"], s["setup_s"], s["search_s"]))

    expl_budget = 12.0

    def text(self, max_blocked=25, with_map=True):
        gr = self.gr
        L = [self.summary_line()]
        L.append("grid %dx%d cells of %.2f mm, layers %s; via cost %.1f mm, escape cap %d" % (
            gr.nx, gr.ny, gr.s, "/".join(gr.layers), gr.via_cost, gr.esc_cap))
        for n in gr.hint_notes:
            L.append("hint note: " + n)
        L.append("")
        L.append("== convergence (overflow per iteration) ==")
        for c in gr.curve:
            L.append("  it %2d  overflow %4d  resources %3d  conns %3d  rerouted %3d  pf %7.2f  t %5.1fs" % (
                c["iter"], c["overflow"], c["over_resources"], c["conns_over"], c["rerouted"], c["pf"], c["t"]))
        L.append("")
        L.append("== hot spots (overflow now first, then accumulated history) ==")
        rows = gr.hot_spots(self.ov, self.xv)
        if not rows:
            L.append("  none")
        for h, d in rows:
            nm = sorted(d["nets"])
            lab = ""
            hh = h.split()[-1]
            if gr.names.T and hh in gr.names.T.cuts and gr.names.cut_label(hh):
                lab = " [%s]" % gr.names.cut_label(hh)
            w = d["where"]
            L.append("  %-8s%s over %d  hist %.1f  (%d resources%s)  at (%.0f,%.0f)  nets: %s" % (
                h, lab, d["over"], d["hist"], d["n"], ", sum cap %d" % d["cap"] if d["cap"] else "", w[0] if w else 0, w[1] if w else 0,
                " ".join(nm[:14]) + (" ...(+%d)" % (len(nm) - 14) if len(nm) > 14 else "")))
        L.append("")
        L.append("== thrash (net: rips, fight partners) ==")
        th = gr.thrash()
        if not th:
            L.append("  no rip-ups (converged in the first pass)")
        for net, d in th[:12]:
            f = sorted(d["fights"].items(), key=lambda kv: -kv[1])[:4]
            L.append("  %-12s ripped %3dx over %d conns; vs %s" % (net, d["rips"], d["conns"], ", ".join("%s %dx" % x for x in f) or "-"))
        pairs = defaultdict(int)
        for net, d in th:
            for o, k in d["fights"].items():
                pairs[tuple(sorted((net, o)))] += k
        if pairs:
            L.append("  fight pairs: " + "  ".join("%s<>%s %d" % (a, b, k) for (a, b), k in sorted(pairs.items(), key=lambda kv: -kv[1])[:8]))
        L.append("")
        L.append("== blocked connections (%d) ==" % len(self.over_conn))
        self.blocked = []
        t_end = time.time() + self.expl_budget
        for i, ci in enumerate(sorted(self.over_conn, key=lambda ci: -sum(self.ov.get(r, 0) for r in (gr.routes[ci] or {"res": []})["res"]))):
            ex = gr.explain_conn(ci, self.ov, self.xv, with_alt=(i < max_blocked and time.time() < t_end))
            self.blocked.append(ex)
            if i < max_blocked:
                L.append("  " + ex["line"] + "  (%s)" % ex["pads"])
        if len(self.over_conn) > max_blocked:
            L.append("  ... +%d more (use --nets / --region to focus)" % (len(self.over_conn) - max_blocked))
        if with_map:
            L.append("")
            L.append("== history-cost map (accumulated congestion = tangle heat) ==")
            L.append(gr.history_map())
        return "\n".join(L)

    def to_json(self):
        gr = self.gr
        if not hasattr(self, "blocked"):
            self.text(with_map=False)
        return {"version": 1, "summary": self.st, "grid": {"cell": gr.s, "origin": [gr.gx0, gr.gy0], "nx": gr.nx, "ny": gr.ny,
                                                           "layers": gr.layers, "via_dia": gr.rl.via_dia, "via_drill": gr.rl.via_drill,
                                                           "track_width": gr.rl.track_width, "clearance": gr.rl.clearance},
                "convergence": gr.curve,
                "connections": [gr.route_json(ci) for ci in range(len(gr.conns))],
                "blocked": [{k: v for k, v in b.items()} for b in self.blocked],
                "hot_spots": [{"handle": h, "over": d["over"], "hist": round(d["hist"], 2), "nets": sorted(d["nets"])}
                              for h, d in gr.hot_spots(self.ov, self.xv)],
                "thrash": [{"net": n, "rips": d["rips"], "fights": dict(d["fights"])} for n, d in gr.thrash()]}


# ----------------------------------------------------------------------------------------- entry points
def select_conns(board, rn, names, nets=None, region=None):
    conns = list(rn.conns)
    if nets:
        pats = [p.strip() for p in nets.split(",") if p.strip()]
        conns = [c for c in conns if any(fnmatch.fnmatchcase(c.net, p) for p in pats)]
    if region:
        sel = []
        for rg in [x.strip() for x in region.split(",")]:
            if names.T and rg in names.T.regions:
                bb = names.T.regions[rg].bbox
                for c in conns:
                    cb = geom.bbox_of([c.pa, c.pb])
                    if geom.bbox_overlap(bb, cb):
                        # line actually passes through the region?
                        for t in [i / 24 for i in range(25)]:
                            p = (c.pa[0] + t * (c.pb[0] - c.pa[0]), c.pa[1] + t * (c.pb[1] - c.pa[1]))
                            if bb[0] <= p[0] <= bb[2] and bb[1] <= p[1] <= bb[3]:
                                sel.append(c)
                                break
            elif names.T and rg in names.T.cuts:
                cc = names.T.cut_cross[rg]
                sel += [names.T.conns[i] for i in cc]
            else:
                raise SystemExit("unknown region/cut id %r" % rg)
        seen = set()
        conns = []
        for c in sel:
            if id(c) not in seen:
                seen.add(id(c))
                conns.append(c)
    return conns


def run(board, rn=None, cell=1.0, iters=30, tlimit=30.0, hints=(), nets=None, region=None, via_cost=6.0, esc_cap=2,
        res=0.1, verbose=False, names=None, stall=8, **kw):
    if kw.setdefault("styles", "auto") == "auto":          # net styles of badpcb.json (none configured -> {} -> exactly the global costs)
        from . import styles as _st
        kw["styles"] = _st.for_board(board)
    rn = rn or ratsnest.build(board)
    names = names or Names(board, rn)
    conns = select_conns(board, rn, names, nets, region)
    gr = GRouter(board, rn, conns, cell=cell, res=res, via_cost=via_cost, esc_cap=esc_cap, hints=hints, names=names,
                 verbose=verbose, **kw)
    gr.run(iters=iters, tlimit=max(0.0, tlimit - (time.time() - gr.t0)), verbose=verbose, stall=stall)
    return Result(gr)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="badpcb groute", description="PathFinder global router with explanations")
    ap.add_argument("board")
    ap.add_argument("--cell", type=float, default=1.0)
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument("--time", type=float, default=30.0, help="total budget in seconds (includes setup)")
    ap.add_argument("--hints")
    ap.add_argument("--nets", help="comma list of globs, e.g. EN*,SENSE*")
    ap.add_argument("--region", help="region/cut ids (R8,C12): only connections passing through")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--via-cost", type=float, default=6.0)
    ap.add_argument("--stall", type=int, default=8, help="stop after this many iterations without improvement")
    ap.add_argument("--esc-cap", type=int, default=2)
    ap.add_argument("--max-blocked", type=int, default=25)
    ap.add_argument("--no-map", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    board = load_board(a.board)
    res = run(board, cell=a.cell, iters=a.iters, tlimit=a.time, hints=load_hints(a.hints), nets=a.nets, region=a.region,
              via_cost=a.via_cost, esc_cap=a.esc_cap, verbose=a.verbose, stall=a.stall)
    print(res.text(max_blocked=a.max_blocked, with_map=not a.no_map))
    if a.json_out:
        with open(a.json_out, "w") as fh:
            json.dump(res.to_json(), fh)
        print("wrote %s" % a.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
