"""badpcb joint: solve the placement findings of one IC together (ESCAPE intruders + ROLES decaps/filter caps + edge margin + plane stubs).

    python3 -m badpcb audit <session|board> --joint [U1[,U2..]] [--edge 1.5]
    badpcb plan <session> [U1[,U2..]] [--edge 1.5]          (same thing)

Why: the per-finding fixes of `badpcb audit` fight each other (ESCAPE pushes a filter cap out of the fan zone, ROLES pulls it
back in; both happily land the part on the board edge; a moved decap strands its plane-via stub). Here every part that
is wanted near the IC is placed in ONE pass against the same constraints:
  * pads outside every fine-pitch escape zone (grown by 0.3 mm), courtyard at least `edge` mm from the board edge,
  * role distance rule kept (decap 3 mm, filter cap 6 mm, ...; hard limit 1.15 x rule), series R / test points stay near their pin,
  * legal.py/actions.fits legality against the board *as already modified by the earlier parts of the plan*,
  * a plane-via stub the part owns moves with it (delvia ... stub; move; via ... pad=).
Most constrained part first (shortest distance rule), candidate score = distance to the pin + 0.25 x displacement + edge/rotation/rip penalties
+ a little intent wirelength. Pure planning: the live world is restored before returning. The plan is validated by
re-scanning the fan zones, re-inferring the roles and evaluate.py on the whole action chain.
"""
import time

from . import actions as A
from . import audit as AU
from . import geom, legal
from .audit import Fix, Trial, _pads_hit, escape_zones, zone_scan

HARD = 1.15            # hard limit = HARD x role rule
SOFT_FAR = 12.0        # hard limit for parts without a distance rule (series R, test points)


class Member:
    def __init__(self, ref, why, anchors, limit, net):
        self.ref, self.why, self.anchors, self.limit, self.net = ref, why, anchors, limit, net


def _ic_zones(ac, ref):
    return [z for z in escape_zones(ac, {ref}) if z.n_pads >= 6]


def _margin(b, bb):
    x0, y0, x1, y1 = b.bbox
    return min(bb[0] - x0, bb[1] - y0, x1 - bb[2], y1 - bb[3])


def collect(ac, ref, edge, minimal=False):
    """Parts to place for IC `ref`: unexpected fan-zone intruders + role parts anchored at it that are too far or too close to the edge."""
    b = ac.b
    zones = _ic_zones(ac, ref)
    roles = ac.roles()
    members = {}
    only = None
    if minimal:       # only what actually blocks a signal stub of the fan, plus the filter caps of those nets (the board lane's manual fix pattern)
        only = set()
        for z in zones:
            sc = zone_scan(ac, z)
            tol = {r for r in sc["parts"] if AU._tolerated(ac, z, r)}
            bs = AU._blocked_signal(ac, z, sc, tol)
            only |= {o for _d, o in bs if o in b.parts}
            bn = {d.net for d, _o in bs}
            only |= {r for r, ro in roles.items() if ro.kind == "filter_cap" and ro.net in bn and ro.anchor.startswith(ref + ".")}
    for r, ro in roles.items():
        if only is not None and r not in only:
            continue
        if not ro.anchor or ro.anchor.split(".")[0] != ref or not ac.movable(r):
            continue
        bad = AU.role_violation(ro) is not None and not ac.routed_part(r)
        low = _margin(b, b.parts[r].bbox) < edge
        if bad or low:
            anchors = [d.pos for d in b.pads() if d.ref == ref and d.net == ro.net] or ([ro.anchor_pt] if ro.anchor_pt else [])
            if anchors:
                members[r] = Member(r, "role" if bad else "edge", anchors, ro.limit, ro.net)
    for z in zones:
        sc = zone_scan(ac, z)
        for r in sc["parts"]:
            if r in members or not ac.movable(r) or AU._tolerated(ac, z, r) or (only is not None and r not in only):
                continue
            p = b.parts[r]
            anchors, net = [], ""
            for d in p.pads:                                   # pad whose net reaches a pin of the IC
                pins = [q.pos for q in b.pads() if q.ref == ref and q.net == d.net and d.net and not ac.is_gnd(d.net)]
                if pins:
                    anchors, net = pins, d.net
                    break
            ro = roles.get(r)
            if not anchors and ro and ro.anchor_pt:
                anchors, net = [ro.anchor_pt], ro.net
            if not anchors:
                pp = b.parts[ref].pads
                anchors = [(sum(d.pos[0] for d in pp) / len(pp), sum(d.pos[1] for d in pp) / len(pp))]
            members[r] = Member(r, "intruder " + z.name, anchors, ro.limit if ro else None, net)
    return zones, sorted(members.values(), key=lambda m: (m.limit if m.limit else 99.0, AU._nat(m.ref)))


def _stubs(ac, ref):
    """[(via, pad)] plane-via stubs owned by the part (unlocked via + unlocked short track from the pad to it)."""
    b = ac.b
    out = []
    for pd in b.parts[ref].pads:
        if pd.net not in ac.planes:
            continue
        for t in b.tracks:
            if t.net != pd.net or t.locked or geom.dist(t.a, pd.pos) > 0.05:
                continue
            for v in b.vias:
                if v.net == pd.net and not v.locked and geom.dist(v.pos, t.b) < 1e-6:
                    out.append((v, pd))
    return out


def _partners(ac, ref):
    out = []
    for net, pa, pb, ra, rb, ia, ib in ac.lines():
        if ra == ref and rb != ref:
            out.append(pb)
        elif rb == ref and ra != ref:
            out.append(pa)
    return out


def _cands(ac, m, rects, edge, step=0.3, relax=1.0, grow=0.3):
    b = ac.b
    p = b.parts[m.ref]
    hard = (m.limit * HARD if m.limit else SOFT_FAR) * relax
    pk = ac.plane_keep(m.ref)
    k = [i for i, d in enumerate(p.pads) if d.net == m.net] or [0]
    partners = _partners(ac, m.ref)
    ax = sum(a[0] for a in m.anchors) / len(m.anchors)
    ay = sum(a[1] for a in m.anchors) / len(m.anchors)
    R = hard + 2.5
    out = []
    for rot in (p.rot, A._norm(p.rot + 180), A._norm(p.rot + 90), A._norm(p.rot + 270)):
        bb0, pads0 = A.place_geom(ac.w, m.ref, (0.0, 0.0), rot, p.side)
        dr = abs(A._norm(rot - p.rot))
        rc = 0.0 if dr < 1e-6 else (0.3 if abs(dr - 180) < 1e-6 else 0.5)
        nx = int(2 * R / step)
        for ix in range(nx + 1):
            x = round(ax - R + ix * step, 2)
            for iy in range(nx + 1):
                y = round(ay - R + iy * step, 2)
                bb = (bb0[0] + x, bb0[1] + y, bb0[2] + x, bb0[3] + y)
                mg = _margin(b, bb)
                if mg < edge:
                    continue
                pads = [((pp[0] + x, pp[1] + y), pr, pl) for pp, pr, pl in pads0]
                d = min(geom.dist(pads[i][0], a) for i in k for a in m.anchors)
                if d > hard:
                    continue
                if rects and _pads_hit(p, pads, rects, grow):
                    continue
                if pk and not pk(pads):
                    continue
                disp = geom.dist((x, y), p.pos)
                wl = sum(geom.dist(pads[0][0], q) for q in partners) / len(partners) if partners else 0.0
                cost = 0.5 * d + 0.25 * disp + rc + 0.03 * wl + (0.3 * (3.0 - mg) / 1.5 if mg < 3.0 else 0.0)
                out.append((cost, (x, y), rot, d))
    out.sort(key=lambda c: c[0])
    return out


def _act(p, pos, rot):
    return A.Action("move", ref=p.ref, x=pos[0], y=pos[1], **({"rot": rot} if abs(A._norm(rot - p.rot)) > 1e-6 else {}))


def place_one(ac, m, rects, edge, relax, tries, inv_stack, ap, grow=0.3):
    b = ac.b
    p = b.parts[m.ref]
    old = (p.pos, p.rot)
    stubs = _stubs(ac, m.ref)
    n_ok = 0
    best = None
    for cost, pos, rot, d in _cands(ac, m, rects, edge, relax=relax, grow=grow)[:tries * 40]:
        if n_ok >= 3:
            break
        if not A.fits(ac.w, m.ref, pos, rot):
            continue
        warns = len(legal.warnings(legal.pad_conflicts(ac.w, m.ref, pos, rot, p.side)))
        mark = len(inv_stack)
        chain = ["delvia %s %g %g stub" % (v.net, v.pos[0], v.pos[1]) for v, pd in stubs]
        pids = [pd.id for _, pd in stubs]
        mv = _act(p, pos, rot)
        ok = all(ap(A.parse(t)[0]) is not None for t in chain) and ap(mv) is not None
        vias = []
        if ok:
            for pid in pids:
                pd = A._pad_by_id(b, pid)
                pt = AU._via_spot(ac, pd, pd.net)
                va = A.parse("via %s %g %g pad=%s" % (pd.net, pt[0], pt[1], pid))[0] if pt else None
                if va is None or ap(va) is None:
                    ok = False
                    break
                vias.append(va.compact())
        while len(inv_stack) > mark:          # trial chain rolled back; the winner is re-applied below
            A.apply(ac.w, inv_stack.pop(), check_first=False)
        if not ok:
            continue
        n_ok += 1
        c = cost + 1.0 * warns
        if best is None or c < best[0]:
            best = (c, pos, rot, d, chain, mv.compact(), vias)
    if best is None:
        return None
    c, pos, rot, d, chain, mvs, vias = best
    for t in chain + [mvs] + vias:
        ap(A.parse(t)[0])
    return chain, mvs, vias, (m.ref, m.why, old, (pos, rot), d, m.limit, len(stubs))


def solve(ac, refs, edge=1.5, tries=40, minimal=False):
    """-> (acts [compact str], rows, failed [(ref, why)]).  World restored on return."""
    b = ac.b
    acts, rows, failed = [], [], []
    inv_stack = []

    def ap(a):
        errs, _ = A.check(ac.w, a)
        if errs:
            return None
        inv, _n = A.apply(ac.w, a, check_first=False)
        inv_stack.append(inv)
        return inv

    try:
        for ref in refs:
            zones, members = collect(ac, ref, edge, minimal)
            rects = [z.rect for z in ac.zones]
            for z in zones:
                if z.rect not in rects:
                    rects.append(z.rect)
            pending = list(members)
            for level, (relax, edge_l, grow) in enumerate(((1.0, edge, 0.3), (1.6, max(0.8, edge - 0.7), 0.3), (2.2, 0.5, 0.1))):
                if level >= 1 and not pending:
                    break
                left = []
                if level == 0:        # most constrained first: fewest feasible candidates, then shortest distance rule
                    cc = {m.ref: len(_cands(ac, m, rects, edge_l, relax=relax, grow=grow)) for m in pending}
                    pending.sort(key=lambda m: (cc[m.ref], m.limit or 99.0, AU._nat(m.ref)))
                for m in pending:
                    got = place_one(ac, m, rects, edge_l, relax, tries, inv_stack, ap, grow)
                    if got is None:
                        left.append(m)
                        continue
                    chain, mvs, vias, row = got
                    acts += chain + [mvs] + vias
                    rows.append(row + (level,))
                pending = left
            failed += [(m.ref, m.why) for m in pending]
        return acts, rows, failed
    finally:
        while inv_stack:
            A.apply(ac.w, inv_stack.pop(), check_first=False)


def _state_numbers(ac, refs):
    """fan usage per zone, blocked signal stubs, role violations for the live world."""
    zs = {}
    for ref in refs:
        for z in _ic_zones(ac, ref):
            sc = zone_scan(ac, z)
            tol = {r for r in sc["parts"] if AU._tolerated(ac, z, r)}
            zs[z.name] = (sc["used"], len(AU._blocked_signal(ac, z, sc, tol)))
    roles = AU.infer_roles(ac)
    viol = sorted((r for r, ro in roles.items() if AU.role_violation(ro) and not ac.routed_part(r)), key=AU._nat)
    return zs, viol


def _stub_adds(ac, acts, moved):
    """Plane-via stubs for plane pads of moved parts that end up as unserved islands (parts that had no stub of their own)."""
    from . import evaluate as EV
    st, w = ac.st, ac.w
    done, nets, adds = [], set(), []
    tok = None
    try:
        for a in acts:
            inv, n = A.apply(w, A.parse(a)[0], check_first=False)
            done.append(inv)
            nets |= n
        if nets:
            tok = st.eng.refresh(nets)
        cands, _notes = EV.plane_candidates(st)
        for c in cands:
            a = c[0]
            pad = a.a.get("pad", "")
            if pad.split(".")[0] not in moved:
                continue
            if A.check(w, a)[0]:
                continue
            inv, _n = A.apply(w, a, check_first=False)
            done.append(inv)
            adds.append(a.compact())
    finally:
        for inv in reversed(done):
            A.apply(w, inv, check_first=False)
        if tok is not None:
            st.eng.restore(tok)
    return adds


def run(st, refs=None, edge=1.5, name=None, minimal=False):
    t0 = time.time()
    ac = AU.Ctx(st)
    ac.roles()
    mcu = ac.w.cfg["mcu"]
    refs = list(refs) if refs else [mcu]
    if refs == [mcu]:                       # other ICs anchoring violating roles join, after the MCU
        for r, ro in ac.roles().items():
            a = ro.anchor.split(".")[0] if ro.anchor else ""
            if a and a != mcu and a not in refs and ac.is_ic(ac.b.parts[a]) and not a.startswith("J") and AU.role_violation(ro):
                refs.append(a)
    before_z, before_v = _state_numbers(ac, refs)
    acts, rows, failed = solve(ac, refs, edge, minimal=minimal)
    L = ["badpcb joint plan %s: IC %s, edge margin %.1f mm%s" % (name or st.name, ",".join(refs), edge, ", MINIMAL (only the parts blocking a stub + their filter caps)" if minimal else "")]
    for ref, why, old, new, d, lim, ns, rlx in rows:
        L.append("  %-4s %-14s (%.2f,%.2f)@%g -> (%.2f,%.2f)@%g  pin %.1f%s mm%s%s" % (
            ref, why, old[0][0], old[0][1], old[1], new[0][0], new[0][1], new[1], d, "/%g" % lim if lim else "",
            "  (+%d plane stub moved)" % ns if ns else "",
            ["", "  [relaxed: rule x1.6, edge 0.8]", "  [LAST RESORT: rule x2.2, edge 0.5, fan clearance 0.1]"][rlx]))
    for ref, why in failed:
        L.append("  %-4s %-14s NO LEGAL SPOT under the joint constraints (leave it, relax --edge, or move something else first)" % (ref, why))
    if not acts:
        L.append("  nothing to do")
        return ac, acts, "\n".join(L)
    adds = _stub_adds(ac, acts, {r[0] for r in rows})
    if adds:
        L.append("  + %d plane-via stub(s) for moved plane pads that lost their access" % len(adds))
        acts = acts + adds
    fx = Fix(acts, "joint ring plan")
    ac.predict(fx)
    with Trial(ac.w) as tr:
        for a in acts:
            tr.apply(A.parse(a)[0])
        after_z, after_v = _state_numbers(ac, refs)
    L.append("fan zones (used%, blocked signal stubs): " + "  ".join(
        "%s %.0f%%/%d -> %.0f%%/%d" % (k, 100 * before_z[k][0], before_z[k][1], 100 * after_z[k][0], after_z[k][1]) for k in sorted(before_z)))
    L.append("role violations %d -> %d%s" % (len(before_v), len(after_v), " (still: %s)" % " ".join(after_v) if after_v else ""))
    r = fx.res
    L.append("evaluate: %s  dscore %+.1f (%s)  %s" % ("legal" if r.ok else "ILLEGAL", r.dscore, AU.fmt_delta(r.d),
                                                      "; ".join(r.warnings[:2])[:120] if r.warnings else ""))
    L.append("apply: badpcb act %s '%s'" % (name or st.name, "; ".join(acts)))
    L.append("(%d moves, %.1fs)" % (len(rows), time.time() - t0))
    return ac, acts, "\n".join(L)


def main_args(target, refs, edge, minimal=False):
    st = AU.load_state(target)
    ac, acts, txt = run(st, [x for x in (refs or "").split(",") if x] or None, edge, name=target if len(target) < 40 else st.name, minimal=minimal)
    print(txt)
    return 0
