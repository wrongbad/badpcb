"""badpcb audit: placement linter. Catches "dumb placement" before minutes-long routing passes.

    python3 -m badpcb audit <board.kicad_pcb | session> [--json] [--full] [--roles] [--groute] [--only ESCAPE,PLANE,..]
    badpcb suggest <session> audit            # the audit's best fix per finding as an eval table: `badpcb act S @N`

Detectors (priority order): ESCAPE PLANE BOWTIE ORIENT TENSION ROLES INTENT BUSORDER COURTYARD DENSITY.
Every finding has a severity, the named handles (parts, nets, regions), evidence numbers, fixes written as badpcb compact
actions (pasteable into `badpcb act` / `badpcb eval`) with the fix's predicted delta (evaluate.py, read-only: applied and undone)
and its legality (actions.check -> legal.py).  A finding with fixes that are all illegal is reported without a fix.

Design rules to stay quiet: ORIENT / BOWTIE / TENSION only report when a legal fix improves the metric; parts whose
connections are already routed are skipped by those three; expected parts (decaps at their pin) are not "intruders";
overlaps are judged on the courtyard model with a tolerance (the extractor boxes are a few 0.01 mm larger than KiCad's).

Pure stdlib (runs under $KIPY too). The intent ratsnest ("what the netlist wants", pad to pad MST over every signal net,
existing copper ignored) is what the geometric detectors see, so they work the same on a routed and an unrouted board.
"""
import dataclasses
import json
import math
import re
import sys
import time
from collections import defaultdict

from . import actions as A
from . import evaluate as EV
from . import config, feas, geom, legal, ratsnest, swaps
from . import intent as IN
from .state import fmt_delta, parse_specs

DETS = ["ESCAPE", "PLANE", "BOWTIE", "ORIENT", "TENSION", "ROLES", "INTENT", "BUSORDER", "COURTYARD", "DENSITY"]
SEV_W = {"ERR": 10.0, "WARN": 4.0, "NOTE": 1.0}
SEV_ORD = {"ERR": 0, "WARN": 1, "NOTE": 2}
COURTYARD_BIAS = 0.09         # extractor courtyard boxes are ~0.045 mm larger per side than KiCad's polygons
COURTYARD_TOL = 0.005         # mm of true overlap that KiCad's DRC flags (any positive overlap)
PLANE_VIA_RADIUS = 2.5        # mm: a plane via must fit this close to the pad
MAX_EVALS = 90

# Role-rule / escape-zone tunables (mm unless noted). Override per project with an "audit": {...} block in badpcb.json;
# unknown keys are rejected. `badpcb audit --roles` and `badpcb project` print the effective values.
DEFAULTS = {
    "ic_min_pins": 6,            # connected pins for a part to count as an IC (role anchors, fan-zone sides)
    "bulk_farads": 1.5e-6,       # supply cap above this is "bulk" (looser distance rule, may anchor at a connector)
    "decap_bypass_mm": 3.0,      # bypass cap centre -> supply pin of its IC
    "decap_bulk_mm": 10.0,       # bulk cap -> IC supply pin / connector
    "reg_cap_mm": 4.0,           # bulk cap on an inductor net -> inductor or IC pin
    "crystal_mm": 8.0,           # crystal pad -> IC pin
    "xtal_load_mm": 3.0,         # load cap -> crystal pad
    "filter_cap_mm": 6.0,        # signal cap -> IC pin
    "filter_cap_rc_mm": 8.0,     # signal cap on an RC node (one resistor hop) -> IC pin
    "pull_mm": 15.0,             # pull-up / pull-down resistor -> IC pin
    "esd_mm": 6.0,               # ESD/TVS -> connector pad
    "violation_slack": 1.3,      # a role is only a finding beyond limit x slack
    "err_factor": 2.5,           # ... and an ERR beyond limit x err_factor (else WARN)
    "joint_hard_factor": 1.15,   # joint placement: hard limit = factor x role rule
    "joint_far_mm": 12.0,        # joint placement: hard limit for parts without a distance rule
    "zone_depth_min_mm": 2.0,    # fan-zone depth = clamp(zone_depth_pitch * pitch * pins, min, max)
    "zone_depth_max_mm": 3.0,
    "zone_depth_pitch": 0.3,
    "zone_widen_mm": 1.0,        # fan-zone widening beyond the outermost pads, per side
    "intent_err": 8.0,           # INTENT: an entry whose weighted penalty is at least this is an ERR (below: WARN)
}


def params():
    """DEFAULTS overridden by the project's "audit" block (cached per project object)."""
    cur = config.current()
    c = getattr(params, "_c", None)
    if c is None or c[0] is not cur:
        over = cur.audit()
        bad = sorted(set(over) - set(DEFAULTS))
        if bad:
            sys.exit("badpcb: unknown \"audit\" key(s) in badpcb.json: %s (known: %s)" % (", ".join(bad), ", ".join(DEFAULTS)))
        c = params._c = (cur, {**DEFAULTS, **over})
    return c[1]


# ============================================================================= model
class Fix:
    def __init__(self, acts, note="", tag=""):
        self.acts = list(acts)            # compact action strings
        self.note = note
        self.tag = tag
        self.res = None                   # evaluate.Result
        self.gain = None                  # detector-specific benefit (ESCAPE: drop in fan usage); ranks fixes before dscore
        self.after = ""

    @property
    def spec(self):
        return "; ".join(self.acts)

    @property
    def ok(self):
        return self.res is not None and self.res.ok

    def legal_str(self):
        r = self.res
        if r is None:
            return ""
        if not r.ok:
            return "ILLEGAL (%s)" % "; ".join(r.errors)[:90]
        if r.warnings:
            return "legal, warn: %s" % r.warnings[0][:80] + (" (+%d)" % (len(r.warnings) - 1) if len(r.warnings) > 1 else "")
        return "legal"

    def as_dict(self):
        d = {"actions": self.acts, "note": self.note, "tag": self.tag}
        if getattr(self, "g1", None):
            d["groute"] = {"before": self.g0, "after": self.g1}
        if self.res is not None:
            d.update({"legal": self.res.ok, "errors": self.res.errors, "warnings": self.res.warnings,
                      "delta": self.res.d, "dscore": round(self.res.dscore, 3)})
        return d


class Finding:
    def __init__(self, det, sev, title, handles=None, ev=None, fixes=None, where=None, mag=0.0, detail=None):
        self.det, self.sev, self.title = det, sev, title
        self.handles = handles or {}
        self.ev = ev or {}
        self.fixes = fixes or []
        self.where = where                # (x, y) anchor for comparison with groute hot spots
        self.mag = mag                    # ordering inside (sev, detector)
        self.detail = detail or []        # extra text lines (shown with --full)
        self.advice = []                  # non-pasteable suggestions (zone reshape ...)
        self.sticky = None

    @property
    def weight(self):
        return SEV_W[self.sev] * (1.0 + min(2.0, self.mag))

    @property
    def best(self):
        ok = [f for f in self.fixes if f.ok]
        return ok[0] if ok else None

    def as_dict(self):
        return {"det": self.det, "sev": self.sev, "title": self.title, "handles": self.handles, "evidence": self.ev,
                "where": list(self.where) if self.where else None, "weight": round(self.weight, 2),
                "fixes": [f.as_dict() for f in self.fixes], "advice": self.advice}


# ============================================================================= context
GND_RX = re.compile(r"^(A|D|P|S)?GND\w*$|^VSS\w*$", re.I)
SUPPLY_RX = re.compile(r"^[+-]?\d+V\d*\w*$|VDD|VCC|VREG|VBUS|VBAT|VIN|^\+\d", re.I)
PASSIVE_RX = re.compile(r"^(R|C|L|FB|D|F|TP)\d")


def farads(v):
    m = re.match(r"^\s*([0-9.]+)\s*([pnuµμm]?)", v or "")
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0}[m.group(2)]


class Trial:
    """Apply actions to the live world temporarily (geometry only; engine untouched); undone on exit."""

    def __init__(self, w):
        self.w, self.inv = w, []

    def apply(self, act):
        errs, _ = A.check(self.w, act)
        if errs:
            return False
        inv, _n = A.apply(self.w, act, check_first=False)
        self.inv.append(inv)
        return True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        for inv in reversed(self.inv):
            A.apply(self.w, inv, check_first=False)
        self.inv = []


class Ctx:
    def __init__(self, st, rules_pro=None):
        self.st = st
        self.w = st.world
        self.b = st.world.board
        from . import apply as _ap
        self.eff = feas.load_rules(rules_pro or _ap.default_rules_pro(self.b.source), self.b)
        self.planes = set(self.b.plane_nets()) | set(st.assume)
        self.t0 = time.time()
        self.n_eval = 0
        self._irn = None
        self._roles = None
        self._zones = None
        self.base = st.metrics()
        self.skel = st.skel

    # ---- nets
    def is_gnd(self, net):
        return bool(net) and (bool(GND_RX.match(net)) or (net in self.planes and net.upper().startswith("GND")))

    def is_supply(self, net):
        return bool(net) and not self.is_gnd(net) and (net in self.planes or bool(SUPPLY_RX.search(net)))

    def is_signal(self, net):
        return bool(net) and net not in self.planes and not self.is_gnd(net) and not self.is_supply(net)

    def is_passive(self, p):
        return len(p.pads) <= 3 and bool(PASSIVE_RX.match(p.ref))

    def is_ic(self, p):
        return not self.is_passive(p) and len({d.num for d in p.pads if d.net}) >= params()["ic_min_pins"] and not p.ref.startswith("SW")

    def layer_of(self, ref):
        return "B.Cu" if self.b.parts[ref].side == "B" else "F.Cu"

    def region(self, pt):
        return self.skel.region_at(pt).id

    @property
    def zones(self):
        if self._zones is None:
            self._zones = [z for z in escape_zones(self) if z.n_pads >= params()["ic_min_pins"]]
        return self._zones

    # ---- intent ratsnest: pad to pad MST of every non-plane net, existing copper ignored
    @property
    def irn(self):
        if self._irn is None:
            b2 = dataclasses.replace(self.b, tracks=[], vias=[])
            self._irn = ratsnest.build(b2, self.st.assume)
        return self._irn

    def lines(self):
        """[(net, pa, pb, refa, refb, padid_a, padid_b)] intent connections."""
        out = []
        for c in self.irn.conns:
            out.append((c.net, c.pa, c.pb, c.a.split(".")[0], c.b.split(".")[0], c.a, c.b))
        return out

    def routed_part(self, ref):
        """True if every signal pad of the part already has copper on it (nothing left to improve by placing)."""
        p = self.b.parts[ref]
        pads = [d for d in p.pads if self.is_signal(d.net)]
        if not pads:
            return False
        if not hasattr(self, "_endpoints"):
            ep = defaultdict(list)
            for t in self.b.tracks:
                ep[t.net].append(t.a)
                ep[t.net].append(t.b)
            for v in self.b.vias:
                ep[v.net].append(v.pos)
            self._endpoints = ep
        for d in pads:
            r = max(d.size) / 2 + 0.05
            if not any(abs(q[0] - d.pos[0]) <= r and abs(q[1] - d.pos[1]) <= r for q in self._endpoints.get(d.net, ())):
                return False
        return True

    # ---- evaluation
    def predict(self, fix):
        if fix.res is None:
            self.n_eval += 1
            fix.res = EV.evaluate(self.st, [fix.spec])[0]
        return fix.res

    def movable(self, ref, big=False):
        p = self.b.parts.get(ref)
        if not p or p.locked or ref == self.w.cfg["mcu"]:
            return False
        if big:
            return len(p.pads) <= 20 and p.area < 150
        return len(p.pads) <= 6 and p.area < 25

    # ---- placement search
    def plane_keep(self, ref):
        """pads -> bool: every pad of a plane net that now sits inside its plane zone stays inside it (None if nothing to keep)."""
        b = self.b
        keep = [(i, d) for i, d in enumerate(b.parts[ref].pads) if d.net in self.planes and A._in_zone(b, d.net, d.pos)]
        if not keep:
            return None
        return lambda pads: all(A._in_zone(b, d.net, pads[i][0]) for i, d in keep)

    def pads_at(self, ref, pos, rot, side=None):
        p = self.b.parts[ref]
        _, pads = A.place_geom(self.w, ref, pos, rot, side or p.side)
        return pads

    def legal_spots(self, ref, anchor, pad_num=None, rmin=0.0, rmax=6.0, avoid=(), keep=2, step=0.3, rots=None, need=None, nang=20):
        """Legal placements of `ref` with pad `pad_num` within [rmin, rmax] of anchor, avoiding rects `avoid`.
        need(pads)->bool is an extra pad-level predicate. -> [(r, x, y, rot)] nearest first (one per rotation)."""
        p = self.b.parts[ref]
        pad_num = pad_num or p.pads[0].num
        k = [i for i, d in enumerate(p.pads) if d.num == str(pad_num)][0]
        need0 = need
        pk = self.plane_keep(ref)
        need = (lambda pads, pos: pk(pads) and (need0 is None or need0(pads, pos))) if pk else need0
        res = []
        for rot in (rots if rots is not None else [p.rot, p.rot + 180, p.rot + 90, p.rot + 270]):
            pads0 = self.pads_at(ref, p.pos, rot)
            off = (pads0[k][0][0] - p.pos[0], pads0[k][0][1] - p.pos[1])
            found = None
            r = max(rmin, 0.0)
            while r <= rmax + 1e-9 and found is None:
                for j in range(nang):
                    a = 2 * math.pi * j / nang
                    T = (anchor[0] + r * math.cos(a), anchor[1] + r * math.sin(a))
                    pos = (round(T[0] - off[0], 2), round(T[1] - off[1], 2))
                    pads = self.pads_at(ref, pos, rot)
                    if avoid and _pads_hit(p, pads, avoid):
                        continue
                    if need and not need(pads, pos):
                        continue
                    if A.fits(self.w, ref, pos, rot):
                        found = (r, pos[0], pos[1], A._norm(rot))
                        break
                r += step
            if found:
                res.append(found)
        res.sort(key=lambda s: (round(s[0], 1), abs(A._norm(s[3] - p.rot))))
        return res[:keep]


def _ctx_ring_spot(self, ref, center, ok, rmax=12.0, step=0.4, nang=24, rots=None):
    """Nearest legal placement of `ref` to `center` (centre of the part) for which ok(bbox, pos, rot) holds: rings of growing radius,
    same orientation first (rotating is only tried for small passives). -> (x, y, rot) or None."""
    p = self.b.parts[ref]
    if rots is None:
        rots = [p.rot, p.rot + 180, p.rot + 90, p.rot + 270] if self.movable(ref) else [p.rot]
    pk = self.plane_keep(ref)
    r = 0.0
    while r <= rmax + 1e-9:
        cands = []
        for rot in rots:
            for j in range(nang if r > 0 else 1):
                a = 2 * math.pi * j / nang
                pos = (round(center[0] + r * math.cos(a), 2), round(center[1] + r * math.sin(a), 2))
                bb, pads = A.place_geom(self.w, ref, pos, rot, p.side)
                if not ok(bb, pos, rot) or (pk and not pk(pads)) or not A.fits(self.w, ref, pos, rot):
                    continue
                cands.append((abs(A._norm(rot - p.rot)), geom.dist(pos, p.pos), pos, A._norm(rot)))
        if cands:
            c = min(cands, key=lambda t: (t[0], t[1]))
            return c[2][0], c[2][1], c[3]
        r += step
    return None


Ctx.ring_spot = _ctx_ring_spot


def _pads_hit(part, pads, rects, grow=0.1):
    for d, (pp, pr, pl) in zip(part.pads, pads):
        sh = legal.pad_shape(pp, pr, d.size, d.shape)
        bb = sh.bbox
        for r in rects:
            if not (bb[2] < r[0] - grow or bb[0] > r[2] + grow or bb[3] < r[1] - grow or bb[1] > r[3] + grow):
                return True
    return False


def mv(ref, x, y, rot=None):
    return A.Action("move", ref=ref, x=x, y=y, **({"rot": rot} if rot is not None else {})).compact()


def mv_spot(p, s):
    r, x, y, rot = s
    return mv(p.ref, x, y, rot if abs(A._norm(rot - p.rot)) > 1e-6 else None)


# ============================================================================= ESCAPE
class EZone:
    pass


def escape_zones(ac, refs=None, any_pitch=False):
    """Escape zone of every fine-pitch side: rect from the pad row outward by D (2..3 mm, 0.3 * pitch * pins), widened by 1 mm.
    any_pitch: for the named `refs`, also parts that are not fine-pitch (design-intent `clear` zones); cheap (only those parts are scanned)."""
    b = ac.b
    K = params()
    if any_pitch and refs:
        geo = {}
        for r in refs:
            if r in b.parts:
                S = feas.sides(b.parts[r])
                g = {k: feas.side_geom(k, L) for k, L in S.items()}
                g = {k: v for k, v in g.items() if v}
                if g:
                    geo[r] = (S, g)
    else:
        fp, geo = feas.fine_parts(b)
    out = []
    for ref, (S, g) in sorted(geo.items()):
        if refs and ref not in refs:
            continue
        p = b.parts[ref]
        lay = ac.layer_of(ref)
        for key in sorted(g):
            pitch, pw, ln = g[key]
            L = S[key]
            used = [d for d in L if d.net]
            if not used:
                continue
            ax = 1 if key in "WE" else 0               # tangent axis
            nrm = {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}[key]
            z = EZone()
            z.ref, z.side, z.layer, z.pitch, z.pw, z.ln = ref, key, lay, pitch, pw, ln
            z.n_pads, z.pads = len(L), used
            z.D = min(K["zone_depth_max_mm"], max(K["zone_depth_min_mm"], K["zone_depth_pitch"] * pitch * len(L)))
            sgn = nrm[0] + nrm[1]
            z.sgn, z.ax = sgn, ax
            line = sum(d.pos[1 - ax] for d in L) / len(L)
            z.line = line
            ts = [d.pos[ax] for d in used]
            z.t0, z.t1 = min(ts) - pw / 2 - K["zone_widen_mm"], max(ts) + pw / 2 + K["zone_widen_mm"]
            a = line + sgn * ln / 2
            c = line + sgn * (ln / 2 + z.D)
            lo, hi = min(a, c), max(a, c)
            z.rect = (z.t0, lo, z.t1, hi) if ax == 0 else (lo, z.t0, hi, z.t1)
            z.a_n, z.c_n = a, c
            z.name = "%s.%s" % (ref, key)
            out.append(z)
    return out


STEP = 0.05


class Raster:
    """Blocked map of the zone rect on one layer, obstacles inflated by clearance + track/2 (rules per obstacle location)."""

    def __init__(self, rect):
        self.x0, self.y0 = rect[0], rect[1]
        self.nx = int(math.ceil((rect[2] - rect[0]) / STEP)) + 1
        self.ny = int(math.ceil((rect[3] - rect[1]) / STEP)) + 1
        self.owner = {}

    def cell(self, x, y):
        return int(round((x - self.x0) / STEP)), int(round((y - self.y0) / STEP))

    def mark(self, bb, label, test):
        i0, j0 = self.cell(bb[0], bb[1])
        i1, j1 = self.cell(bb[2], bb[3])
        for i in range(max(0, i0), min(self.nx - 1, i1) + 1):
            x = self.x0 + i * STEP
            for j in range(max(0, j0), min(self.ny - 1, j1) + 1):
                if test(x, self.y0 + j * STEP):
                    self.owner.setdefault((i, j), label)

    def blocked(self, x, y):
        i, j = self.cell(x, y)
        return self.owner.get((i, j))


def _rules_at(ac, pt):
    """(clearance, track) at a point: the fine rule area it sits in, else defaults."""
    clr, tr = ac.eff.clr, ac.eff.track
    for a in ac.eff.areas:
        bb = a.resolve(ac.b)
        if bb and bb[0] <= pt[0] <= bb[2] and bb[1] <= pt[1] <= bb[3]:
            if a.clr is not None:
                clr = a.clr
            if a.track is not None:
                tr = min(tr, a.track) if a.track < tr else tr
    return clr, tr


def zone_scan(ac, z):
    """Everything foreign inside zone z + escape capacity. Reads the live world."""
    b = ac.b
    ref = z.ref
    side_nets = {d.net for d in z.pads}
    R = (z.rect[0] - 1.0, z.rect[1] - 1.0, z.rect[2] + 1.0, z.rect[3] + 1.0)
    rasters = {}
    layers = b.signal_layers()
    for l in layers:
        rasters[l] = Raster(z.rect)
    vr = Raster(z.rect)                  # via sites: anything on any layer blocks a via (inflated by via radius + clearance)
    vmarg = max(0.0, b.rules.via_dia / 2 - ac.eff.track / 2)
    parts_in, vias_in, tracks_in = {}, [], []
    # pads
    pidx = legal._pad_index(ac.w)
    seen = set()
    for c in legal._cells(R):
        for oref, od, osh in pidx.get(c, ()):
            if oref == ref or id(od) in seen:
                continue
            seen.add(id(od))
            bb = osh.bbox
            if bb[2] < R[0] or bb[0] > R[2] or bb[3] < R[1] or bb[1] > R[3]:
                continue
            clr, tr = _rules_at(ac, od.pos)
            infl = clr + tr / 2
            lay = [l for l in layers if l in od.layers or od.through]
            if not lay:
                continue
            inzone = not (bb[2] < z.rect[0] - infl or bb[0] > z.rect[2] + infl or bb[3] < z.rect[1] - infl or bb[1] > z.rect[3] + infl)
            if inzone:
                for l in lay:
                    if l == z.layer or od.through:
                        parts_in.setdefault(oref, set()).add(od.num)
            for l in lay:
                rasters[l].mark((bb[0] - infl, bb[1] - infl, bb[2] + infl, bb[3] + infl), oref,
                                lambda x, y, od=od, infl=infl: A._pt_rect_dist((x, y), od) <= infl)
            iv = infl + vmarg
            vr.mark((bb[0] - iv, bb[1] - iv, bb[2] + iv, bb[3] + iv), oref, lambda x, y, od=od, iv=iv: A._pt_rect_dist((x, y), od) <= iv)
    # vias / tracks
    for v in b.vias:
        if not (R[0] <= v.pos[0] <= R[2] and R[1] <= v.pos[1] <= R[3]):
            continue
        if v.net in side_nets or (v.net in ac.planes and not v.locked):
            continue
        clr, tr = _rules_at(ac, v.pos)
        rr = v.dia / 2 + clr + tr / 2
        vias_in.append(v)
        for l in layers:
            rasters[l].mark((v.pos[0] - rr, v.pos[1] - rr, v.pos[0] + rr, v.pos[1] + rr), "via:" + (v.net or "-"),
                            lambda x, y, v=v, rr=rr: geom.dist((x, y), v.pos) <= rr)
        rv = rr + vmarg
        vr.mark((v.pos[0] - rv, v.pos[1] - rv, v.pos[0] + rv, v.pos[1] + rv), "via:" + (v.net or "-"), lambda x, y, v=v, rv=rv: geom.dist((x, y), v.pos) <= rv)
    for t in b.tracks:
        if t.net in side_nets or t.layer not in rasters or (t.net in ac.planes and not t.locked):
            continue
        if max(t.a[0], t.b[0]) < R[0] or min(t.a[0], t.b[0]) > R[2] or max(t.a[1], t.b[1]) < R[1] or min(t.a[1], t.b[1]) > R[3]:
            continue
        clr, tr = _rules_at(ac, t.a)
        rr = t.w / 2 + clr + tr / 2
        tracks_in.append(t)
        rasters[t.layer].mark((min(t.a[0], t.b[0]) - rr, min(t.a[1], t.b[1]) - rr, max(t.a[0], t.b[0]) + rr, max(t.a[1], t.b[1]) + rr),
                              "trk:" + (t.net or "-"), lambda x, y, t=t, rr=rr: geom.pt_seg_dist((x, y), t.a, t.b) <= rr)
        rv = rr + vmarg
        vr.mark((min(t.a[0], t.b[0]) - rv, min(t.a[1], t.b[1]) - rv, max(t.a[0], t.b[0]) + rv, max(t.a[1], t.b[1]) + rv),
                "trk:" + (t.net or "-"), lambda x, y, t=t, rv=rv: geom.pt_seg_dist((x, y), t.a, t.b) <= rv)
    # capacity = tracks leaving across the zone's cross-section on the part's own layer (worst depth)
    #          + via sites in two staggered rows (a stub ends in a via that takes the net to another layer)
    def runs(rs, d, pitch_fn):
        n = z.line + z.sgn * (z.ln / 2 + d)
        run, tot = 0, 0
        t = z.t0
        pitch = None
        while t <= z.t1 + 1e-9:
            xy = (t, n) if z.ax == 0 else (n, t)
            if pitch is None:
                pitch = pitch_fn(xy)
            if rs.blocked(*xy):
                if run:
                    tot += int(((run - 1) * STEP) / pitch) + 1
                run = 0
            else:
                run += 1
            t += STEP
        if run:
            tot += int(((run - 1) * STEP) / pitch) + 1
        return tot
    lane_pitch = lambda xy: sum(_rules_at(ac, xy))
    worst = None
    d = 0.35
    while d <= z.D - 0.1 + 1e-9:
        k = runs(rasters[z.layer], d, lane_pitch)
        worst = k if worst is None else min(worst, k)
        d += 0.25
    via_pitch = lambda xy: b.rules.via_dia + _rules_at(ac, xy)[0]
    nvia = 0
    d = 0.7
    while d <= z.D - 0.2 + 1e-9:
        nvia += runs(vr, d, via_pitch)
        d += 0.7
    caps = {"lanes": worst or 0, "vias": nvia}
    # direct straight stubs on the part's layer
    direct, blocked = 0, []
    rs = rasters.get(z.layer)
    for dpad in z.pads:
        t = dpad.pos[z.ax]
        free, who = True, None
        s = 0.0
        while s <= z.D:
            n = z.line + z.sgn * (z.ln / 2 + s) if s > 0 else z.line + z.sgn * z.ln / 2
            xy = (t, n) if z.ax == 0 else (n, t)
            o = rs.blocked(*xy) if rs else None
            if o:
                free, who = False, o
                break
            s += STEP
        if free:
            direct += 1
        else:
            blocked.append((dpad, who))
    need = len(z.pads)
    cap = caps["lanes"] + caps["vias"]
    r = {"need": need, "cap": cap, "caps": caps, "used": need / cap if cap else 9.99, "direct": direct, "blocked": blocked,
         "parts": parts_in, "vias": vias_in, "tracks": tracks_in}
    return r


def _tolerated(ac, z, oref):
    """Expected neighbours of a fine-pitch side: decaps that serve one of the side's supply pins."""
    role = ac.roles().get(oref)
    if role and role.kind in ("decap", "reg_cap") and role.anchor and role.anchor.startswith(z.ref + "."):
        return True
    return False


def _blocked_signal(ac, z, sc, tol):
    return [(d, o) for d, o in sc["blocked"] if ac.is_signal(d.net) and o not in tol]


def detect_escape(ac):
    out = []
    zones = ac.zones
    for z in zones:
        sc = zone_scan(ac, z)
        z.scan = sc
        intr = {r: nums for r, nums in sc["parts"].items() if not _tolerated(ac, z, r)}
        tol = sorted(r for r in sc["parts"] if r not in intr)
        z.tol = set(tol)
        locked_vias = [v for v in sc["vias"] if v.locked and ac.is_signal(v.net)]
        locked_trk = [t for t in sc["tracks"] if t.locked and ac.is_signal(t.net)]
        used = sc["used"]
        sig = [d for d in z.pads if ac.is_signal(d.net)]
        bs = _blocked_signal(ac, z, sc, z.tol)
        z.bs = bs
        frac = len(bs) / max(1, len(sig))
        if used >= 0.75 or (len(bs) >= 6 and frac >= 0.7):
            sev = "ERR"
        elif used >= 0.5 or len(bs) >= 3:
            sev = "WARN"
        elif bs:
            sev = "NOTE"
        else:
            continue
        mid = ((z.rect[0] + z.rect[2]) / 2, (z.rect[1] + z.rect[3]) / 2)
        f = Finding("ESCAPE", sev, "%s fan zone %.1f mm deep: %d stubs needed / %d free slots (%d lanes + %d via sites) = %.0f%% used; %d of %d signal stubs blocked by %s" % (
            z.name, z.D, sc["need"], sc["cap"], sc["caps"]["lanes"], sc["caps"]["vias"], 100 * used, len(bs), len(sig),
            " ".join(sorted({o for _, o in bs}, key=_nat)[:8])),
            handles={"part": z.ref, "side": z.side, "region": ac.region(mid), "intruders": sorted(intr, key=_nat),
                     "tolerated": tol, "nets": sorted({d.net for d, _ in bs})},
            ev={"need": sc["need"], "cap": sc["cap"], "cap_split": sc["caps"], "used_pct": round(100 * used), "signal_pads": len(sig),
                "signal_blocked": len(bs), "direct_stubs": sc["direct"],
                "blocked_pads": ["%s(%s)<-%s" % (d.id, d.net, o) for d, o in bs],
                "foreign_vias": len(sc["vias"]), "foreign_tracks": len(sc["tracks"]), "locked_vias": len(locked_vias),
                "locked_tracks": len(locked_trk), "rect": [round(v, 2) for v in z.rect]},
            where=mid, mag=max(0.0, used - 0.5) + 0.15 * len(bs))
        f.zone = z
        out.append(f)
        # fixes: push intruders out along the side normal (legal spots only), worst blocker first
        order = sorted(intr, key=lambda r: (-sum(1 for _, o in bs if o == r), -len(intr[r]), _nat(r)))
        f.fixes += _escape_push(ac, z, [r for r in order if ac.movable(r)])
        f.fixes += _escape_swaps(ac, z, sc)
        if locked_trk or locked_vias:
            nets = sorted({t.net for t in locked_trk} | {v.net for v in locked_vias})
            f.advice.append("locked signal copper in the zone (nets %s): `unlock %s` frees it for the router" % (" ".join(nets[:6]), nets[0]))
        unm = [r for r in order if not ac.movable(r)]
        if unm:
            f.advice.append("not movable here (locked/large/other side): %s" % " ".join(unm))
    return out


def _push_candidates(ac, z, ref):
    """Cheapest legal spot with every pad outside all escape zones of the same part: along the side normal and/or sideways
    (the board edge often blocks the normal). Keeps a role distance rule (filter cap near its pin) when the part has one."""
    p = ac.b.parts[ref]
    nx, ny = ({"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}[z.side])
    rects = [q.rect for q in ac.zones if q.ref == z.ref]
    role = ac.roles().get(ref)
    cands = []
    for rot in (p.rot, A._norm(p.rot + 90)):
        for k in range(-24, 25):
            dt = 0.5 * k
            for j in range(0, 17):
                dn = 0.25 * j
                pos = (round(p.pos[0] + nx * dn + (dt if z.ax == 0 else 0), 2), round(p.pos[1] + ny * dn + (dt if z.ax == 1 else 0), 2))
                cost = dn + 0.7 * abs(dt) + (0.4 if abs(rot - p.rot) > 1 else 0)
                cands.append((cost, pos, rot))
    cands.sort(key=lambda c: c[0])
    tried = 0
    best = None
    pk = ac.plane_keep(ref)
    for cost, pos, rot in cands:
        pads = ac.pads_at(ref, pos, rot)
        if _pads_hit(p, pads, rects, 0.3):
            continue
        if pk and not pk(pads):
            continue
        if role and role.limit and role.anchor_pt is not None:
            pn = [pd for pd, (pp, _, _) in zip(p.pads, pads) if pd.net == role.net]
            if pn and min(geom.dist(pp[0], role.anchor_pt) for pd, pp in zip(p.pads, pads) if pd.net == role.net) > max(role.limit, role.dist or 0) * 1.5:
                continue
        tried += 1
        if tried > 400:
            break
        if not A.fits(ac.w, ref, pos, rot):
            continue
        warns = len(legal.warnings(legal.pad_conflicts(ac.w, ref, pos, rot, p.side)))
        c2 = cost + 1.2 * warns
        if best is None or c2 < best[0]:
            best = (c2, pos[0], pos[1], A._norm(rot))
            if warns == 0:
                break
        if best and cost > best[0] + 1.0:
            break
    return [best] if best else []


def _trial_zone(ac, z, acts):
    """scan the zone with the actions applied temporarily -> (used, blocked signal count) or None if they do not apply."""
    with Trial(ac.w) as tr:
        for a in acts:
            if not tr.apply(a if isinstance(a, A.Action) else A.parse(a)[0]):
                return None
        sc = zone_scan(ac, z)
        return sc["used"], len(_blocked_signal(ac, z, sc, z.tol))


def _escape_push(ac, z, refs):
    fixes = []
    if not refs:
        return fixes
    acts = []
    with Trial(ac.w) as tr:
        for r in refs[:6]:
            c = _push_candidates(ac, z, r)
            if not c:
                continue
            p = ac.b.parts[r]
            s = (0, c[0][1], c[0][2], c[0][3])
            a = A.Action("move", ref=r, x=s[1], y=s[2], **({"rot": s[3]} if abs(A._norm(s[3] - p.rot)) > 1e-6 else {}))
            if tr.apply(a):
                acts.append(a)
    variants = [("push", acts)]
    if len(acts) > 1:
        variants.append(("push1", acts[:1]))
    if len(acts) > 2:
        variants.append(("push2", acts[:2]))
    for tag, ac_list in variants:
        r = _trial_zone(ac, z, ac_list)
        f = Fix([a.compact() for a in ac_list], "push %d intruder(s) out of the fan along the %s normal" % (len(ac_list), z.side), tag)
        if r:
            f.after = "%s fan %.0f%% -> %.0f%%, blocked signal stubs %d -> %d" % (z.name, 100 * z.scan["used"], 100 * r[0], len(z.bs), r[1])
            f.gain = (z.scan["used"] - r[0]) + 0.15 * (len(z.bs) - r[1])
        fixes.append(f)
    return fixes


def _escape_swaps(ac, z, sc):
    """swap/assign candidates that move a blocked pin's net onto a pin with a clear straight stub (same swap group)."""
    out = []
    if z.ref != ac.w.cfg["mcu"] or not z.bs:
        return out
    blocked_pads = {d.num for d, _ in z.bs}
    free_pads = {d.num for d in z.pads} - {d.num for d, _ in sc["blocked"]}
    for gid, G in ac.w.groups().items():
        if G.kind not in ("pin_permutation", "pin_assignment"):
            continue
        bn = [(n, G.current[n]) for n in G.nets if G.current.get(n) in blocked_pads]
        fn = [(n, G.current[n]) for n in G.nets if G.current.get(n) in free_pads]
        for n, p in bn[:3]:
            for m, q in fn[:8]:
                out.append(Fix([A.Action("swap_pins", group=gid, a=n, b=m).compact()], "move %s (pin %s, blocked) onto pin %s (clear)" % (n, p, q), "swap"))
    return out[:12]


# ============================================================================= PLANE
def detect_plane(ac):
    out = []
    b = ac.b
    groups = defaultdict(list)
    boxed = []
    unserved = {pid for I in ac.st.eng.stub_islands() for pid in I.pads}
    for pd in b.pads():
        net = pd.net
        if net not in ac.planes or not any(z.net == net for z in b.zones):
            continue
        if A._in_zone(b, net, pd.pos):
            if pd.id in unserved and not _via_spot(ac, pd, net):
                boxed.append(pd)
            continue
        if pd.through and net in {d.net for d in b.parts[pd.ref].pads if d.net and d is not pd} and False:
            continue
        mine = {l for z in b.zones if z.net == net for l in z.layers}
        under = sorted({z.net for z in b.zones if z.net != net and set(z.layers) & mine and any(
            geom.point_in_poly(pd.pos, zp["outer"]) and not any(geom.point_in_poly(pd.pos, h) for h in zp["holes"]) for zp in z.polys)})
        groups[(net, tuple(under))].append((pd, EV._zone_gap(b, net, pd.pos)))
    clusters = []
    for (net, under), L in sorted(groups.items()):
        rest = list(L)
        while rest:
            cl = [rest.pop(0)]
            grew = True
            while grew:
                grew = False
                for t in list(rest):
                    if any(geom.dist(t[0].pos, u[0].pos) <= 15.0 for u in cl):
                        cl.append(t)
                        rest.remove(t)
                        grew = True
            clusters.append(((net, under), cl))
    for (net, under), L in clusters:
        pads = sorted(L, key=lambda t: _nat(t[0].id))
        parts = sorted({pd.ref for pd, _ in pads}, key=_nat)
        xs = [pd.pos[0] for pd, _ in pads]
        ys = [pd.pos[1] for pd, _ in pads]
        zl = sorted({l for z in b.zones if z.net == net for l in z.layers})
        gmin, gmax = min(g for _, g in pads), max(g for _, g in pads)
        box = (round(min(xs) - 1.5, 1), round(min(ys) - 1.5, 1), round(max(xs) + 1.5, 1), round(max(ys) + 1.5, 1))
        strand = sorted({pd.id for pd in b.pads() if pd.net in under and box[0] <= pd.pos[0] <= box[2] and box[1] <= pd.pos[1] <= box[3]}, key=_nat)
        f = Finding("PLANE", "ERR", "%d %s pad(s) sit over the %s plane, %.0f-%.0f mm outside the %s zone on %s: no plane under them, so no plane via within reach" % (
            len(pads), net, "/".join(under) or "no", gmin, gmax, net, "/".join(zl)),
            handles={"net": net, "plane_under": list(under), "parts": parts, "pads": [pd.id for pd, _ in pads],
                     "regions": sorted({ac.region(pd.pos) for pd, _ in pads}, key=_nat)},
            ev={"n_pads": len(pads), "gap_mm": [round(gmin, 1), round(gmax, 1)], "bbox": [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]},
            where=(sum(xs) / len(xs), sum(ys) / len(ys)), mag=min(2.0, len(pads) / 6.0))
        zn = [z for z in b.zones if z.net == net]
        lay = "/".join(zl)
        f.advice.append("reshape the split: add box (%.1f,%.1f)-(%.1f,%.1f) to the %s zone on %s and carve it out of %s%s" % (
            box[0], box[1], box[2], box[3], net, lay, "/".join(under) or "the other zone",
            "; box holds %d %s pad(s) (%s) that would then need moving or their own via" % (len(strand), "/".join(under), " ".join(strand[:6])) if strand else " (no other supply pads inside)"))
        # fix: move every affected part onto its plane region, nearest legal spot, one atomic chain
        acts = _plane_moves(ac, parts, net)
        if acts:
            f.fixes.append(Fix(acts, "move the part(s) onto the %s region" % net, "onplane"))
        if under:
            lays = sorted({l for z in zn for l in z.layers} & {l for z in b.zones if z.net in under for l in z.layers})
            bx = [max(box[0], b.bbox[0]), max(box[1], b.bbox[1]), min(box[2], b.bbox[2]), min(box[3], b.bbox[3])]
            if lays:
                ra = A.Action("reshape", net=net, mode="add", box=[round(v, 1) for v in bx], layer=lays[0])
                racts = [ra.compact()]
                with Trial(ac.w) as tr:           # the pads that now sit on their plane get their via in the same chain
                    if tr.apply(ra):
                        for pd, _g in pads:
                            pt = _via_spot(ac, pd, net)
                            if pt is not None:
                                va = A.Action("add_via", net=net, x=pt[0], y=pt[1], pad=pd.id)
                                if tr.apply(va):
                                    racts.append(va.compact())
                f.fixes.append(Fix(racts, "reshape the split: give the box to %s (carved out of %s), vias for its pads" % (net, "/".join(under)), "reshape"))
        out.append(f)
    if boxed:
        parts = sorted({pd.ref for pd in boxed}, key=_nat)
        f = Finding("PLANE", "WARN", "%d supply pad(s) inside their plane but no legal via spot within %.1f mm: %s" % (
            len(boxed), PLANE_VIA_RADIUS, " ".join(pd.id for pd in boxed[:8])),
            handles={"pads": [pd.id for pd in boxed], "parts": parts}, ev={"n_pads": len(boxed)},
            where=boxed[0].pos, mag=0.3)
        f.advice.append("nudge the part(s) off foreign copper or `unlock`/rip the blocking track, then `badpcb suggest S planes`")
        out.append(f)
    return out


def _via_spot(ac, pd, net, radius=PLANE_VIA_RADIUS):
    b = ac.b
    rl = b.rules
    obs = A.local_obstacles(b, pd.pos, radius + 2.0)
    lay = A._stub_layer(ac.w, pd)
    for r in (0.7, 1.0, 1.4, 1.9, 2.5):
        if r > radius + 1e-9:
            break
        for j in range(24):
            t = 2 * math.pi * j / 24
            pt = (round(pd.pos[0] + r * math.cos(t), 2), round(pd.pos[1] + r * math.sin(t), 2))
            if not A._in_zone(b, net, pt):
                continue
            if A._clearance_issues(ac.w, net, pt, rl.via_dia / 2, obs=obs):
                continue
            if A._clearance_issues(ac.w, net, None, 0, layers=[lay], seg=(pd.pos, pt), w=rl.track_width, obs=obs):
                continue
            return pt
    return None


def _plane_moves(ac, parts, net):
    b = ac.b
    acts = []
    with Trial(ac.w) as tr:
        for ref in parts:
            p = b.parts[ref]
            if not ac.movable(ref):
                continue
            plane_pads = [(i, d) for i, d in enumerate(p.pads) if d.net in ac.planes]

            def need(pads, pos, plane_pads=plane_pads):
                return all(_deep_in_zone(b, d.net, pads[i][0]) for i, d in plane_pads)
            anchor = p.pos
            best = None
            for rot in (p.rot, A._norm(p.rot + 180), A._norm(p.rot + 90), A._norm(p.rot + 270)):
                r = 0.5
                while r <= 40 and best is None:
                    for j in range(32):
                        a = 2 * math.pi * j / 32
                        pos = (round(anchor[0] + r * math.cos(a), 2), round(anchor[1] + r * math.sin(a), 2))
                        pads = ac.pads_at(ref, pos, rot)
                        if not need(pads, pos):
                            continue
                        if A.fits(ac.w, ref, pos, rot) and all(_via_spot_at(ac, d, pads[i][0], d.net) for i, d in plane_pads):
                            best = (r, pos, rot)
                            break
                    r += 1.0
                if best:
                    break
            if best:
                a = A.Action("move", ref=ref, x=best[1][0], y=best[1][1], **({"rot": best[2]} if abs(A._norm(best[2] - p.rot)) > 1e-6 else {}))
                if tr.apply(a):
                    acts.append(a.compact())
    return acts


def _deep_in_zone(b, net, pt, m=0.4):
    return all(A._in_zone(b, net, (pt[0] + dx, pt[1] + dy)) for dx, dy in ((0, 0), (m, 0), (-m, 0), (0, m), (0, -m)))


def _via_spot_at(ac, pd, pos, net):
    class _P:
        pass
    q = _P()
    q.pos, q.layers, q.ref, q.num, q.net, q.size, q.rot, q.through = pos, pd.layers, pd.ref, pd.num, pd.net, pd.size, pd.rot, pd.through
    try:
        return _via_spot(ac, q, net, PLANE_VIA_RADIUS) is not None
    except Exception:
        return True


# ============================================================================= COURTYARD
def detect_courtyard(ac):
    out = []
    b = ac.b
    parts = [p for p in b.parts.values() if p.pads]
    grid = defaultdict(list)
    for p in parts:
        for c in legal._cells(p.bbox):
            grid[c].append(p)
    seen = set()
    hits = []
    for p in parts:
        for c in legal._cells(p.bbox):
            for q in grid[c]:
                if q.ref >= p.ref or (p.ref, q.ref) in seen:
                    continue
                seen.add((p.ref, q.ref))
                if p.side != q.side and not (A._through(p) or A._through(q)):
                    continue
                ox = min(p.bbox[2], q.bbox[2]) - max(p.bbox[0], q.bbox[0]) - COURTYARD_BIAS
                oy = min(p.bbox[3], q.bbox[3]) - max(p.bbox[1], q.bbox[1]) - COURTYARD_BIAS
                if min(ox, oy) >= COURTYARD_TOL and not _in_notch(p, q) and not _in_notch(q, p):
                    hits.append((min(ox, oy), p, q, ox, oy))
    for d, p, q, ox, oy in sorted(hits, key=lambda h: (-h[0], h[1].ref)):
        mover, other = (p, q) if p.area <= q.area else (q, p)
        if mover.locked or not ac.movable(mover.ref, big=True):
            mover, other = other, mover
        f = Finding("COURTYARD", "ERR", "%s courtyard overlaps %s by %.2f x %.2f mm (KiCad DRC: courtyards_overlap)" % (mover.ref, other.ref, ox, oy),
                    handles={"parts": [mover.ref, other.ref], "region": ac.region(mover.pos)},
                    ev={"overlap_x": round(ox, 3), "overlap_y": round(oy, 3)}, where=mover.pos, mag=0.3)
        if ac.movable(mover.ref, big=True):
            f.fixes += _nudge_clear(ac, mover, other)
        out.append(f)
    # pad-to-pad clearance violations between different parts
    for p in parts:
        clr = ac.eff.at(b, p.bbox)[0]
        msgs = [m for m in legal.pad_conflicts(ac.w, p.ref, p.pos, p.rot, p.side, clr=clr) if m.startswith("pad ") and " from pad " in m]
        if msgs:
            other = re.search(r"from pad (\w+)\.", msgs[0])
            pair = tuple(sorted((p.ref, other.group(1) if other else "?")))
            if pair in {tuple(sorted((h[1].ref, h[2].ref))) for h in hits}:
                continue
            if p.ref != pair[0] and pair[0] != "?":
                continue            # report each pair once
            f = Finding("COURTYARD", "ERR", "pad clearance: %s" % msgs[0], handles={"parts": list(pair)}, ev={"n": len(msgs)}, where=p.pos, mag=0.5)
            if ac.movable(p.ref, big=True):
                f.fixes += _nudge_clear(ac, p, b.parts.get(pair[1]) or p, pads_only=True)
            out.append(f)
    return out


def _in_notch(big, small):
    """Big QFN/BGA courtyards have notched corners that the bbox hides: ignore overlaps that sit in a corner square."""
    if len({d.num for d in big.pads}) < 32:
        return False
    x0, y0, x1, y1 = big.bbox
    c = max(1.0, 0.12 * min(x1 - x0, y1 - y0))
    bb = small.bbox
    ox0, oy0, ox1, oy1 = max(x0, bb[0]), max(y0, bb[1]), min(x1, bb[2]), min(y1, bb[3])
    for cx0, cx1 in ((x0, x0 + c), (x1 - c, x1)):
        for cy0, cy1 in ((y0, y0 + c), (y1 - c, y1)):
            if ox0 >= cx0 - 1e-6 and ox1 <= cx1 + 1e-6 and oy0 >= cy0 - 1e-6 and oy1 <= cy1 + 1e-6:
                return True
    return False


def _true_overlap(a, b):
    """Courtyard overlap on the bias-corrected model (>0 means KiCad's DRC would flag it)."""
    ox = min(a[2], b[2]) - max(a[0], b[0]) - COURTYARD_BIAS
    oy = min(a[3], b[3]) - max(a[1], b[1]) - COURTYARD_BIAS
    return min(ox, oy)


def _hits_third_courtyard(ac, mover, other, bb_new, bb_old):
    """A nudge away from `other` must not create (or deepen) a real courtyard overlap with any third part on the same side
    (board lane: `nudge C15 0.04 -0.04` cleared U1 but overlapped C17)."""
    side = ac.b.parts[mover.ref].side
    for q in ac.b.parts.values():
        if q.ref in (mover.ref, other.ref) or not q.pads or (q.side != side and not any(d.through for d in q.pads)):
            continue
        if abs(q.pos[0] - mover.pos[0]) > 12 or abs(q.pos[1] - mover.pos[1]) > 12:
            continue
        new, old = _true_overlap(bb_new, q.bbox), _true_overlap(bb_old, q.bbox)
        if new > -0.003 and new > old + 0.002:
            return True
    return False


def _nudge_clear(ac, mover, other, pads_only=False):
    p = ac.b.parts[mover.ref]
    clr = ac.eff.at(ac.b, p.bbox)[0]
    cands = []
    for dist in [round(0.01 * x, 3) for x in range(1, 61)]:
        for ang in range(0, 360, 45):
            dx, dy = round(dist * math.cos(math.radians(ang)), 3), round(dist * math.sin(math.radians(ang)), 3)
            pos = (round(p.pos[0] + dx, 3), round(p.pos[1] + dy, 3))
            bb, _ = A.place_geom(ac.w, mover.ref, pos, p.rot, p.side)
            if not pads_only:
                ox = min(bb[2], other.bbox[2]) - max(bb[0], other.bbox[0]) - COURTYARD_BIAS
                oy = min(bb[3], other.bbox[3]) - max(bb[1], other.bbox[1]) - COURTYARD_BIAS
                if min(ox, oy) >= -0.005:
                    continue
            if not A.fits(ac.w, mover.ref, pos, p.rot):
                continue
            if any(m.startswith("pad ") and " from pad " in m for m in legal.pad_conflicts(ac.w, mover.ref, pos, p.rot, p.side, clr=clr)):
                continue
            if _hits_third_courtyard(ac, mover, other, bb, p.bbox):
                continue
            cands.append((dist, dx, dy))
        if cands:
            break
    cands.sort()
    return [Fix([A.Action("nudge", ref=mover.ref, dx=round(c[1], 2), dy=round(c[2], 2)).compact()], "smallest legal nudge", "nudge") for c in cands[:3]]


# ============================================================================= BOWTIE / ORIENT / TENSION
def _lines_cross(l1, l2):
    return geom.seg_intersect(l1[0], l1[1], l2[0], l2[1]) is not None


def detect_bowtie(ac):
    out = []
    lines = ac.lines()
    by_pair = defaultdict(list)
    for ln in lines:
        net, pa, pb, ra, rb = ln[:5]
        if ra == rb:
            continue
        by_pair[tuple(sorted((ra, rb)))].append(ln)
    for (ra, rb), L in sorted(by_pair.items()):
        nets = sorted({l[0] for l in L})
        if len(nets) < 2:
            continue
        xs = [(l1, l2) for i, l1 in enumerate(L) for l2 in L[i + 1:] if l1[0] != l2[0] and _lines_cross((l1[1], l1[2]), (l2[1], l2[2]))]
        if not xs:
            continue
        pa_, pb_ = ac.b.parts[ra], ac.b.parts[rb]
        cands = sorted([pa_, pb_], key=lambda p: p.area)
        mover = next((p for p in cands if ac.movable(p.ref, big=True) and not ac.routed_part(p.ref)), None)
        if mover is None:
            continue
        f = Finding("BOWTIE", "WARN", "%s <-> %s: %d nets (%s) with %d crossing ratsnest line(s)" % (ra, rb, len(nets), " ".join(nets[:5]), len(xs)),
                    handles={"parts": [ra, rb], "nets": nets, "region": ac.region(mover.pos)},
                    ev={"crossings": len(xs), "nets": len(nets)}, where=mover.pos, mag=min(2.0, len(xs) / 3.0))
        for d in (180, 90, 270):
            f.fixes.append(Fix([A.Action("rotate", ref=mover.ref, deg=A._norm(mover.rot + d), rel=False).compact()], "rotate %s by %d" % (mover.ref, d), "rot"))
        if len(mover.pads) <= 8:
            f.fixes.append(Fix([A.Action("flip", ref=mover.ref).compact()], "flip %s to the other side" % mover.ref, "flip"))
        out.append(f)
    return out


def detect_orient(ac):
    out = []
    b = ac.b
    lines = ac.lines()
    bbs = [(min(l[1][0], l[2][0]), min(l[1][1], l[2][1]), max(l[1][0], l[2][0]), max(l[1][1], l[2][1])) for l in lines]
    for ref, p in sorted(b.parts.items()):
        if len(p.pads) != 2 or p.locked or ac.routed_part(ref) or not ac.is_passive(p):
            continue
        own = [(i, l) for i, l in enumerate(lines) if ref in (l[3], l[4]) and l[3] != l[4]]
        if not own:
            continue
        num_of = {d.num: k for k, d in enumerate(p.pads)}

        def cost(pads):
            tot, nx = 0.0, 0
            for i, l in own:
                mine_a = l[3] == ref
                pid = l[5] if mine_a else l[6]
                other = l[2] if mine_a else l[1]
                k = num_of.get(pid.split(".", 1)[1])
                if k is None:
                    continue
                pt = pads[k][0]
                tot += geom.dist(pt, other)
                seg = (pt, other)
                sb = (min(pt[0], other[0]), min(pt[1], other[1]), max(pt[0], other[0]), max(pt[1], other[1]))
                for j, o in enumerate(lines):
                    if o[0] == l[0] or ref in (o[3], o[4]):
                        continue
                    ob = bbs[j]
                    if ob[0] > sb[2] or ob[2] < sb[0] or ob[1] > sb[3] or ob[3] < sb[1]:
                        continue
                    if geom.seg_intersect(seg[0], seg[1], o[1], o[2]):
                        nx += 1
            return tot, nx
        cur_len, cur_x = cost([(d.pos, d.rot, d.layers) for d in p.pads])
        best = None
        for d in (180, 90, 270):
            rot = A._norm(p.rot + d)
            ln_, x_ = cost(ac.pads_at(ref, p.pos, rot))
            gain = (cur_x - x_) * 4.0 + (cur_len - ln_)
            if (cur_x - x_ >= 1 or cur_len - ln_ >= max(1.0, 0.2 * cur_len)) and (best is None or gain > best[0]):
                best = (gain, d, cur_x - x_, cur_len - ln_)
        if best is None:
            continue
        gain, d, dx, dl = best
        nets = sorted({l[0] for _, l in own})
        f = Finding("ORIENT", "NOTE", "%s rotated %d would cut its own connections: crossings %+d, length %+.1f mm (nets %s)" % (ref, d, -dx, -dl, " ".join(nets)),
                    handles={"part": ref, "nets": nets, "region": ac.region(p.pos)}, ev={"cross_gain": dx, "len_gain_mm": round(dl, 2), "rot": d},
                    where=p.pos, mag=min(1.0, gain / 8.0))
        f.fixes.append(Fix([A.Action("rotate", ref=ref, deg=A._norm(p.rot + d), rel=False).compact()], "rotate %d" % d, "rot"))
        out.append(f)
    return out


def _weiszfeld(pts, it=40):
    x = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    for _ in range(it):
        num, den = [0.0, 0.0], 0.0
        for p in pts:
            d = max(1e-6, geom.dist(x, p))
            num[0] += p[0] / d
            num[1] += p[1] / d
            den += 1 / d
        nx = (num[0] / den, num[1] / den)
        if geom.dist(nx, x) < 1e-4:
            x = nx
            break
        x = nx
    return x


def detect_tension(ac):
    out = []
    b = ac.b
    lines = ac.lines()
    nb = defaultdict(list)
    for net, pa, pb, ra, rb, ia, ib in lines:
        if ra == rb:
            continue
        nb[ra].append((net, pb, ib))
        nb[rb].append((net, pa, ia))
    rows = []
    for ref, L in nb.items():
        p = b.parts[ref]
        if p.locked or not ac.movable(ref) or ac.routed_part(ref) or re.match(r"^(SW|TP|J|H|MH|FID)", ref):
            continue
        pts = [q for _, q, _ in L]
        m = _weiszfeld(pts)
        cen = (sum(d.pos[0] for d in p.pads) / len(p.pads), sum(d.pos[1] for d in p.pads) / len(p.pads))
        cur = sum(geom.dist(cen, q) for q in pts)
        best = sum(geom.dist(m, q) for q in pts)
        d = geom.dist(cen, m)
        excess = cur - best
        spread = best / len(pts)
        rows.append((excess, d, spread, ref, m, L))
    for excess, d, spread, ref, m, L in sorted(rows, key=lambda r: -r[0]):
        if excess < 5.0 or d < 5.0:
            continue
        p = b.parts[ref]
        tn = excess / (spread + 3.0)
        if tn < 1.0:
            continue
        nets = sorted({n for n, _, _ in L})
        pid = next((pid for pid_ in [p.pads[0]] for pid in [pid_.num]), p.pads[0].num)
        f = Finding("TENSION", "NOTE" if excess < 15 else "WARN",
                    "%s sits %.1f mm from the median of its %d connections (%.1f mm of wire to spare, tension %.1f); target near (%.1f, %.1f)" % (
                        ref, d, len(L), excess, tn, m[0], m[1]),
                    handles={"part": ref, "nets": nets, "target": [round(m[0], 1), round(m[1], 1)], "region": ac.region(p.pos)},
                    ev={"dist_to_median": round(d, 1), "excess_mm": round(excess, 1), "tension": round(tn, 2), "n_conn": len(L)},
                    where=p.pos, mag=min(1.5, tn / 4.0))
        pad_num = _best_pad_toward(p, m)
        for s in ac.legal_spots(ref, m, pad_num, 0.0, 6.0, keep=2):
            f.fixes.append(Fix([mv_spot(p, s)], "legal spot %.1f mm from the median" % s[0], "spot"))
        f.advice.append("`badpcb spots S %s %.1f %.1f` lists more" % (ref, m[0], m[1]))
        out.append(f)
    return out


def _best_pad_toward(p, pt):
    return min(p.pads, key=lambda d: geom.dist(d.pos, pt)).num


# ============================================================================= ROLES
class Role:
    def __init__(self, ref, kind, anchor="", anchor_pt=None, dist=None, limit=None, net="", note=""):
        self.ref, self.kind, self.anchor, self.anchor_pt, self.dist, self.limit, self.net, self.note = ref, kind, anchor, anchor_pt, dist, limit, net, note

    def as_dict(self):
        return {"ref": self.ref, "kind": self.kind, "anchor": self.anchor, "dist": None if self.dist is None else round(self.dist, 2),
                "limit": self.limit, "net": self.net, "note": self.note}


def _ctx_roles(self):
    if self._roles is None:
        self._roles = infer_roles(self)
    return self._roles


Ctx.roles = _ctx_roles


def infer_roles(ac):
    b = ac.b
    netpads = defaultdict(list)
    for d in b.pads():
        if d.net:
            netpads[d.net].append(d)
    mcu = ac.w.cfg["mcu"]
    K = params()
    roles = {}

    def ic_pads(net, exclude=None):
        return [d for d in netpads.get(net, []) if d.ref != exclude and ac.is_ic(b.parts[d.ref])]

    def nearest(pt, pads):
        return min(pads, key=lambda d: geom.dist(d.pos, pt)) if pads else None

    for ref, p in sorted(b.parts.items(), key=lambda kv: _nat(kv[0])):
        if ref.startswith("SW") or not p.pads:
            continue
        nums = {d.num for d in p.pads}
        two = len(nums) == 2 and ac.is_passive(p)
        if ref.startswith("Y") or "Crystal" in p.fpid:
            ics = [(d, nearest(d.pos, [q for q in ic_pads(d.net, ref)])) for d in p.pads if d.net and not ac.is_gnd(d.net)]
            ics = [(d, q) for d, q in ics if q]
            if ics:
                dist = max(geom.dist(d.pos, q.pos) for d, q in ics)
                roles[ref] = Role(ref, "crystal", "/".join(q.id for _, q in ics), ics[0][1].pos, dist, K["crystal_mm"], note="to %s" % ics[0][1].ref)
            continue
        if not two:
            continue
        pads = {d.num: d for d in p.pads}
        pl = list(pads.values())
        nets = [d.net for d in pl]
        if ref.startswith("C"):
            g = [d for d in pl if ac.is_gnd(d.net)]
            if len(g) != 1:
                continue
            sig = [d for d in pl if d is not g[0]][0]
            X = sig.net
            val = farads(p.value)
            if ac.is_supply(X):
                bulk = val is not None and val > K["bulk_farads"]
                ind = [d for d in netpads.get(X, []) if d.ref.startswith("L") and not d.ref.startswith("LED")]
                if ind and bulk:
                    q = nearest(sig.pos, ic_pads(X) + ind)
                    roles[ref] = Role(ref, "reg_cap", q.id if q else "", q.pos if q else None, geom.dist(sig.pos, q.pos) if q else None, K["reg_cap_mm"], X,
                                      "regulator output/input cap at the inductor or IC pin")
                    continue
                anchors = ic_pads(X, ref) + ([d for d in netpads.get(X, []) if d.ref.startswith("J")] if bulk else [])
                q = nearest(sig.pos, anchors)
                if q:
                    roles[ref] = Role(ref, "decap", q.id, q.pos, geom.dist(sig.pos, q.pos), K["decap_bulk_mm"] if bulk else K["decap_bypass_mm"], X, "bulk" if bulk else "bypass")
                continue
            if X in ("", None):
                continue
            xt = [d for d in netpads.get(X, []) if d.ref.startswith("Y") or "Crystal" in b.parts[d.ref].fpid]
            if xt:
                q = nearest(sig.pos, xt)
                roles[ref] = Role(ref, "xtal_load", q.id, q.pos, geom.dist(sig.pos, q.pos), K["xtal_load_mm"], X, "crystal load cap")
                continue
            q = nearest(sig.pos, ic_pads(X, ref))
            if q:
                roles[ref] = Role(ref, "filter_cap", q.id, q.pos, geom.dist(sig.pos, q.pos), K["filter_cap_mm"], X, "signal filter cap at the IC pin")
            else:
                # cap on an RC node: follow one resistor hop to the IC pin
                for r2 in netpads.get(X, []):
                    if r2.ref.startswith("R") and len(b.parts[r2.ref].pads) == 2:
                        other = [d for d in b.parts[r2.ref].pads if d is not r2]
                        if other and ic_pads(other[0].net):
                            q = nearest(sig.pos, ic_pads(other[0].net))
                            roles[ref] = Role(ref, "filter_cap", q.id, q.pos, geom.dist(sig.pos, q.pos), K["filter_cap_rc_mm"], X, "RC node via %s" % r2.ref)
                            break
        elif ref.startswith("R"):
            a, c = pl
            for me, ot in ((a, c), (c, a)):
                if ac.is_supply(ot.net) and ac.is_signal(me.net):
                    q = nearest(me.pos, ic_pads(me.net))
                    if q:
                        roles[ref] = Role(ref, "pullup", q.id, q.pos, geom.dist(me.pos, q.pos), K["pull_mm"], me.net, "pull-up to %s" % ot.net)
                    break
                if ac.is_gnd(ot.net) and ac.is_signal(me.net):
                    q = nearest(me.pos, ic_pads(me.net))
                    if q:
                        roles[ref] = Role(ref, "pulldown", q.id, q.pos, geom.dist(me.pos, q.pos), K["pull_mm"], me.net, "pull-down")
                    break
            else:
                if ac.is_signal(a.net) and ac.is_signal(c.net):
                    for me in (a, c):
                        q = nearest(me.pos, ic_pads(me.net))
                        if q:
                            roles[ref] = Role(ref, "series_R", q.id, q.pos, geom.dist(me.pos, q.pos), None, me.net, "series R into %s (no distance rule)" % q.id)
                            break
        elif ref.startswith("D"):
            g = [d for d in pl if ac.is_gnd(d.net)]
            if g:
                sig = [d for d in pl if d is not g[0]][0]
                conn = [d for d in netpads.get(sig.net, []) if d.ref.startswith("J") or "USB" in d.ref]
                q = nearest(sig.pos, conn)
                if q:
                    roles[ref] = Role(ref, "esd", q.id, q.pos, geom.dist(sig.pos, q.pos), K["esd_mm"], sig.net, "ESD/TVS at the connector")
        elif ref.startswith("L"):
            q = nearest(pl[0].pos, [d for d in netpads.get(pl[0].net, []) + netpads.get(pl[1].net, []) if ac.is_ic(b.parts[d.ref])])
            roles[ref] = Role(ref, "inductor", q.id if q else "", q.pos if q else None, geom.dist(pl[0].pos, q.pos) if q else None, None, pl[0].net, "regulator inductor")
    return roles


def role_violation(r):
    K = params()
    if r.limit is None or r.dist is None or r.dist <= r.limit * K["violation_slack"]:
        return None
    if r.kind in ("pullup", "pulldown"):
        return "NOTE"
    return "ERR" if r.dist > K["err_factor"] * r.limit else "WARN"




def detect_roles(ac):
    out = []
    b = ac.b
    groups = defaultdict(list)
    for ref, r in ac.roles().items():
        sev = role_violation(r)
        if sev and not ac.routed_part(ref):
            groups[(r.kind, r.anchor.split(".")[0] if r.anchor else "", sev)].append(r)
    for (kind, anc, sev), L in sorted(groups.items(), key=lambda kv: (SEV_ORD[kv[0][2]], kv[0][0])):
        L.sort(key=lambda r: -(r.dist - r.limit))
        txt = ", ".join("%s %.1f/%g mm to %s" % (r.ref, r.dist, r.limit, r.anchor) for r in L[:5]) + (" ..." if len(L) > 5 else "")
        f = Finding("ROLES", sev, "%s too far from its %s: %s" % (kind, "pin" if kind != "esd" else "connector", txt),
                    handles={"role": kind, "parts": [r.ref for r in L], "nets": sorted({r.net for r in L}), "anchor": anc,
                             "region": ac.region(b.parts[L[0].ref].pos)},
                    ev={"n": len(L), "worst_excess_mm": round(max(r.dist - r.limit for r in L), 1)}, where=b.parts[L[0].ref].pos,
                    mag=min(2.0, max(r.dist - r.limit for r in L) / 5.0))
        acts = []
        with Trial(ac.w) as tr:
            zones = escape_zones(ac, {anc}) if anc else []
            avoid = [z.rect for z in zones]
            for r in L[:4]:
                if not ac.movable(r.ref):
                    continue
                p = b.parts[r.ref]
                pad = [d for d in p.pads if d.net == r.net]
                if not pad or r.anchor_pt is None:
                    continue
                dmax = max(r.limit, (max(z.D for z in zones) + 1.0) if zones else 0.0)
                spots = ac.legal_spots(r.ref, r.anchor_pt, pad[0].num, 0.4, dmax, avoid, keep=1)
                if not spots:
                    continue
                s = spots[0]
                a = A.Action("move", ref=r.ref, x=s[1], y=s[2], **({"rot": s[3]} if abs(A._norm(s[3] - p.rot)) > 1e-6 else {}))
                if tr.apply(a):
                    acts.append(a.compact())
        if acts:
            f.fixes.append(Fix(acts, "back to the pin, outside the fan zone" if zones else "back next to the anchor", "near"))
            if len(acts) > 1:
                f.fixes.append(Fix(acts[:1], "worst one only", "near1"))
        else:
            f.advice.append("no legal spot within the rule distance (fan zone / crowding); move something else first")
        out.append(f)
    return out


# ============================================================================= INTENT
def _intent_fix_move(ac, it, C, ref, center, rmax):
    """Move `ref` to the nearest legal spot to `center` where this entry no longer penalises it and the other entries that involve the
    part do not get worse (the part's courtyard is checked against the others as placed)."""
    w, b = ac.w, ac.b
    bbf = lambda bb: (lambda r: bb if r == ref else b.parts[r].bbox)
    here = b.parts[ref].bbox
    budget = IN.part_cost(w, ref, here) - IN.eval_item(w, C, it, bbf(here), only=ref)[0] + 1e-9
    ok = lambda bb, pos, rot: IN.eval_item(w, C, it, bbf(bb), only=ref)[0] <= 1e-9 and IN.part_cost(w, ref, bb) <= budget
    return ac.ring_spot(ref, center, ok, rmax=rmax)


def _spot_act(p, s):
    return mv(p.ref, s[0], s[1], s[2] if abs(A._norm(s[2] - p.rot)) > 1e-6 else None)


def _intent_movable(ac, ref):
    p = ac.b.parts.get(ref)
    return bool(p) and not p.locked and ref != ac.w.cfg["mcu"] and len(p.pads) <= 20 and p.area < 150


def detect_intent(ac):
    """Violated design intents (badpcb.json "intent" / `intent add`): one finding per entry with its worst offenders, WARN unless the
    weighted penalty reaches params()["intent_err"]. Reference drift and block spread are findings of their block."""
    w, b = ac.w, ac.b
    if not w.intent:
        return []
    K = params()
    C = IN.compiled(w)
    out = []
    for msg in C.warnings:
        out.append(Finding("INTENT", "NOTE", "intent: " + msg, handles={"role": "intent"}, ev={"warning": msg}, mag=0.0))
    for it, pen, viol in IN.report(w):
        if pen < 0.05:
            continue
        e, k = it["e"], it["kind"]
        sev = "ERR" if pen >= K["intent_err"] else "WARN"
        worst = sorted([v for v in viol if v.get("part")], key=lambda v: -v["pen"])
        parts = [v["part"] for v in worst]
        h = {"role": "intent", "intent": it["id"], "parts": parts}
        ev = {"id": it["id"], "kind": k, "penalty": round(pen, 1), "weight": it["w"], "n": len(viol)}
        fixes, advice = [], []
        where = b.parts[parts[0]].pos if parts else None
        if k == "near":
            txt = ", ".join("%s %.1f/%g mm to %s" % (v["part"], v["have"], v["limit"], e["b"]) for v in worst[:5]) + (" ..." if len(worst) > 5 else "")
            title = "near %s: %s" % (e["b"], txt)
            h["nets"] = []
            movers = [v for v in worst if _intent_movable(ac, v["part"]) and v["part"] != it["B"]]
        elif k == "apart":
            title = "apart: " + ", ".join("%s %.1f/%g mm from %s" % (v["part"], v["have"], v["limit"], v["other"]) for v in worst[:5])
            movers = [v for v in worst if _intent_movable(ac, v["part"])]
        elif k == "edge":
            title = "edge %s: " % e["side"] + ", ".join("%s %.1f mm from the edge (max %g)" % (v["part"], v["have"], v["limit"]) for v in worst[:5])
            movers = [v for v in worst if _intent_movable(ac, v["part"])]
        elif k == "clear":
            z = e["zone"] if isinstance(e["zone"], str) else "box %s" % ",".join("%g" % x for x in e["zone"])
            title = "clear %s: %d part(s) inside: %s" % (z, len(viol), ", ".join("%s %.1f mm2" % (v["part"], v["amount"]) for v in worst[:6]))
            h["intruders"] = parts
            movers = [v for v in worst if _intent_movable(ac, v["part"])]
            for v in worst:
                if not _intent_movable(ac, v["part"]):
                    advice.append("%s is locked/large: move it by hand (courtyard %.1f mm2 inside %s)" % (v["part"], v["amount"], z))
        else:                                                                            # block: span and/or reference drift
            sp = [v for v in viol if v.get("kind") == "span"]
            dr = [v for v in viol if v.get("kind") == "drift"]
            bits = []
            if sp:
                bits.append("spread %.1f mm (max %g)" % (sp[0]["have"], sp[0]["limit"]))
            if dr:
                d = dr[0]["drift"]
                bits.append("%.1f mm RMS off the reference%s" % (d["rms"], ", %d part(s) turned" % d["rot"] if d["rot"] else ""))
            title = "block %s: %s" % (e["name"], "; ".join(bits))
            h["role"] = "intent"
            movers = []
            acts = []
            if dr:
                d = dr[0]["drift"]
                ordered = sorted(d["parts"].items(), key=lambda kv: -(kv[1][0] + 2.0 * kv[1][3]))
                parts = [r for r, v in ordered if v[0] > 0.5 or v[3]]
                h["parts"] = parts
                where = b.parts[parts[0]].pos if parts else None
                with Trial(ac.w) as tr:
                    for r, (off, pos, erot, miss) in ordered[:8]:
                        if (off <= 0.5 and not miss) or not _intent_movable(ac, r) or r == it["anchor"]:
                            continue
                        p = b.parts[r]
                        rots = [min((p.rot, p.rot + 90, p.rot + 180, p.rot + 270), key=lambda q: (IN._rot_miss(q, erot), abs(A._norm(q - p.rot))))]
                        sp_ = ac.ring_spot(r, pos, lambda bb, pp, rr: True, rmax=3.0, rots=rots)
                        if sp_ is None:
                            continue
                        a = A.Action("move", ref=r, x=sp_[0], y=sp_[1], **({"rot": sp_[2]} if abs(A._norm(sp_[2] - p.rot)) > 1e-6 else {}))
                        if tr.apply(a):
                            acts.append(a.compact())
                if acts:
                    fixes.append(Fix(acts, "members to their reference positions", "ref"))
                    if len(acts) > 1:
                        fixes.append(Fix(acts[:1], "worst one only", "ref1"))
            if sp and not fixes:
                A_ = [r for r in it["A"] if r != it["anchor"] and _intent_movable(ac, r)]
                if A_:
                    cx = sum(b.parts[r].pos[0] for r in it["A"]) / len(it["A"])
                    cy = sum(b.parts[r].pos[1] for r in it["A"]) / len(it["A"])
                    far = max(A_, key=lambda r: geom.dist(b.parts[r].pos, (cx, cy)))
                    sp_ = ac.ring_spot(far, (cx, cy), lambda bb, pp, rr: True, rmax=(e["max_span_mm"] or 10) / 2 + 3.0)
                    if sp_:
                        fixes.append(Fix([_spot_act(b.parts[far], sp_)], "farthest member back toward the block centre", "span"))
                        h["parts"] = [far]
            if not fixes:
                advice.append("no legal move found; the block members are boxed in or locked")
            f = Finding("INTENT", sev, title, handles=h, ev=ev, fixes=fixes, where=where, mag=min(2.0, pen / K["intent_err"]))
            f.advice = advice
            out.append(f)
            continue
        # near / apart / edge / clear: nearest legal spot (to where the part is now) where the entry stops penalising it
        acts = []
        with Trial(ac.w) as tr:
            for v in movers[:4]:
                p = b.parts[v["part"]]
                s_ = _intent_fix_move(ac, it, C, p.ref, p.pos, rmax=min(60.0, 3.0 + 2.5 * max(v.get("amount", 0.0), 1.0) + (v.get("have", 0.0) if k == "near" else 0.0)))
                if s_ is None:
                    continue
                a = A.Action("move", ref=p.ref, x=s_[0], y=s_[1], **({"rot": s_[2]} if abs(A._norm(s_[2] - p.rot)) > 1e-6 else {}))
                if tr.apply(a):
                    acts.append(a.compact())
        if acts:
            fixes.append(Fix(acts, "nearest legal spot that satisfies the intent", "intent"))
            if len(acts) > 1:
                fixes.append(Fix(acts[:1], "worst one only", "intent1"))
        elif not advice:
            advice.append("no legal spot satisfies this intent for the worst parts (crowding); relax the entry (intent rm %s) or move something else first" % it["id"])
        f = Finding("INTENT", sev, title, handles=h, ev=ev, fixes=fixes, where=where, mag=min(2.0, pen / K["intent_err"]))
        f.advice = advice
        out.append(f)
    return out


def sec_roles(ac):
    rs = ac.roles()
    L = ["thresholds (badpcb.json \"audit\" overrides): " + " ".join("%s=%g" % kv for kv in params().items()),
         "inferred roles (%d parts); limit = max centre-to-anchor distance of the role rule; edit the driver's mental model if wrong:" % len(rs)]
    by = defaultdict(list)
    for r in rs.values():
        by[r.kind].append(r)
    for kind in sorted(by):
        items = sorted(by[kind], key=lambda r: _nat(r.ref))
        L.append("  %-10s %d: %s" % (kind, len(items), "  ".join(
            "%s>%s %s%s" % (r.ref, r.anchor or "-", "-" if r.dist is None else "%.1f" % r.dist,
                            "" if r.limit is None else ("/%g%s" % (r.limit, "!" if role_violation(r) else ""))) for r in items)))
    return L


# ============================================================================= BUSORDER
def _tangent_inv(ac, A_ref, B_ref, pairs):
    """pairs [(net, pad_on_A, pad_on_B)]: per side group of A, inversions between edge order and partner order."""
    out = []
    a = ac.b.parts[A_ref]
    by_side = defaultdict(list)
    for net, pa, pb in pairs:
        by_side[swaps._side(a, pa.pos)].append((net, pa, pb))
    for side, L in by_side.items():
        if len(L) < 3:
            continue
        ax = 1 if side in "WE" else 0
        P = sorted(L, key=lambda t: t[1].pos[ax])
        order_a = [t[0] for t in P]
        inv = []
        for i in range(len(P)):
            for j in range(i + 1, len(P)):
                da = P[i][1].pos[ax] - P[j][1].pos[ax]
                db = P[i][2].pos[ax] - P[j][2].pos[ax]
                if da * db < 0 and abs(db) > 0.3:
                    inv.append((P[i][0], P[j][0]))
        out.append((side, order_a, inv))
    return out


def detect_busorder(ac):
    out = []
    b = ac.b
    mcu = ac.w.cfg["mcu"]
    # pairs sharing >= 3 non-plane nets
    share = defaultdict(lambda: defaultdict(list))
    netpads = defaultdict(list)
    for d in b.pads():
        if d.net and ac.is_signal(d.net):
            netpads[d.net].append(d)
    for net, pl in netpads.items():
        refs = defaultdict(list)
        for d in pl:
            refs[d.ref].append(d)
        keys = [r for r in refs if ac.is_ic(b.parts[r]) or r.startswith("J") or r.startswith("U")]
        for i, ra in enumerate(sorted(keys)):
            for rb in sorted(keys)[i + 1:]:
                share[(ra, rb)][net] = (refs[ra][0], min(refs[rb], key=lambda d: geom.dist(d.pos, refs[ra][0].pos)))
    for (ra, rb), nets in sorted(share.items()):
        if len(nets) < 3:
            continue
        for X, Y, flip in ((ra, rb, False), (rb, ra, True)):
            prs = [(n, (pa if not flip else pb), (pb if not flip else pa)) for n, (pa, pb) in nets.items()]
            for side, order, inv in _tangent_inv(ac, X, Y, prs):
                if len(inv) < 2:
                    continue
                f = Finding("BUSORDER", "WARN", "%s.%s <-> %s: %d nets leave in pin order %s, partner order differs: %d inversion(s) (%s)" % (
                    X, side, Y, len(order), " ".join(order[:8]), len(inv), " ".join("%sx%s" % q for q in inv[:4])),
                    handles={"part": X, "partner": Y, "side": side, "nets": order, "region": ac.region(b.parts[X].pos)},
                    ev={"inversions": len(inv), "nets": len(order)}, where=b.parts[X].pos, mag=min(2.0, len(inv) / 4.0))
                f.fixes += _bus_fixes(ac, X, Y, order, inv)
                out.append(f)
    # MCU swap groups: pin order vs arrival order on the ring (same view as the report's u1 section)
    if mcu in b.parts:
        irn = ac.irn
        for gid, G in ac.w.groups().items():
            if G.kind not in ("pin_permutation", "pin_assignment"):
                continue
            try:
                v = swaps.pin_vs_arrival(b, irn, G, ac.w.cfg)
            except (KeyError, IndexError):
                continue
            n_over = len(v["over_package"])
            if v["inversions"] < 2 and n_over < 2:
                continue
            f = Finding("BUSORDER", "WARN" if v["inversions"] >= 4 or n_over >= 3 else "NOTE",
                        "%s group '%s': pin order vs arrival order has %d inversion(s) (%s); %d/%d nets arrive from another side than their pin" % (
                            mcu, gid, v["inversions"], " ".join("%sx%s" % q for q in v["pairs"][:4]), n_over, len(v["pin_order"])),
                        handles={"part": mcu, "group": gid, "nets": [n for n, _, _ in v["pin_order"]], "over_package": v["over_package"],
                                 "region": ac.region(b.parts[mcu].pos)},
                        ev={"inversions": v["inversions"], "over_package": n_over}, where=b.parts[mcu].pos,
                        mag=min(2.0, v["inversions"] / 6.0 + n_over / 6.0))
            fx = []
            for x, y in v["pairs"][:10]:
                fx.append(Fix([A.Action("swap_pins", group=gid, a=x, b=y).compact()], "swap an inverted pair", "swap"))
            try:
                s = swaps.suggest(b, irn, G, ac.w.cfg)
                if s["assignment"]:
                    acts = [a.compact() for a in EV.realize(G, s["assignment"])]
                    if acts:
                        fx.append(Fix(acts, "monotone assignment (angular mismatch %d -> %d deg)" % (s["cost_before_deg"], s["cost_after_deg"]), "assign"))
            except (KeyError, IndexError):
                pass
            f.fixes += fx
            out.append(f)
    return out


def _bus_fixes(ac, X, Y, order, inv):
    out = []
    mov = [r for r in (X, Y) if ac.movable(r, big=True) and not ac.routed_part(r)]
    for r in sorted(mov, key=lambda r: ac.b.parts[r].area)[:1]:
        p = ac.b.parts[r]
        for d in (180, 90, 270):
            out.append(Fix([A.Action("rotate", ref=r, deg=A._norm(p.rot + d), rel=False).compact()], "rotate %s %d" % (r, d), "rot"))
    for gid, G in ac.w.groups().items():
        if G.kind in ("pin_permutation", "pin_assignment") and X == ac.w.cfg["mcu"]:
            for x, y in inv[:6]:
                if x in G.nets and y in G.nets:
                    out.append(Fix([A.Action("swap_pins", group=gid, a=x, b=y).compact()], "swap group %s" % gid, "swap"))
    return out


# ============================================================================= DENSITY
def detect_density(ac):
    out = []
    T = ac.skel
    lines = ac.lines()
    use = {}
    for cid, c in T.cuts.items():
        if c.cap_total <= 0:
            continue
        n = 0
        bb = (min(c.p0[0], c.p1[0]), min(c.p0[1], c.p1[1]), max(c.p0[0], c.p1[0]), max(c.p0[1], c.p1[1]))
        for l in lines:
            if max(l[1][0], l[2][0]) < bb[0] or min(l[1][0], l[2][0]) > bb[2] or max(l[1][1], l[2][1]) < bb[1] or min(l[1][1], l[2][1]) > bb[3]:
                continue
            if geom.seg_intersect(l[1], l[2], c.p0, c.p1):
                n += 1
        use[cid] = (n, c.cap_total)
    pins = defaultdict(int)
    for d in ac.b.pads():
        if ac.is_signal(d.net):
            pins[ac.region(d.pos)] += 1
    rows = []
    for rid, R in T.regions.items():
        area = (R.bbox[2] - R.bbox[0]) * (R.bbox[3] - R.bbox[1]) / 100.0
        cs = [(cid, c) for cid, c in T.cuts.items() if not c.label and rid in (c.a, c.b) and cid in use]
        if not cs:
            continue
        worst = max(cs, key=lambda kv: use[kv[0]][0] / max(1, use[kv[0]][1]))
        n, cap = use[worst[0]]
        rows.append((n / max(1, cap), rid, pins[rid] / area, worst[0], n, cap))
    for cid, c in T.cuts.items():
        if c.label and cid in use:           # escape ring cuts: "U1 ring N"
            n, cap = use[cid]
            if n / max(1, cap) >= 0.7:
                reg = ac.region(c.mid)
                out.append(Finding("DENSITY", "WARN" if n > cap else "NOTE", "%s (%s): %d nets cross the ring / capacity %d tracks (%.0f%%)" % (c.label, cid, n, cap, 100 * n / max(1, cap)),
                                   handles={"cut": cid, "region": reg, "label": c.label}, ev={"demand": n, "cap": cap}, where=c.mid, mag=min(1.5, n / max(1, cap) - 0.5)))
    dens_all = sorted(r[2] for r in rows if r[2] > 0)
    med = dens_all[len(dens_all) // 2] if dens_all else 0.0
    for u, rid, dens, cid, n, cap in sorted(rows, key=lambda r: -r[2])[:3]:
        if (dens >= 10 and dens >= 3 * max(med, 0.5)) or u >= 0.85:
            reg = T.regions[rid]
            out.append(Finding("DENSITY", "WARN" if (u > 1.0 or (u >= 0.5 and dens >= 10)) else "NOTE",
                               "region %s: %.0f signal pins/cm2 (board median %.1f), tightest cut %s demand %d / capacity %d tracks (%.0f%%); parts %s" % (
                                   rid, dens, med, cid, n, cap, 100 * u, ",".join(reg.parts) or "-"),
                               handles={"region": rid, "cut": cid, "parts": reg.parts}, ev={"pins_cm2": round(dens, 1), "median": round(med, 1), "demand": n, "cap": cap},
                               where=((reg.bbox[0] + reg.bbox[2]) / 2, (reg.bbox[1] + reg.bbox[3]) / 2), mag=min(1.5, max(0.0, u - 0.3) + dens / 40.0)))
    return out


# ============================================================================= driver
def _nat(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


DETECTORS = {"ESCAPE": detect_escape, "PLANE": detect_plane, "BOWTIE": detect_bowtie, "ORIENT": detect_orient, "TENSION": detect_tension,
             "ROLES": detect_roles, "INTENT": detect_intent, "BUSORDER": detect_busorder, "COURTYARD": detect_courtyard, "DENSITY": detect_density}


class Report:
    def __init__(self, ac, findings, secs, name):
        self.ac, self.findings, self.secs, self.name = ac, findings, secs, name
        self.groute = None

    @property
    def score(self):
        return sum(f.weight for f in self.findings)

    def counts(self):
        c = defaultdict(int)
        for f in self.findings:
            c[f.sev] += 1
        return c


def _rank(f):
    return (SEV_ORD[f.sev], DETS.index(f.det), -f.mag)


def _improves(f, fix):
    """Does the predicted delta justify reporting this fix for its detector?"""
    r = fix.res
    if not r or not r.ok:
        return False
    if f.det in ("BOWTIE", "ORIENT", "TENSION"):
        return r.dscore < -0.05
    if f.det == "BUSORDER":
        return r.dscore < 0
    if f.det == "INTENT":
        return r.d.get("intent", 0) < -0.05
    if f.det == "ESCAPE":
        return (fix.gain or 0) > 0.01 or (fix.tag == "swap" and r.dscore < -0.3)
    return True


def _prune(f):
    if f.det == "ESCAPE":
        f.fixes = [x for x in f.fixes if x.tag != "swap" or (x.res and x.res.ok and x.res.dscore < -0.3)]


def _eval_fixes(ac, f, budget):
    seen = set()
    fx = []
    for x in f.fixes:
        if x.spec in seen:
            continue
        seen.add(x.spec)
        fx.append(x)
    f.fixes = fx
    n = 0
    for x in f.fixes:
        if ac.n_eval >= budget or n >= 8:
            break
        ac.predict(x)
        n += 1
    f.fixes = [x for x in f.fixes if x.res is not None]
    _prune(f)
    if f.det == "ESCAPE":
        f.fixes.sort(key=lambda x: (not x.ok, not (x.gain is not None and x.gain > 0.01) and not (x.tag == "swap" and x.res.dscore < -0.3),
                                    -(x.gain or 0.0), x.res.dscore))
    else:
        f.fixes.sort(key=lambda x: (not x.ok, not _improves(f, x), x.res.dscore if x.res else 0.0))


GSCORE = 0.0          # seconds of groute re-ranking (badpcb suggest S audit --gscore); 0 = off


def run(st, only=None, rules_pro=None, name=None, budget=MAX_EVALS, gscore=None):
    t0 = time.time()
    gscore = GSCORE if gscore is None else gscore
    ac = Ctx(st, rules_pro)
    ac.roles()
    only = [x.upper() for x in only] if only else list(DETS)
    findings = []
    times = {}
    for det in DETS:
        if det not in only:
            continue
        t1 = time.time()
        findings += DETECTORS[det](ac)
        times[det] = time.time() - t1
    findings.sort(key=_rank)
    keep = []
    for f in findings:
        _eval_fixes(ac, f, budget)
        if f.det in ("BOWTIE", "ORIENT", "TENSION", "BUSORDER"):
            f.fixes = [x for x in f.fixes if _improves(f, x)]
            if not f.fixes:
                continue
        keep.append(f)
    findings = keep
    taken = set()
    for f in findings:
        if f.det in ("ESCAPE", "PLANE", "ROLES", "INTENT", "COURTYARD", "BUSORDER"):
            for k in ("parts", "part"):
                v = f.handles.get(k)
                taken |= set(v if isinstance(v, list) else [v] if v else [])
            for x in f.fixes[:1]:
                taken |= {a.split()[1] for a in x.acts if len(a.split()) > 1}
    findings = [f for f in findings if not (f.det in ("TENSION", "ORIENT") and f.handles.get("part") in taken)]
    findings.sort(key=_rank)
    if gscore:
        from . import gscore as GS
        t1 = time.time()
        n = GS.rank_fixes(ac, findings, gscore)
        times["GSCORE"] = time.time() - t1
        ac.n_groute = n
    rep = Report(ac, findings, times, name or st.name)
    rep.secs = times
    rep.wall = time.time() - t0
    return rep


# ----------------------------------------------------------------------------- text
def _fmt_fix(f, fx, width=170):
    if fx is None:
        return "fix: none legal found"
    r = fx.res
    s = "fix> %s" % fx.spec
    tail = "  => dscore %+.1f (%s) %s" % (r.dscore, fmt_delta(r.d), fx.legal_str())
    after = getattr(fx, "after", "")
    if after:
        tail += "  [%s]" % after
    if getattr(fx, "g1", None):
        from . import gscore as GS
        tail += "  {%s}" % GS.fmt(fx.g0, fx.g1)
    if len(s) + len(tail) > width:
        return s + "\n         " + tail.strip()
    return s + tail


def text(rep, full=False, limit_lines=60):
    ac = rep.ac
    c = rep.counts()
    L = ["badpcb audit %s: %d finding(s) [%d ERR %d WARN %d NOTE], score %.0f  (%d what-ifs, %.1fs)%s" % (
        rep.name, len(rep.findings), c["ERR"], c["WARN"], c["NOTE"], rep.score, ac.n_eval, rep.wall,
        "  [%d open conns, %d locked tracks]" % (ac.base["unrouted"], sum(t.locked for t in ac.b.tracks)))]
    body = []
    for i, f in enumerate(rep.findings, 1):
        h = f.handles
        hs = " ".join("%s=%s" % (k, ",".join(map(str, v)) if isinstance(v, (list, tuple)) else v) for k, v in h.items()
                      if k in ("part", "parts", "net", "nets", "region", "regions", "intruders", "role", "plane_under", "group", "cut") and v)
        lines = ["#%d %-4s %-9s %s" % (i, f.sev, f.det, f.title), "      %s" % (hs if full else hs[:130])]
        if f.ev.get("blocked_pads") and full:
            lines.append("      blocked pads: %s" % " ".join(f.ev["blocked_pads"]))
        best = f.best
        fxs = [best] if best else []
        if full:
            fxs = [x for x in f.fixes][:5]
        if not f.fixes:
            lines.append("      fix: none generated")
        elif not best and not full:
            x = f.fixes[0]
            lines.append("      fix: all candidates illegal, e.g. %s -> %s" % (x.spec[:80], x.legal_str()))
        for x in fxs:
            lines.append("      " + _fmt_fix(f, x))
        if full:
            for a in f.advice:
                lines.append("      advice: " + a)
        elif f.advice:
            lines.append("      advice: " + f.advice[0][:190])
        body.append(lines)
    if full:
        for b in body:
            L += b
    else:
        used = len(L) + 2
        shown = 0
        for b in body:
            nl = sum(x.count("\n") + 1 for x in b)
            if used + nl > limit_lines - 1:
                break
            L += b
            used += nl
            shown += 1
        if shown < len(body):
            L.append("... %d more finding(s) (%s); --full prints all" % (len(body) - shown, ", ".join(sorted({"%s" % f.det for f in rep.findings[shown:]}))))
    if not rep.findings:
        L.append("no findings: placement looks routable")
    L.append("summary: score %.0f = " % rep.score + " ".join("%s %.0f" % (d, sum(f.weight for f in rep.findings if f.det == d))
                                                              for d in DETS if any(f.det == d for f in rep.findings)))
    return L


def as_json(rep):
    return {"name": rep.name, "score": round(rep.score, 1), "counts": dict(rep.counts()), "seconds": round(rep.wall, 1),
            "findings": [f.as_dict() for f in rep.findings],
            "groute": rep.groute}


# ----------------------------------------------------------------------------- groute cross-check
def run_groute(rep, tlimit=25.0):
    """groute at 1 mm (short budget) on the same board; returns text lines comparing its hot spots with the audit's anchors."""
    from . import groute
    ac = rep.ac
    b = ac.b
    note = ""
    nsig = sum(1 for t in b.tracks if not t.locked and t.net not in ac.planes)
    if nsig > 200:
        locked_t = [t for t in b.tracks if t.locked]
        locked_v = [v for v in b.vias if v.locked]
        b = dataclasses.replace(b, tracks=locked_t, vias=locked_v)
        note = " (board is routed: groute ran on a copy stripped of its unlocked copper)"
    rn = ratsnest.build(b, ac.st.assume)
    res = groute.run(b, rn, cell=1.0, iters=12, tlimit=tlimit, stall=4)
    gr = res.gr
    rows = gr.hot_spots(res.ov, res.xv)
    out = ["groute 1 mm%s: %s" % (note, res.summary_line())]
    anchors = [(f, f.where) for f in rep.findings if f.where]
    agree = 0
    jrows = []
    for h, d in rows[:8]:
        w = d["where"]
        near = None
        if w:
            near = min(anchors, key=lambda a: geom.dist(a[1], w), default=None)
        dist = geom.dist(near[1], w) if near and w else None
        ok = dist is not None and dist <= 8.0
        agree += bool(ok)
        out.append("  hot %-8s over %d hist %.1f at (%.0f,%.0f) nets %s  -> %s" % (
            h, d["over"], d["hist"], w[0] if w else 0, w[1] if w else 0, " ".join(sorted(d["nets"])[:6]),
            "audit agrees: #%d %s %s (%.1f mm)" % (rep.findings.index(near[0]) + 1, near[0].det, near[0].title.split(":")[0][:30], dist) if ok else "no audit finding within 8 mm"))
        jrows.append({"handle": h, "over": d["over"], "hist": round(d["hist"], 1), "where": w, "agree": bool(ok)})
    # and the reverse: audit ESCAPE/DENSITY anchors that groute does not mark
    out.append("  hot spots matched by an audit finding: %d/%d" % (agree, min(8, len(rows))))
    rep.groute = {"summary": res.st, "hot": jrows, "note": note}
    return out


# ----------------------------------------------------------------------------- suggest hook
def _key(f):
    h = f.handles
    return (f.det, str(h.get("part")), str(h.get("side")), str(sorted(h.get("parts", []) or [])), str(h.get("net")), str(h.get("group")), str(h.get("role")))


def suggest(st, n=10, max_eval=40, budget_s=60.0):
    """badpcb suggest S audit: the best fix of each finding, in audit order, as evaluate Results.
    Rows are CUMULATIVE: row k is evaluated (legality and delta) on the state after rows 1..k-1, so applying
    `badpcb act S @1`, `@2`, ... in order works; a fix that became illegal falls back to the finding's next candidate."""
    rep = run(st, name=st.name)
    ac = rep.ac
    notes, res = [], []
    base = st.metrics()
    done, toks = [], []
    w = st.world
    try:
        for i, f in enumerate(rep.findings, 1):
            if len(res) >= n:
                break
            cands = [x for x in f.fixes if x.ok]
            tried_fresh = False
            while True:
                picked = None
                for fx in cands:
                    if fx.tag in ("swap", "assign") and f.det == "ESCAPE":
                        continue
                    r = EV._eval_one(st, parse_specs(fx.spec), base)
                    if not r.ok:
                        continue
                    if f.det in ("BOWTIE", "ORIENT", "TENSION", "BUSORDER") and r.dscore >= 0:
                        continue
                    picked = (fx, r)
                    break
                if picked or tried_fresh or not done:
                    break
                # every stored fix became illegal after the earlier rows: re-derive this finding on the cumulative state
                tried_fresh = True
                ac2 = Ctx(st)
                ac2.roles()
                again = [g for g in DETECTORS[f.det](ac2) if _key(g) == _key(f)]
                if not again:
                    break
                _eval_fixes(ac2, again[0], MAX_EVALS)
                cands = [x for x in again[0].fixes if x.ok]
            if not picked:
                continue
            fx, r = picked
            nets = set()
            for a in parse_specs(fx.spec):
                inv, nn = A.apply(w, a, check_first=False)
                done.append(inv)
                nets |= nn
            toks.append(st.eng.refresh(nets) if nets else None)
            base = st.eng.metrics()
            r.note = "audit #%d %s" % (i, f.det)
            res.append(r)
            notes.append("row %d = audit #%d %s: %s" % (len(res), i, f.det, f.title[:100]))
    finally:
        for tok in reversed(toks):
            if tok is not None:
                st.eng.restore(tok)
        for inv in reversed(done):
            A.apply(w, inv, check_first=False)
    notes.insert(0, "audit score %.0f, %d finding(s); %d fix row(s); rows are cumulative: apply them in order (act S @1, then @2 ...)" % (
        rep.score, len(rep.findings), len(res)))
    return res, notes


# ----------------------------------------------------------------------------- CLI
def load_state(target, assume=()):
    import os
    from . import cli, state
    if os.path.exists(target) and not os.path.isdir(target):
        board = cli.load_board(target)
        assume = list(assume)
        if not board.plane_nets() and not assume:
            counts = defaultdict(int)
            for pd in board.pads():
                if pd.net:
                    counts[pd.net] += 1
            assume = sorted(n for n, k in counts.items() if k >= 25)
        return state.State(os.path.basename(target).rsplit(".", 1)[0], os.path.abspath(target), board, assume, 21.0)
    return state.State.load(target)


def main_args(a):
    t0 = time.time()
    st = load_state(a.target)
    only = [x for x in (a.only or "").split(",") if x]
    bad = [x for x in only if x.upper() not in DETS]
    if bad:
        sys.exit("unknown detector(s) %s (have %s)" % (",".join(bad), ",".join(DETS)))
    rep = run(st, only or None, name=a.target if len(a.target) < 40 else st.name, gscore=getattr(a, "gscore", 0.0) or None)
    if a.json:
        if a.groute:
            run_groute(rep)
        d = as_json(rep)
        if a.roles:
            d["roles"] = [r.as_dict() for r in rep.ac.roles().values()]
        print(json.dumps(d, indent=1, default=str))
        return 0
    if a.roles:
        print("\n".join(sec_roles(rep.ac)))
        print()
    print("\n".join(text(rep, a.full)))
    if a.groute:
        print("\n".join(run_groute(rep)))
    print("(badpcb audit %.1fs)" % (time.time() - t0), file=sys.stderr)
    return 0
