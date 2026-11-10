"""What-if evaluation and suggestions. Nothing here commits anything: every candidate is applied to the live
world, the engine is refreshed for the affected nets only, metrics are read, and everything is undone exactly.

    evaluate(state, ["move R17 152 40", "rot C27 90; move C27 150 20"])  -> [Result]
    suggest(state, "adc")                    # group  (adc | en)
    suggest(state, "ADC3")                   # net: part moves near the pin, RC set, pin swaps/assigns
    suggest(state, "K1") / suggest(state, "R8")   # conflict cluster / region: worst nets' suggestions
    suggest(state, "R17")                    # one part: best spots around its neighbours
    suggest(state, "planes")                 # plane vias for unserved islands (net filter: "planes:GND")
Results are ranked by Δscore (engine.WEIGHTS; lower is better). The agent decides what to apply.
"""
import math
import re
import time
import types
from collections import defaultdict

from . import actions as A
from . import engine as E
from . import geom, swaps, topo
from .state import _delta, fmt_delta, parse_specs


class Result:
    def __init__(self, acts):
        self.acts = acts
        self.ok, self.errors, self.warnings = True, [], []
        self.m = self.d = None
        self.dscore = 0.0
        self.note = ""

    @property
    def label(self):
        return "; ".join(a.compact() for a in self.acts)

    def as_dict(self):
        return {"label": self.label, "ok": self.ok, "errors": self.errors, "warnings": self.warnings, "delta": self.d,
                "dscore": round(self.dscore, 3), "after": self.m, "actions": [a.to_dict() for a in self.acts], "note": self.note}


def _eval_one(st, acts, base):
    r = Result(acts)
    w = st.world
    done, nets = [], set()
    tok = None
    try:
        for a in acts:
            errs, warns = A.check(w, a)
            if errs:
                r.ok, r.errors = False, errs
                return r
            r.warnings += warns
            inv, n = A.apply(w, a, check_first=False)
            done.append(inv)
            nets |= n
        if nets:
            tok = st.eng.refresh(nets)
        r.m = st.eng.metrics()
        r.d = _delta(base, r.m)
        r.dscore = E.score(r.m) - E.score(base)
    finally:
        for inv in reversed(done):
            A.apply(w, inv, check_first=False)
        if tok is not None:
            st.eng.restore(tok)
    return r


def evaluate(st, candidates):
    """candidates: list of (str | Action | list of those). Returns [Result] in input order."""
    base = st.metrics()
    out = []
    for c in candidates:
        try:
            acts = parse_specs(c)
        except A.ActionError as e:
            r = Result([])
            r.ok, r.errors = False, e.errors
            r.note = str(c)
            out.append(r)
            continue
        out.append(_eval_one(st, acts, base))
    return out


def format_table(results, base=None, limit=None):
    L = []
    for i, r in enumerate(results[:limit], 1):
        if not r.ok:
            L.append("%2d. ILLEGAL  %s  -- %s" % (i, r.label or r.note, "; ".join(r.errors)))
            continue
        L.append("%2d. %-44s dscore %+7.1f | %s%s" % (i, r.label, r.dscore, fmt_delta(r.d),
                                                   "   (warn: %s)" % "; ".join(r.warnings) if r.warnings else ""))
    return L


# ----------------------------------------------------------------------------- suggestion helpers
def _movable(board, ref):
    p = board.parts.get(ref)
    return bool(p) and not p.locked and len(p.pads) <= 6 and p.area < 25


def _ang(c, p):
    return math.atan2(p[1] - c[1], p[0] - c[0])


def _adiff(a, b):
    d = abs(a - b) % (2 * math.pi)
    return min(d, 2 * math.pi - d)


def neighbours(st, ref):
    """For each signal pad of `ref`: [(net, pad_id, other_end_point, length, other_label)] from the live ratsnest."""
    out = []
    eng = st.eng
    for pd in st.world.board.parts[ref].pads:
        if not pd.net or pd.net in eng.planes or pd.net not in eng.nets:
            continue
        st_ = eng.nets[pd.net]
        mine = [I for I in st_["islands"] if pd.id in I.pads]
        if not mine:
            continue
        idx = mine[0].idx
        for cid in st_["cids"]:
            c = eng.recs[cid].conn
            if c.ia == idx:
                out.append((pd.net, pd.id, c.pb, c.length, c.b))
            elif c.ib == idx:
                out.append((pd.net, pd.id, c.pa, c.length, c.a))
    return out


def _spots(st, ref, pad_id, anchor, keep=3, rots=None):
    """Legal placements of part `ref` that put pad `pad_id` close to `anchor`: [(dist, x, y, rot)]."""
    w = st.world
    p = w.board.parts[ref]
    num = pad_id.split(".", 1)[1]
    res = []
    for rot in (rots if rots is not None else [p.rot, p.rot + 90, p.rot + 180, p.rot + 270]):
        _, pads = A.place_geom(w, ref, p.pos, rot, p.side)
        k = [i for i, pd in enumerate(p.pads) if pd.num == num][0]
        off = (pads[k][0][0] - p.pos[0], pads[k][0][1] - p.pos[1])
        best = None
        for r in (0.7, 1.2, 1.8, 2.5, 3.3, 4.2):
            for j in range(16):
                t = 2 * math.pi * j / 16
                T = (anchor[0] + r * math.cos(t), anchor[1] + r * math.sin(t))
                pos = (T[0] - off[0], T[1] - off[1])
                if A.fits(w, ref, pos, rot):
                    if best is None or r < best[0]:
                        best = (r, pos)
            if best:
                break
        if best:
            res.append((best[0], round(best[1][0], 2), round(best[1][1], 2), A._norm(rot)))
    res.sort(key=lambda s: (s[0], abs(A._norm(s[3] - p.rot))))
    return res[:keep]


def part_candidates(st, ref, max_anchors=3, keep=2):
    """Candidate single-part moves: near each far neighbour of the part."""
    w = st.world
    p = w.board.parts[ref]
    nb = [n for n in neighbours(st, ref) if n[3] > 2.5]
    nb.sort(key=lambda n: (0 if w.cfg["mcu"] and n[4].startswith(w.cfg["mcu"] + ".") else 1, -n[3]))
    seen, out = [], []
    for net, pad, pt, ln, lab in nb:
        if any(geom.dist(pt, s) < 2.0 for s in seen):
            continue
        seen.append(pt)
        if len(seen) > max_anchors:
            break
        for r, x, y, rot in _spots(st, ref, pad, pt, keep):
            out.append([A.Action("move", ref=ref, x=x, y=y, **({"rot": rot} if abs(A._norm(rot - p.rot)) > 1e-6 else {}))])
    return out


def rc_set_candidate(st, net, cap_pin=True):
    """Move the passives of an MCU-attached net next to the MCU pin, nearest part first (greedy, legal at each step)."""
    w = st.world
    b = w.board
    ref = w.cfg["mcu"]
    upads = [pd for pd in b.parts[ref].pads if pd.net == net]
    if not upads:
        return None
    anchor = upads[0].pos
    parts = sorted({pd.ref for pd in b.pads_of_net(net) if pd.ref != ref and _movable(b, pd.ref)},
                   key=lambda r: geom.dist(b.parts[r].pos, anchor))
    acts, inv = [], []
    try:
        for r in parts:
            pad = [pd for pd in b.parts[r].pads if pd.net == net][0]
            sp = _spots(st, r, pad.id, anchor, 1)
            if not sp:
                continue
            _, x, y, rot = sp[0]
            a = A.Action("move", ref=r, x=x, y=y, rot=rot)
            errs, _w = A.check(w, a)
            if errs:
                continue
            i, _n = A.apply(w, a, check_first=False)
            inv.append(i)
            acts.append(a)
    finally:
        for i in reversed(inv):
            A.apply(w, i, check_first=False)
    return acts or None


def realize(G, target):
    """Action sequence (assign into free pins first, swaps for cycles) turning G.current into `target` (net->pin)."""
    cur = dict(G.current)
    pend = {n: p for n, p in target.items() if cur.get(n) != p}
    acts = []
    guard = 0
    while pend and guard < 50:
        guard += 1
        prog = False
        for n, p in list(pend.items()):
            if p not in cur.values():
                acts.append(A.Action("assign", group=G.id, net=n, pin=p))
                cur[n] = p
                del pend[n]
                prog = True
        if not prog:
            n, p = next(iter(pend.items()))
            holder = [m for m, q in cur.items() if q == p][0]
            acts.append(A.Action("swap_pins", group=G.id, a=n, b=holder))
            cur[n], cur[holder] = cur[holder], cur[n]
            for x in (n, holder):
                if x in pend and cur[x] == pend[x]:
                    del pend[x]
    return acts


def pin_candidates(st, gid, nets=None, top=8):
    """Single assigns / swaps ranked by angular gain, a couple of combinations, and the monotone full assignment."""
    w = st.world
    b, cfg = w.board, w.cfg
    G = w.groups().get(gid)
    if G is None:
        raise A.ActionError("unknown group '%s'" % gid)
    ref = cfg["mcu"]
    part = b.parts[ref]
    rn = st.eng.rn_view()
    arr = swaps.arrivals(b, rn, ref, G.nets)
    pos = {}
    for pd in part.pads:
        pos.setdefault(pd.num, pd.pos)
    cost = lambda n, p: _adiff(_ang(part.pos, arr[n][0]), _ang(part.pos, pos[p]))
    used = set(G.current.values())
    occupied = {pd.num for pd in part.pads if pd.net}
    singles = []
    for n in (nets or G.nets):
        if n not in arr or n not in G.current:
            continue
        if G.kind == "pin_assignment":
            for p in G.pins:
                if p not in occupied:
                    singles.append((cost(n, p) - cost(n, G.current[n]), [A.Action("assign", group=gid, net=n, pin=p)], {n: p}))
    names = list(G.current)
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            if x in arr and y in arr and (nets is None or x in nets or y in nets):
                dlt = cost(x, G.current[y]) + cost(y, G.current[x]) - cost(x, G.current[x]) - cost(y, G.current[y])
                singles.append((dlt, [A.Action("swap_pins", group=gid, a=x, b=y)], {x: G.current[y], y: G.current[x]}))
    singles.sort(key=lambda s: s[0])
    out = [s[1] for s in singles[:top] if s[0] < 0]
    # greedy combination of the best non-conflicting moves
    combo, usedn, usedp = [], set(), set()
    for dlt, acts, mv in singles:
        if dlt >= 0:
            break
        if usedn & set(mv) or usedp & set(mv.values()):
            continue
        combo += acts
        usedn |= set(mv)
        usedp |= set(mv.values())
        if len(combo) in (2, 4, 8):
            out.append(list(combo))
    if nets is None:
        s = swaps.suggest(b, rn, G, cfg, ref)
        if s["assignment"]:
            full = realize(G, s["assignment"])
            if full:
                out.append(full)
    return out


def _zone_gap(b, net, pt):
    if A._in_zone(b, net, pt):
        return 0.0
    return min((geom.pt_seg_dist(pt, p, q) for z in b.zones if z.net == net for zp in z.polys
                for p, q in zip(zp['outer'], zp['outer'][1:] + zp['outer'][:1])), default=float('inf'))


def plane_candidates(st, net_filter=None, per_island=1):
    """Plane vias for islands that lack plane access: sample spots around the island's pads."""
    w = st.world
    b = w.board
    rl = b.rules
    out, notes = [], []
    for I in st.eng.stub_islands():
        if net_filter and I.net != net_filter:
            continue
        found = []
        for pid in I.pads[:3]:
            pd = A._pad_by_id(b, pid)
            if pd is None:
                continue
            obs = A.local_obstacles(b, pd.pos, 7.0)
            lay = A._stub_layer(w, pd)
            for r in (0.8, 1.1, 1.5, 2.0, 2.6, 3.4, 4.2, 5.0, 6.0):
                for j in range(32):
                    t = 2 * math.pi * j / 32
                    pt = (round(pd.pos[0] + r * math.cos(t), 2), round(pd.pos[1] + r * math.sin(t), 2))
                    if I.net in b.plane_nets() and not A._in_zone(b, I.net, pt):
                        continue
                    if A._clearance_issues(w, I.net, pt, rl.via_dia / 2, obs=obs):
                        continue
                    if A._clearance_issues(w, I.net, None, 0, layers=[lay], seg=(pd.pos, pt), w=rl.track_width, obs=obs):
                        continue
                    found.append((r, pid, pt))
                    break
                if found and found[-1][1] == pid:
                    break
        found.sort()
        if not found:
            gap = _zone_gap(b, I.net, I.pts[0])
            notes.append("%s %s: no legal via spot (%s); needs a routed stub or stitching track" % (
                I.net, "/".join(I.pads), "%.0f mm outside the %s zone" % (gap, I.net) if gap > 0 else "inside the zone but boxed in within 6 mm"))
        for r, pid, pt in found[:per_island]:
            out.append([A.Action("add_via", net=I.net, x=pt[0], y=pt[1], pad=pid)])
    return out, notes


def _crossing_nets(st, region=None):
    eng = st.eng
    cnt = defaultdict(int)
    for cid, r in eng.recs.items():
        for o in r.x:
            if cid < o:
                if region:
                    p = geom.seg_intersect(r.conn.pa, r.conn.pb, eng.recs[o].conn.pa, eng.recs[o].conn.pb)
                    if not p or eng.T.region_at(p).id != region:
                        continue
                cnt[r.conn.net] += 1
                cnt[eng.recs[o].conn.net] += 1
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))


def clusters(st):
    nets = sorted({n for k in st.eng.crossing_pairs() for n in k})
    shim = types.SimpleNamespace(matrix=lambda: (nets, st.eng.crossing_pairs()))
    return topo.Topo.clusters(shim)


def net_candidates(st, net, limit_parts=6):
    w = st.world
    b = w.board
    cands = []
    parts = []
    for pd in b.pads_of_net(net):
        if pd.ref not in parts and _movable(b, pd.ref):
            parts.append(pd.ref)
    for r in parts[:limit_parts]:
        cands += part_candidates(st, r)
    rc = rc_set_candidate(st, net)
    if rc:
        cands.append(rc)
    for gid, G in w.groups().items():
        if G.kind in ("pin_permutation", "pin_assignment") and net in G.nets:
            cands += pin_candidates(st, gid, nets=[net], top=4)
    return cands


def suggest(st, target, n=10, max_eval=40, budget_s=20.0):
    """Generate candidates for `target`, evaluate them, return (ranked [Result], notes [str]). Never applies."""
    t0 = time.time()
    w = st.world
    b = w.board
    notes, cands = [], []
    tl = target.strip()
    if tl.lower() == "audit":                 # the placement linter's fixes as an eval table (rk/audit.py)
        from . import audit
        return audit.suggest(st, n, max_eval, budget_s)
    groups = {g: G for g, G in w.groups().items() if G.kind in ("pin_permutation", "pin_assignment")}
    kind = None
    if ":" in tl and tl.split(":", 1)[0].lower() in ("part", "region", "net", "group", "cluster", "planes", "plane"):
        kind, tl = tl.split(":", 1)
        kind = kind.lower()
        if kind in ("plane", "planes"):
            tl = "planes:" + tl
    kinds = []
    low = tl.lower()
    if kind:
        kinds = [kind if kind != "plane" else "planes"]
        if tl.startswith("planes:"):
            kinds = ["planes"]
    elif low in groups:
        kinds = ["group"]
    elif low.startswith("plane") or low in ("vias", "stubs"):
        kinds = ["planes"]
    elif low == "mcu" or (w.cfg["mcu"] and low == w.cfg["mcu"].lower()):
        kinds = ["mcu"]
    else:
        if tl in b.parts:
            kinds.append("part")
        if tl.upper() in w.regions:
            kinds.append("region")
        if tl in st.eng.nets or tl in b.nets:
            kinds.append("net")
        if re.match(r"^K\d+$", tl, re.I):
            kinds.append("cluster")
    if not kinds:
        raise A.ActionError("cannot suggest for '%s': use a net, part ref, group (%s), cluster K#, region R#, planes, or mcu (the configured MCU part). "
                            "Prefix with part:/region:/net: when a name is ambiguous" % (tl, ",".join(groups)))
    if len(kinds) > 1:
        notes.append("'%s' is ambiguous (%s): trying all; use %s:%s to pick one" % (tl, "+".join(kinds), kinds[0], tl))
    for k in kinds:
        if k == "group":
            cands += pin_candidates(st, low)
            if low == "adc":
                for net in groups["adc"].nets:
                    rc = rc_set_candidate(st, net)
                    if rc:
                        cands.append(rc)
        elif k == "planes":
            nf = tl.split(":", 1)[1] if ":" in tl else None
            c2, notes2 = plane_candidates(st, nf or None)
            cands += c2
            notes += notes2
        elif k == "part":
            if tl not in b.parts:
                raise A.ActionError("no part '%s'" % tl)
            if not _movable(b, tl):
                notes.append("%s is locked/large: not suggesting moves for it" % tl)
            else:
                cands += part_candidates(st, tl, keep=3)
        elif k == "net":
            cands += net_candidates(st, tl)
        elif k == "cluster":
            cl = [c for c in clusters(st) if c["id"] == tl.upper()]
            if not cl:
                raise A.ActionError("no cluster %s now (have %s)" % (tl, " ".join(c["id"] for c in clusters(st))))
            notes.append("cluster %s: %s" % (tl.upper(), " ".join(cl[0]["nets"])))
            for net in cl[0]["nets"]:
                cands += net_candidates(st, net, limit_parts=3)
        elif k == "region":
            if tl.upper() not in w.regions:
                raise A.ActionError("no region '%s'" % tl)
            worst = _crossing_nets(st, tl.upper())[:6]
            notes.append("region %s worst nets by crossings: %s" % (tl.upper(), " ".join("%s(%d)" % kv for kv in worst)))
            for net, _ in worst:
                cands += net_candidates(st, net, limit_parts=3)
        elif k == "mcu":
            for g in groups:
                cands += pin_candidates(st, g)
            pc, notes2 = plane_candidates(st)
            cands += pc
            notes += notes2
            for net, _ in _crossing_nets(st, st.eng.T.region_at(b.parts[w.cfg["mcu"]].pos).id)[:5]:
                cands += net_candidates(st, net, limit_parts=2)
    # dedupe, cap
    seen, uniq = set(), []
    for c in cands:
        key = "; ".join(a.compact() for a in c)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    notes.append("%d candidate(s) generated" % len(uniq))
    if len(uniq) > max_eval:
        notes.append("evaluating the first %d (raise with --max-eval)" % max_eval)
        uniq = uniq[:max_eval]
    base = st.metrics()
    res = []
    for c in uniq:
        if time.time() - t0 > budget_s:
            notes.append("time budget hit")
            break
        res.append(_eval_one(st, c, base))
    ok = sorted([r for r in res if r.ok], key=lambda r: (r.dscore, len(r.acts)))
    if not ok or ok[0].dscore > -0.5:
        notes.append("NO candidate improves the score (best %s): this target is locally optimal for the generators; "
                     "try another target or craft moves with `spots`" % ("%+.1f" % ok[0].dscore if ok else "n/a"))
    dd, keep = set(), []
    for r in ok:                      # same actions in another order / same effect twice: show once
        k = frozenset(a.compact() for a in r.acts)
        if k not in dd:
            dd.add(k)
            keep.append(r)
    ok = keep
    bad = [r for r in res if not r.ok]
    if bad:
        notes.append("%d candidate(s) rejected as illegal" % len(bad))
    return ok[:n], notes
