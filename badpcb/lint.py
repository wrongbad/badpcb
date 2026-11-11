"""Route lint: how ugly is the routed copper of each signal net, as a table an agent can act on.

    python3 -m badpcb lint BOARD [--nets GLOB ...] [--top N] [--all] [--json]       (also `report --section lint`)

Per routed signal net (plane nets, nets without copper and single-pad nets are skipped):
  detour   routed track length / direct pad-to-pad (MST) length             (limit: style detour_max, default DEFAULTS.detour_free)
  vias     signal vias (vias of plane nets and stitching vias with no track on them are not counted; the first 0.5 x (pads-1) are free)
  jogs     segments shorter than jog_factor x track width between two bends; bends per 10 mm (free up to bend_free_per10)
  acute    corners sharper than 90 degrees (interior angle < acute_below);  c90 = right-angle corners
  dir      long segments running against the dominant direction of their layer (inferred from all signal copper per layer)
  split    outer-layer track whose projection onto the adjacent plane layer leaves the zone outlines (split_mm) or hops between
           zones of different nets (split_x): the return current has no clean reference. Zone outlines only, fills are never read.
  pair     `pair` styles: skew (length difference), gap variation and uncoupled fraction between P and N
  esc      copper of a net with no pad on a fine-pitch part inside that part's escape zone (audit.escape_zones geometry)
  avoid    length running within avoid_mm of nets the style says to keep away from
Score = sum(weight x quantity); weights = DEFAULTS["weights"], overridden by badpcb.json "lint": {"weights": {...}} and scaled per net by its
style (via_penalty -> via weight, keep_ref_plane -> split x3, priority -> everything x1.5). Everything is soft: the table ranks, nothing is vetoed.
Handles are the groute/droute vocabulary (cut `C12` / ring `U1-in` / region `R8`) of the worst feature of the net.
"""
import fnmatch
import json
import math
import sys

from . import config, geom

DEFAULTS = {
    "via_free_per_pad": 0.5,     # vias that cost nothing per (pads - 1) of the net
    "bad_score": 10.0,           # a net at/above this is "bad"
    "detour_free": 1.4,          # ratio up to which a detour is free
    "detour_min_direct_mm": 3.0,  # shorter nets: ratio is noise
    "jog_factor": 3.0,           # jog = segment < jog_factor x width between two bends
    "bend_free_per10": 3.0,      # bends per 10 mm that cost nothing
    "bend_min_deg": 12.0,        # direction change that counts as a bend
    "jog_min_deg": 30.0,         # ... and as a jog end
    "acute_below_deg": 85.0,     # interior angle under this = acute
    "corner90_tol_deg": 5.0,     # |interior - 90| under this = right angle
    "dir_min_len_mm": 1.5,       # segments shorter than this never count as direction breaks
    "dir_dominance": 1.5,        # a layer has a dominant direction when H/V length ratio exceeds this
    "sample_mm": 0.5,            # sampling step along tracks (split, gap, avoid)
    "pair_coupled_mm": 1.0,      # edge gap under which P and N count as coupled
    "pair_skew_mm": 0.5,         # default skew limit
    "avoid_mm": 1.0,
    "bad_share": 0.4,            # share of scored nets that are bad above which the board is a placement problem
    "bad_min": 8,
    "weights": {"detour": 10.0, "via": 2.0, "jog": 1.5, "bend": 0.5, "acute": 6.0, "corner90": 2.0, "dir": 1.0, "split_mm": 1.0,
                "split_cross": 4.0, "skew": 5.0, "gap": 4.0, "uncoupled": 8.0, "esc_mm": 1.5, "esc_via": 3.0, "avoid_mm": 1.0},
    "priority_scale": 1.5,
    "ref_plane_scale": 3.0,
}


def params():
    c = dict(DEFAULTS)
    c["weights"] = dict(DEFAULTS["weights"])
    over = config.current().lint_cfg()
    bad = sorted(set(over) - set(DEFAULTS))
    if bad:
        sys.exit("badpcb: unknown \"lint\" key(s) in badpcb.json: %s (known: %s)" % (", ".join(bad), ", ".join(DEFAULTS)))
    for k, v in over.items():
        if k == "weights":
            wb = sorted(set(v) - set(DEFAULTS["weights"]))
            if wb:
                sys.exit("badpcb: unknown \"lint\".\"weights\" key(s): %s (known: %s)" % (", ".join(wb), ", ".join(DEFAULTS["weights"])))
            c["weights"].update(v)
        else:
            c[k] = v
    return c


KIND_LABEL = {"detour": "detour", "via": "vias", "jog": "jogs", "bend": "bends", "acute": "acute", "corner90": "c90", "dir": "dirbreak",
              "split_mm": "split", "split_cross": "splitX", "skew": "skew", "gap": "gapvar", "uncoupled": "uncoupled", "esc_mm": "escape",
              "esc_via": "escvia", "avoid_mm": "avoid"}


# ----------------------------------------------------------------------------- small geometry
def _len(t):
    return math.hypot(t.a[0] - t.b[0], t.a[1] - t.b[1])


def _key(p):
    return (round(p[0], 3), round(p[1], 3))


def _turn(u, v):
    """Direction change in degrees (0 = straight, 180 = U-turn) between vectors u and v."""
    lu, lv = math.hypot(*u), math.hypot(*v)
    if lu < 1e-9 or lv < 1e-9:
        return 0.0
    c = (u[0] * v[0] + u[1] * v[1]) / (lu * lv)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _orient(t):
    """'H' | 'V' | 'D' of a track (within 10 degrees of an axis, else diagonal)."""
    dx, dy = abs(t.a[0] - t.b[0]), abs(t.a[1] - t.b[1])
    if dx < 1e-9 and dy < 1e-9:
        return None
    ang = math.degrees(math.atan2(dy, dx))
    return "H" if ang < 10 else "V" if ang > 80 else "D"


def _samples(a, b, step):
    n = max(1, int(math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) / step)))
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)], math.hypot(b[0] - a[0], b[1] - a[1]) / n


def _clip_len(a, b, rect):
    """Length of segment a-b inside axis-aligned rect (x0, y0, x1, y1)."""
    cen = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
    iv = geom.clip_seg_rect(a, b, cen, ((rect[2] - rect[0]) / 2, (rect[3] - rect[1]) / 2))
    return 0.0 if iv is None else (iv[1] - iv[0]) * geom.dist(a, b)


class _ZoneIdx:
    """Zones of one layer with bbox rejection: nets_at(p) -> frozenset of zone nets whose outline (minus holes) contains p."""

    def __init__(self, zones, layer):
        self.items = []
        for z in zones:
            if z.net and layer in z.layers:
                for zp in z.polys:
                    o = zp["outer"]
                    xs, ys = [q[0] for q in o], [q[1] for q in o]
                    self.items.append((z.net, o, zp.get("holes", []), (min(xs), min(ys), max(xs), max(ys)), getattr(z, "priority", 0) or 0))

    def nets_at(self, p):
        """Nets of the highest-priority zone(s) covering p (overlapping zones: the higher priority fills, like KiCad)."""
        best, out = None, set()
        for net, o, holes, bb, pr in self.items:
            if bb[0] <= p[0] <= bb[2] and bb[1] <= p[1] <= bb[3] and (best is None or pr >= best) and geom.point_in_poly(p, o) \
                    and not any(geom.point_in_poly(p, h) for h in holes):
                if best is None or pr > best:
                    best, out = pr, set()
                out.add(net)
        return frozenset(out)


class _Shim:
    """What audit.escape_zones needs from an audit context."""

    def __init__(self, b):
        self.b = b

    def layer_of(self, ref):
        return "B.Cu" if self.b.parts[ref].side == "B" else "F.Cu"


# ----------------------------------------------------------------------------- the linter
class NetLint:
    def __init__(self, net):
        self.net = net
        self.score = 0.0
        self.m = {}                 # raw metrics
        self.items = []             # (kind, quantity, points, (a, b) segment where it happens)
        self.styles = []
        self.open = False
        self.handle = "-"
        self.pair_pen = 0.0

    def add(self, kind, qty, w, where=None):
        if qty <= 0:
            return
        pts = qty * w
        self.items.append((kind, qty, pts, where))
        self.score += pts

    def why(self, n=3):
        agg = {}
        for k, q, p, _ in self.items:
            a = agg.setdefault(k, [0.0, 0.0])
            a[0] += q
            a[1] += p
        top = sorted(agg.items(), key=lambda kv: -kv[1][1])[:n]
        return " ".join("%s%s(+%.1f)" % (KIND_LABEL[k], ("%d" % round(q)) if abs(q - round(q)) < 1e-6 else ("%.1f" % q), p) for k, (q, p) in top) or "-"

    def as_dict(self):
        return {"net": self.net, "score": round(self.score, 2), "open": self.open, "handle": self.handle, "styles": self.styles,
                "metrics": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.m.items()},
                "items": [{"kind": k, "qty": round(q, 3), "points": round(p, 2)} for k, q, p, _ in self.items], "why": self.why()}


class Lint:
    def __init__(self, nets, board, total, params, notes):
        self.nets = nets                     # {name: NetLint}
        self.board = board
        self.total = total
        self.p = params
        self.notes = notes

    def ranked(self, only=None):
        return sorted((n for n in self.nets.values() if only is None or n.net in only), key=lambda n: (-n.score, n.net))

    def bad(self):
        return [n for n in self.ranked() if n.score >= self.p["bad_score"]]

    def placement_problem(self):
        """True when bad nets are so many that routing is not the problem: (share, count) or None."""
        n, b = len(self.nets), len(self.bad())
        if n and b >= self.p["bad_min"] and b / n >= self.p["bad_share"]:
            return b, n
        return None


def lint_board(board, rn=None, resolved=None, names=None, params_=None, with_handles=True):
    """Lint every routed signal net of `board` -> Lint. `resolved` = styles.for_board() ({net: NetStyle}); None loads badpcb.json's."""
    from . import ratsnest, styles as ST
    P = params_ or params()
    W = P["weights"]
    if resolved is None:
        resolved = ST.for_board(board)
    rn = rn or ratsnest.build(board)
    plane_nets = set(board.plane_nets()) | set(rn.plane_nets)
    open_nets = {c.net for c in rn.conns}
    layers = list(board.layers)
    plane_layers = set(board.plane_layers())
    sig_layers = layers          # every layer that carries signal copper gets a direction (an outer layer may also carry a GND pour)
    tracks, vias, pads = {}, {}, {}
    for t in board.tracks:
        if t.net and t.net not in plane_nets and _len(t) > 1e-9:
            tracks.setdefault(t.net, []).append(t)
    for v in board.vias:
        if v.net and v.net not in plane_nets:
            vias.setdefault(v.net, []).append(v)
    for pd in board.pads():
        if pd.net and pd.net not in plane_nets:
            pads.setdefault(pd.net, []).append(pd)
    # dominant direction per layer (from all signal copper)
    dom = {}
    for l in sig_layers:
        h = sum(_len(t) for ts in tracks.values() for t in ts if t.layer == l and _orient(t) == "H")
        v = sum(_len(t) for ts in tracks.values() for t in ts if t.layer == l and _orient(t) == "V")
        if h > P["dir_dominance"] * v and h > 0:
            dom[l] = "H"
        elif v > P["dir_dominance"] * h and v > 0:
            dom[l] = "V"
    # reference planes of the outer layers
    zidx = {}
    for i in (0, len(layers) - 1):
        adj = layers[1] if i == 0 and len(layers) > 1 else layers[-2] if len(layers) > 1 else None
        if adj and adj in plane_layers:
            zidx[layers[i]] = _ZoneIdx(board.zones, adj)
    # escape zones of fine-pitch parts
    ezs = []
    try:
        from . import audit as AU
        for z in AU.escape_zones(_Shim(board)):
            ezs.append((z, {p.net for p in board.parts[z.ref].pads if p.net}))
    except Exception as ex:                               # keep the lint usable on odd boards
        ezs = []
        print("lint: escape zones unavailable (%s)" % ex, file=sys.stderr)
    notes = []
    out = {}
    all_nets = sorted(set(tracks) | set(vias))
    track_by_layer = {}                                    # for gap / avoid: layer -> [(net, track)]
    for n, ts in tracks.items():
        for t in ts:
            track_by_layer.setdefault(t.layer, []).append((n, t))
    for net in all_nets:
        ts = tracks.get(net, [])
        pl = pads.get(net, [])
        npad = len({p.id for p in pl})
        if not ts or npad < 2:
            continue
        st = resolved.get(net)
        nl = NetLint(net)
        nl.styles = list(st.styles) if st else []
        nl.open = net in open_nets
        scale = P["priority_scale"] if st and st.priority else 1.0
        w = {k: v * scale for k, v in W.items()}
        if st and st.keep_ref_plane:
            w["split_mm"] *= P["ref_plane_scale"]
            w["split_cross"] *= P["ref_plane_scale"]
        if st and st.via_penalty:
            w["via"] *= 1.0 + st.via_penalty / 6.0
        out[net] = nl
        length = sum(_len(t) for t in ts)
        # direct length: MST of distinct pads
        seen, pp = set(), []
        for p in pl:
            if p.id not in seen:
                seen.add(p.id)
                pp.append(p.pos)
        mst = geom.mst(len(pp), lambda i, j: geom.dist(pp[i], pp[j]))
        direct = sum(geom.dist(pp[i], pp[j]) for i, j in mst)
        nl.m.update(length=length, direct=direct, detour=(length / direct) if direct > 1e-6 else 1.0)
        # --- detour (only when the net is whole: an open net has missing length)
        lim = st.detour_max if st and st.detour_max else P["detour_free"]
        nl.m["detour_max"] = lim
        if not nl.open and direct >= P["detour_min_direct_mm"] and nl.m["detour"] > lim:
            far = max(ts, key=_len)
            nl.add("detour", nl.m["detour"] - lim, w["detour"], (far.a, far.b))
        # --- vias: signal vias = vias with a track end on them (stitching/plane vias carry no signal copper)
        ends = set()
        for t in ts:
            ends.add(_key(t.a))
            ends.add(_key(t.b))
        sv = [v for v in vias.get(net, []) if _key(v.pos) in ends or any(_near(v.pos, e, 0.06) for e in ends)]
        nl.m["vias"] = len(sv)
        free = int(P["via_free_per_pad"] * (npad - 1))        # a net with many pads cannot avoid every layer change
        nl.m["vias_free"] = free
        for v in sv[free:] if free < len(sv) else ():
            nl.add("via", 1, w["via"], (v.pos, v.pos))
        # --- bends / jogs / corners
        anchors = {_key(p.pos) for p in pl} | {_key(v.pos) for v in vias.get(net, [])}
        node = {}
        for t in ts:
            node.setdefault((t.layer, _key(t.a)), []).append((t, t.b))
            node.setdefault((t.layer, _key(t.b)), []).append((t, t.a))
        bends = acute = c90 = 0
        bend_turn = {}
        for (lay, k), lst in node.items():
            if len(lst) != 2 or k in anchors or lst[0][0] is lst[1][0]:
                continue
            (t1, o1), (t2, o2) = lst
            u = (k[0] - o1[0], k[1] - o1[1])
            v = (o2[0] - k[0], o2[1] - k[1])
            tn = _turn(u, v)
            if tn < P["bend_min_deg"]:
                continue
            bends += 1
            bend_turn[(lay, k)] = tn
            interior = 180.0 - tn
            if interior < P["acute_below_deg"]:
                acute += 1
                nl.add("acute", 1, w["acute"], (o1, o2))
            elif abs(interior - 90.0) <= P["corner90_tol_deg"]:
                c90 += 1
                nl.add("corner90", 1, w["corner90"], (o1, o2))
        jogs = 0
        for t in ts:
            if _len(t) < P["jog_factor"] * t.w:
                ka, kb = (t.layer, _key(t.a)), (t.layer, _key(t.b))
                if bend_turn.get(ka, 0) >= P["jog_min_deg"] and bend_turn.get(kb, 0) >= P["jog_min_deg"]:
                    jogs += 1
                    nl.add("jog", 1, w["jog"], (t.a, t.b))
        b10 = bends / length * 10.0 if length > 1e-6 else 0.0
        nl.m.update(bends=bends, bends10=b10, jogs=jogs, acute=acute, c90=c90)
        if b10 > P["bend_free_per10"]:
            nl.add("bend", (b10 - P["bend_free_per10"]) * length / 10.0, w["bend"], (ts[0].a, ts[0].b))
        # --- layer direction breaks
        dbr = 0
        for t in ts:
            d = dom.get(t.layer)
            o = _orient(t)
            if d and o in ("H", "V") and o != d and _len(t) >= P["dir_min_len_mm"]:
                dbr += 1
                nl.add("dir", 1, w["dir"], (t.a, t.b))
        nl.m["dirbreaks"] = dbr
        # --- reference plane splits (outer layers over an adjacent plane layer)
        sp_mm = sp_x = 0.0
        sp_where = None
        for t in ts:
            zi = zidx.get(t.layer)
            if zi is None or not zi.items:
                continue
            pts, step = _samples(t.a, t.b, P["sample_mm"])
            prev = None
            first_bad = None
            for p in pts:
                cov = zi.nets_at(p)
                if not cov:
                    sp_mm += step
                    first_bad = first_bad or p
                elif prev is not None and prev and not (prev & cov):
                    sp_x += 1
                    first_bad = first_bad or p
                prev = cov
            if first_bad:
                sp_where = (t.a, t.b)
        nl.add("split_mm", sp_mm, w["split_mm"], sp_where)
        nl.add("split_cross", sp_x, w["split_cross"], sp_where)
        nl.m.update(split_mm=sp_mm, split_x=int(sp_x))
        # --- escape-zone intrusion
        esc_mm = 0.0
        esc_v = 0
        eh = {}
        esc_where = None
        for z, znets in ezs:
            if net in znets:
                continue
            rect = z.rect
            for t in ts:
                if t.layer != z.layer:
                    continue
                c = _clip_len(t.a, t.b, rect)
                if c > 1e-6:
                    esc_mm += c
                    eh[z.name] = eh.get(z.name, 0.0) + c
                    esc_where = (t.a, t.b)
            for v in sv:
                if rect[0] <= v.pos[0] <= rect[2] and rect[1] <= v.pos[1] <= rect[3]:
                    esc_v += 1
        nl.add("esc_mm", esc_mm, w["esc_mm"], esc_where)
        nl.add("esc_via", esc_v, w["esc_via"], esc_where)
        nl.m.update(esc_mm=esc_mm, esc_zone=",".join(sorted(eh, key=lambda k: -eh[k])))
        # --- avoid
        if st and st.avoid:
            av = {m for m in board.nets if m != net and any(m == a or fnmatch.fnmatchcase(m, a) for a in st.avoid)}
            near = 0.0
            where = None
            for t in ts:
                others = [(n2, t2) for n2, t2 in track_by_layer.get(t.layer, ()) if n2 in av]
                if not others:
                    continue
                pts, step = _samples(t.a, t.b, P["sample_mm"])
                for p in pts:
                    for n2, t2 in others:
                        if geom.pt_seg_dist(p, t2.a, t2.b) - (t.w + t2.w) / 2 < P["avoid_mm"]:
                            near += step
                            where = where or (t.a, t.b)
                            break
            nl.m["avoid_mm"] = near
            nl.add("avoid_mm", near, w["avoid_mm"], where)
    # --- pairs (computed once, split between the two nets)
    done = set()
    for net, st in resolved.items():
        for pr in st.pair:
            if pr in done or pr[0] not in out or pr[1] not in out:
                continue
            done.add(pr)
            _pair(out[pr[0]], out[pr[1]], tracks, P, W, resolved)
    # --- handles + total
    names_ = names
    if with_handles:
        try:
            if names_ is None:
                from . import groute
                names_ = groute.Names(board, rn)
        except Exception:
            names_ = None
    for nl in out.values():
        best = max((i for i in nl.items if i[3]), key=lambda i: i[2], default=None)
        nl.m["score"] = nl.score
        if best and names_ is not None:
            try:
                nl.handle = names_.handle(best[3][0], best[3][1])
            except Exception:
                nl.handle = "-"
        elif best:
            nl.handle = "(%.1f,%.1f)" % best[3][0]
    total = sum(n.score for n in out.values())
    return Lint(out, board, total, P, notes)


def _near(a, b, d):
    return abs(a[0] - b[0]) <= d and abs(a[1] - b[1]) <= d


def _pair(a, b, tracks, P, W, resolved):
    """Skew, gap variation and uncoupled share of the P/N pair; the penalty is shared by both nets."""
    ta, tb = tracks.get(a.net, []), tracks.get(b.net, [])
    la, lb = a.m["length"], b.m["length"]
    sk_lim = None
    for n in (a.net, b.net):
        s = resolved.get(n)
        if s and s.skew_mm is not None:
            sk_lim = s.skew_mm if sk_lim is None else min(sk_lim, s.skew_mm)
    sk_lim = P["pair_skew_mm"] if sk_lim is None else sk_lim
    skew = abs(la - lb)
    gaps, tot, unc = [], 0.0, 0.0
    where = None
    for t in ta:
        others = [t2 for t2 in tb if t2.layer == t.layer]
        pts, step = _samples(t.a, t.b, P["sample_mm"])
        for p in pts:
            tot += step
            g = min((geom.pt_seg_dist(p, t2.a, t2.b) - (t.w + t2.w) / 2 for t2 in others), default=None)
            if g is None or g > P["pair_coupled_mm"]:
                unc += step
                where = where or (t.a, t.b)
            else:
                gaps.append(g)
    var = (max(gaps) - min(gaps)) if gaps else 0.0
    unc_f = unc / tot if tot > 1e-6 else 0.0
    for nl, other in ((a, b), (b, a)):
        nl.m.update(pair=other.net, skew=skew, gap_var=var, uncoupled=unc_f)
        if skew > sk_lim:
            nl.add("skew", (skew - sk_lim) * 0.5, W["skew"], None)
        nl.add("gap", var * 0.5, W["gap"], where)
        nl.add("uncoupled", unc_f * 0.5, W["uncoupled"], where)


# ----------------------------------------------------------------------------- output
def table(L, top=15, only=None, show_all=False, name=""):
    nets = L.ranked(only)
    sc = len(L.nets)
    nopen = sum(1 for n in L.nets.values() if n.open)
    bad = L.bad()
    out = ["LINT %s: %d signal nets scored (%d still open), total ugliness %.1f, %d bad (score >= %g)%s" % (
        name or "board", sc, nopen, L.total, len(bad), L.p["bad_score"], ("; %d listed" % len(nets)) if only is not None else "")]
    if not nets:
        out.append("  no routed signal copper to lint")
        return out
    out.append("  %-3s %-14s %6s %6s %4s %4s %5s %5s %3s %4s %6s %6s %-9s %s" % (
        "#", "net", "score", "detour", "vias", "jogs", "bd/10", "acute", "c90", "dirb", "split", "esc", "handle", "why"))
    for i, n in enumerate(nets if show_all else nets[:top]):
        m = n.m
        out.append("  %-3d %-14s %6.1f %6.2f %4d %4d %5.1f %5d %3d %4d %6.1f %6.1f %-9s %s%s" % (
            i + 1, n.net[:14], n.score, m["detour"], m["vias"], m["jogs"], m["bends10"], m["acute"], m["c90"], m["dirbreaks"],
            m["split_mm"] + 4 * m["split_x"], m["esc_mm"], n.handle[:9], n.why(), "  [OPEN]" if n.open else ""))
        if "pair" in m:
            out.append("      pair %s/%s skew %.2f mm, gap variation %.2f mm, uncoupled %.0f%%" % (n.net, m["pair"], m["skew"], m["gap_var"], 100 * m["uncoupled"]))
    if not show_all and len(nets) > top:
        out.append("  ... %d more (--all / --top N)" % (len(nets) - top))
    pp = L.placement_problem()
    if pp:
        hs = {}
        for n in bad:
            hs.setdefault(n.handle, []).append(n.net)
        out.append("  ESCALATE: %d of %d nets are bad: this is a placement problem, not a routing one (more polish will not fix it). Worst handles: %s" % (
            pp[0], pp[1], "; ".join("%s: %s" % (h, " ".join(v[:5])) for h, v in sorted(hs.items(), key=lambda kv: -len(kv[1]))[:4])))
        out.append("      -> go back to PLAYBOOK step 2/3 (floorplan / escape corridors): `badpcb audit --only ESCAPE,CHANNEL`, `badpcb floorplan`")
    elif bad:
        out.append("  %d bad net(s): candidates for `badpcb droute BOARD --polish %d` (rip and reroute, accepted only if lint improves and DRC/open conns do not get worse)" % (
            len(bad), min(len(bad), 5)))
    return out


def section(c, full=False):
    """`report --section lint` (Ctx c)."""
    L = lint_board(c.board, rn=c.rn, names=None)
    return table(L, top=30 if full else 8, show_all=full, name=getattr(c.board, "source", "") or "board")


def main_args(a):
    from . import cli, ratsnest
    board = cli.load_board(a.board)
    rn = ratsnest.build(board)
    L = lint_board(board, rn=rn)
    only = None
    if a.nets:
        pats = [p for g in a.nets for p in g.split(",") if p]
        only = {n for n in L.nets if any(n == p or fnmatch.fnmatchcase(n, p) for p in pats)}
    if a.json:
        print(json.dumps({"board": a.board, "total": round(L.total, 2), "bad_score": L.p["bad_score"], "bad": [n.net for n in L.bad()],
                          "placement_problem": bool(L.placement_problem()),
                          "nets": [n.as_dict() for n in L.ranked(only)]}, indent=1))
        return
    print("\n".join(table(L, top=a.top, only=only, show_all=a.all, name=a.board)))
