"""CLI: `badpcb report <board.kicad_pcb | model.json> [--json] [--section NAME]` (run from the board project directory).

Sections: feasibility summary clusters copper cuts inversions planes mcu map  (and, full-only: matrix regions conns).
A .kicad_pcb is read directly (pure python, cached in <badpcb home>/cache by mtime); a .json is loaded directly.
Project settings (badpcb.json), state directory (.badpcb/) and external tools: see badpcb/config.py.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

from . import ascii_map, config, model, ratsnest, swaps, topo


def extract_to_cache(path):
    """.kicad_pcb -> cached model.json (re-extracted when older than the board); returns the model path."""
    cache = config.current().cache_dir
    os.makedirs(cache, exist_ok=True)
    path = os.path.abspath(path)
    tag = hashlib.md5(path.encode()).hexdigest()[:6]            # same basename in two directories must not share a cache entry
    out = os.path.join(cache, "%s.%s.model.json" % (os.path.basename(path).rsplit(".", 1)[0], tag))
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(path):
        from . import extract
        try:
            m = extract.extract(path)
        except (OSError, ValueError) as ex:
            sys.exit("badpcb: cannot read %s: %s" % (path, ex))
        tmp = out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(m, fh, separators=(",", ":"))
        os.replace(tmp, out)
    return out


def load_board(path):
    if path.endswith(".json"):
        return model.load(path)
    return model.load(extract_to_cache(path))


class Ctx:
    def __init__(self, board, assume_planes=(), cell=21.0, rules_pro=None):
        self.board = board
        self.rules_pro = rules_pro
        assume_planes = list(assume_planes)
        self.assumed = []
        if not board.plane_nets() and not assume_planes:      # no zones at all: guess the big supply nets
            counts = {}
            for pd in board.pads():
                if pd.net:
                    counts[pd.net] = counts.get(pd.net, 0) + 1
            assume_planes = sorted(n for n, k in counts.items() if k >= 25)
            self.assumed = assume_planes
        self.rn = ratsnest.build(board, assume_planes)
        self.T = topo.build(board, self.rn, cell)
        self.cfg = swaps.load_config(board=board)
        try:
            self.groups = swaps.groups(board, self.cfg) if self.cfg["mcu"] in board.parts else {}
            self.views = {g: swaps.pin_vs_arrival(board, self.rn, G, self.cfg) for g, G in self.groups.items()
                          if G.kind in ("pin_permutation", "pin_assignment")}
            self.sugg = {g: swaps.suggest(board, self.rn, self.groups[g], self.cfg) for g in self.views}
        except (KeyError, IndexError):        # a board whose MCU pads do not match the swap config
            self.groups, self.views, self.sugg = {}, {}, {}


    @property
    def eff(self):
        """Effective rules: Default netclass + board minimums (.kicad_pro) + simple .kicad_dru area rules (badpcb/feas.py)."""
        if not hasattr(self, "_eff"):
            from . import feas, apply as _ap
            self._eff = feas.load_rules(self.rules_pro or _ap.default_rules_pro(self.board.source), self.board)
        return self._eff


def _nets(items, full, limit=16):
    names = [n for n, _, _ in items]
    s = " ".join(names if full or len(names) <= limit else names[:limit])
    return s + ("" if full or len(names) <= limit else " ...(+%d)" % (len(names) - limit))


def sec_summary(c, full):
    b, T, rn = c.board, c.T, c.rn
    m = T.metrics()
    pl = ", ".join("%s:%s" % (l, "+".join(sorted({z.net for z in b.zones if z.net and l in z.layers}))) for l in b.plane_layers())
    nrt = len({x.net for x in rn.conns})
    L = ["board %.0fx%.0f mm, %d layers (signal %s; planes %s), rules clr %.2f w %.2f via %.2f/%.2f" % (
        b.width, b.height, len(b.layers), "/".join(b.signal_layers()), pl or "none", b.rules.clearance, b.rules.track_width,
        b.rules.via_dia, b.rules.via_drill)] + ["  plane decision, " + x for x in b.plane_layer_report()] + [
        "parts %d, pads %d, tracks %d (%d locked), vias %d, regions %d, cuts %d (+%d ring)" % (
            len(b.parts), sum(1 for _ in b.pads()), len(b.tracks), sum(t.locked for t in b.tracks), len(b.vias), len(T.regions),
            len([x for x in T.cuts.values() if not x.label]), len([x for x in T.cuts.values() if x.label])),
        "METRICS unrouted=%d (%d nets) wirelen=%.0fmm X=%d Xcu=%d(%d locked) overflow sum=%d max=%d inversions=%d plane_stubs=%d" % (
            m["unrouted"], nrt, m["wirelength"], m["crossings"], m["cu_crossings"], m["cu_locked"], m["cut_overflow_sum"],
            m["cut_overflow_max"], m["inversions"], m["plane_stubs"]),
        "  X = ratsnest-vs-ratsnest crossings; Xcu = ratsnest lines crossing existing other-net copper (signal layers)"]
    from . import config as _cfg
    if _cfg.current().intent():                    # only when intent is declared: METRICS stays as it was otherwise
        from . import actions as _A, intent as _in
        w = getattr(c, "intent_world", None) or _A.World(b, c.cfg)
        ip, n = _in.penalty(w)
        mi = next(i for i, x in enumerate(L) if x.startswith("METRICS"))
        L[mi] += " intent=%.1f" % ip
        L.append("  intent = weighted penalty of %d declared intent(s) (0 = all met); details: badpcb audit --only INTENT, badpcb floorplan" % n)
    if c.assumed:
        L.append("  NOTE: board has no zones; assuming plane nets (excluded from routing, need only a via): " + " ".join(c.assumed))
    return L


def sec_clusters(c, full):
    T = c.T
    cl = T.clusters()
    nets, M = T.matrix()
    L = ["conflict clusters (greedy on crossing graph, <=8 nets): %d" % len(cl)]
    for k in cl[: (None if full else 5)]:
        L.append("  %s  %d nets, X inside %d, to others %d: %s" % (k["id"], len(k["nets"]), k["internal"], k["external"], " ".join(k["nets"])))
    top = sorted(M.items(), key=lambda kv: (-kv[1], kv[0]))[: (None if full else 8)]
    L.append("  top crossing pairs: " + "  ".join("%s x %s=%d" % (a, b, v) for (a, b), v in top))
    byreg = {}
    for _, _, p in T.crossings:
        r = T.region_at(p).id
        byreg[r] = byreg.get(r, 0) + 1
    tot = max(1, len(T.crossings))
    L.append("  where: " + "  ".join("%s %d (%d%%)" % (r, n, 100 * n // tot) for r, n in sorted(byreg.items(), key=lambda kv: -kv[1])[:5]))
    mcu = c.cfg.get("mcu")
    ucell = [r.id for r in T.regions.values() if mcu and mcu in r.parts]
    if ucell and byreg.get(ucell[0], 0) > 0.4 * tot:
        L.append("  note: %s holds %s - most crossings are pin escape tangles, see section mcu" % (ucell[0], mcu))
    return L


def sec_cuts(c, full):
    T = c.T
    def key(cut):
        return (-(T.demand(cut.id) / max(1, cut.cap_total)), -T.demand(cut.id), cut.id)
    cuts = sorted(T.cuts.values(), key=key)
    L = ["worst cuts by demand/capacity (cap = tracks fitting on signal layers; nets ordered top->bottom (V) / left->right (H)):"]
    for cut in cuts[: (None if full else 7)]:
        d = T.demand(cut.id)
        if d == 0 and not full:
            break
        name = cut.label or "%s|%s" % (cut.a, cut.b)
        L.append("  %-4s %-9s %s %3.0fmm  dem %2d / cap %3d (%s)%s" % (
            cut.id, name, cut.orient, cut.length, d, cut.cap_total, " ".join("%s %d" % (l.split(".")[0], n) for l, n in cut.cap.items()),
            "  OVERFLOW %d" % T.overflow(cut.id) if T.overflow(cut.id) else ""))
        L.append("       " + _nets(T.cut_nets(cut.id), full))
    return L


def sec_copper(c, full):
    from . import locks
    T, rn, b = c.T, c.rn, c.board
    from collections import Counter
    per = Counter(i for i, _, _ in T.cu_crossings)
    by_net = Counter(n for _, n, _ in T.cu_crossings)
    lk = [t.locked for t in T.cu_cross_tracks]
    nl = sum(lk)
    L = []
    sm = locks.summary(b)
    L.append("existing copper by lock class (tracks+vias; LOCKED = KRT cannot rip it, `unlock <class|net|region>` frees it):")
    for cl in locks.class_names():
        d = sm[cl]
        if d["locked"] + d["unlocked"] == 0:
            continue
        nets = sorted(d["nets"].items(), key=lambda kv: -kv[1])
        L.append("  %-7s %4d locked %4d unlocked  %-38s nets: %s" % (cl, d["locked"], d["unlocked"], locks.label(cl),
                                                                    " ".join("%s:%d" % kv for kv in nets[:(None if full else 8)]) + ("" if full or len(nets) <= 8 else " ...")))
    L.append("ratsnest lines crossing existing foreign copper (each needs a layer hop or detour): %d crossings on %d of %d conns: "
             "%d of them cross LOCKED copper (not rippable: must hop/detour), %d cross rippable copper" % (
                 len(T.cu_crossings), len(per), len(rn.conns), nl, len(lk) - nl))
    bycl = Counter((locks.cls_of(t), t.locked) for t in T.cu_cross_tracks)
    L.append("  crossings by class: " + "  ".join("%s%s %d" % ("LOCKED " if lc else "", cl, n) for (cl, lc), n in bycl.most_common()))
    for i, n in per.most_common(None if full else 6):
        cn = rn.conns[i]
        xs = [(x, t) for (j, x, _), t in zip(T.cu_crossings, T.cu_cross_tracks) if j == i]
        lockedx = Counter(x for x, t in xs if t.locked)
        L.append("  %-9s %s -> %s %4.0fmm crosses %d tracks (%s)%s" % (cn.net, cn.a, cn.b, cn.length, n, " ".join(
            "%s x%d" % kv for kv in Counter(x for x, _ in xs).most_common(4)),
            ("  LOCKED: " + " ".join("%s x%d" % kv for kv in lockedx.most_common(4))) if lockedx else "  (all rippable)"))
    L.append("  crossed nets: " + "  ".join("%s %d%s" % (n, k, "L" if any(t.locked for (j, x, _), t in zip(T.cu_crossings, T.cu_cross_tracks) if x == n) else "")
                                           for n, k in by_net.most_common(None if full else 10)) + "   (L = some of it locked)")
    # locked walls blocking each open net: share of the straight path inside clearance of foreign copper
    blk = []
    for i, cn in enumerate(rn.conns):
        if True:
            d = locks.describe_blockage(b, cn)
            if d:
                blk.append((locks.locked_blockage(b, cn)[0], d))
    blk.sort(key=lambda x: -x[0])
    if blk:
        L.append("locked walls (share of the straight path within clearance of LOCKED copper, worst signal layer; %d conns):" % len(blk))
        for _, d in blk[:(None if full else 8)]:
            L.append("  " + d)
    return L


def sec_inversions(c, full):
    inv = c.T.inversions(top=50 if full else 4)
    L = ["worst inversions (cut pairs whose common nets change order => they cross between the cuts):"]
    if not inv:
        L.append("  none (no two cuts share crossing nets in conflicting order; crossings happen inside single regions, see clusters/u1)")
    for r in inv:
        pr = r["pairs"]
        L.append("  %s -> %s (%s -> %s): %d common conns, %d inversions: %s" % (
            r["cuts"][0], r["cuts"][1], r["regions"][0], r["regions"][1], r["n_common"], r["inversions"],
            "  ".join("%s x %s" % p for p in (pr if full else pr[:6])) + ("" if full or len(pr) <= 6 else " ..."))) 
    return L


def sec_planes(c, full):
    rn = c.rn
    L = ["plane nets (connected through planes; unserved pads need a via/stub):"]
    for n, (s, u) in sorted(rn.plane_nets.items()):
        L.append("  %-6s served islands %d, unserved %d" % (n, s, u))
    from . import geom
    by = {}
    for I in rn.stubs:
        zs = [z for z in c.board.zones if z.net == I.net]
        tag = ""
        if zs:   # distance from the island to the nearest outline of its net's zones (0 = inside: just needs a via)
            p = I.pts[0]
            d = min(geom.pt_seg_dist(p, a, b2) for z in zs for zp in z.polys for a, b2 in zip(zp["outer"], zp["outer"][1:] + zp["outer"][:1]))
            inside = any(geom.point_in_poly(p, zp["outer"]) for z in zs for zp in z.polys)
            tag = "" if inside else "(%.0fmm outside zone)" % d
        by.setdefault(I.net, []).append("/".join(I.pads) + tag)
    for n, lst in sorted(by.items()):
        L.append("  needs via %s: %s" % (n, " ".join(lst if full or len(lst) <= 10 else lst[:10] + ["..."])))
    return L


def sec_mcu(c, full):
    b = c.board
    ref = c.cfg["mcu"]
    if ref not in b.parts:
        return ["no MCU part (set \"mcu\" in badpcb.json%s)" % ("; '%s' is not on this board" % ref if ref else "")]
    pc = swaps.pin_cycle(b, ref, c.cfg)
    plane = set(b.plane_nets())
    sig = [(n, nm, net) for n, nm, net, _ in pc if net and net not in plane]
    L = ["%s pins clockwise (screen, +y down), signals only: %s" % (ref, " ".join("%s:%s" % (n, net) for n, _, net in sig))]
    free = [n for n, _, net, _ in pc if not net]
    L.append("  free (unconnected) pins: %d  e.g. %s" % (len(free), " ".join(free[:12]) + (" ..." if len(free) > 12 else "")))
    for gid, v in c.views.items():
        G = c.groups[gid]
        L.append("%s group '%s': %s" % (ref, gid, G.desc))
        L.append("  pin order   (cw): " + " ".join("%s@%s%s" % (n, p, s) for n, p, s in v["pin_order"]))
        L.append("  arrival ord (cw): " + " ".join("%s<-%s(%s)" % (n, s, lab) for n, s, lab in v["arrival_order"]))
        L.append("  inversions %d (best cyclic shift)%s; %d/%d nets arrive from a different side than their pin (cross the package)" % (
            v["inversions"], ": " + " ".join("%sx%s" % p for p in (v["pairs"] if full else v["pairs"][:8])) if v["pairs"] else "",
            len(v["over_package"]), len(v["pin_order"])))
        s = c.sugg[gid]
        if s["moves"]:
            L.append("  suggest: angular mismatch %d -> %d deg by moving %d: %s" % (
                s["cost_before_deg"], s["cost_after_deg"], len(s["moves"]),
                " ".join("%s:%s>%s" % (n, a, bb) for n, (a, bb) in sorted(s["moves"].items(), key=lambda kv: int(kv[1][1])))))
    for cut in c.T.cuts.values():
        if cut.label.startswith(ref + " ring"):
            d = c.T.demand(cut.id)
            L.append("  %-12s %s dem %2d / cap %2d  %s" % (cut.label, cut.id, d, cut.cap_total, _nets(c.T.cut_nets(cut.id), full, 10)))
    return L


def sec_map(c, full):
    return ascii_map.render(c.T).split("\n")


def sec_matrix(c, full):
    nets, M = c.T.matrix()
    L = ["crossing matrix (nonzero pairs, ratsnest x ratsnest):"]
    for (a, b), v in sorted(M.items(), key=lambda kv: (-kv[1], kv[0])):
        L.append("  %-10s %-10s %d" % (a, b, v))
    return L


def sec_regions(c, full):
    load = c.T.region_load()
    L = ["regions (bbox x0 y0 x1 y1 | keys | big parts | pads | conns through | X inside):"]
    for R in c.T.regions.values():
        L.append("  %-4s %6.1f %5.1f %6.1f %5.1f | %2d | %-8s | %3d | %3d | %3d" % (
            R.id, *R.bbox, R.n_keys, ",".join(R.parts), R.n_pads, load[R.id]["conns"], load[R.id]["cross"]))
    return L


def sec_conns(c, full):
    L = ["ratsnest connections (net a b length):"]
    for x in sorted(c.rn.conns, key=lambda x: (x.net, x.a)):
        L.append("  %-10s %-10s %-10s %6.1f" % (x.net, x.a, x.b, x.length))
    return L


def sec_feasibility(c, full):
    from . import feas
    return feas.sec_feasibility(c, full)


SECTIONS = {"feasibility": sec_feasibility, "summary": sec_summary, "clusters": sec_clusters, "copper": sec_copper, "cuts": sec_cuts, "inversions": sec_inversions,
            "planes": sec_planes, "mcu": sec_mcu, "u1": sec_mcu, "map": sec_map, "matrix": sec_matrix, "regions": sec_regions, "conns": sec_conns}
DEFAULT = ["feasibility", "summary", "clusters", "copper", "cuts", "inversions", "planes", "mcu", "map"]


def as_json(c):
    T = c.T
    load = T.region_load()
    return {"metrics": T.metrics(),
            "clusters": T.clusters(),
            "cuts": [{"id": x.id, "a": x.a, "b": x.b, "label": x.label, "orient": x.orient, "p0": x.p0, "p1": x.p1,
                      "cap": x.cap, "demand": T.demand(x.id), "nets": T.cut_nets(x.id)} for x in T.cuts.values()],
            "regions": [{"id": r.id, "bbox": r.bbox, "parts": r.parts, "keys": r.n_keys, "load": load[r.id]} for r in T.regions.values()],
            "inversions": T.inversions(top=50),
            "plane_nets": c.rn.plane_nets,
            "plane_stubs": [{"net": i.net, "pads": i.pads} for i in c.rn.stubs],
            "mcu": {"views": c.views, "suggest": c.sugg},
            "conns": [x.as_dict() for x in c.rn.conns]}


# ----------------------------------------------------------------------------- session commands
def _load(name):
    from . import state
    return state.State.load(name)


def _line(tag, res, extra=""):
    from . import state
    return "[%s] %s%s" % (tag, state.fmt_delta(res["delta"]), extra)


def cmd_session(a):
    from . import state
    if a.sub == "new":
        st = state.State.new(a.name, a.board, [x for x in a.assume_planes.split(",") if x], a.cell, overwrite=a.force)
        print("session %s created: %s" % (a.name, st.path))
        print("METRICS " + state.fmt_metrics(st.metrics()))
    elif a.sub == "ls":
        d = state.session_dir()
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if f.endswith(".json"):
                with open(os.path.join(d, f)) as fh:
                    j = json.load(fh)
                print("%-14s %d steps, branch %s, %d snapshots, board %s" % (j["name"], len(j["log"]), j.get("branch", "main"),
                                                                           len(j.get("snapshots", {})), os.path.basename(j["board"])))
    elif a.sub == "rm":
        os.remove(state.session_path(a.name))


def cmd_act(a):
    from . import state, actions
    st = _load(a.session)
    for spec in a.specs:
        r = st.act(spec, force=a.force)
        print("[step %d] %s  =>  %s" % (r["step"], "; ".join(r["applied"]), state.fmt_delta(r["delta"])))
        for w in r["warnings"]:
            print("  warn: " + w)
    print("METRICS " + state.fmt_metrics(st.metrics()))


def cmd_eval(a):
    from . import evaluate, state
    st = _load(a.session)
    t0 = time.time()
    res = evaluate.evaluate(st, a.cands)
    print("what-if (nothing applied) from step %d: %s" % (st.step(), state.fmt_metrics(st.metrics())))
    print("\n".join(evaluate.format_table(res)))
    st.last = [r.label for r in res]
    st.save()
    print("(%d candidates, %.2fs)" % (len(res), time.time() - t0), file=sys.stderr)


def cmd_suggest(a):
    from . import evaluate, state
    st = _load(a.session)
    t0 = time.time()
    if getattr(a, "gscore", 0.0):
        from . import audit as _au
        _au.GSCORE = a.gscore
    res, notes = evaluate.suggest(st, a.target, a.n, a.max_eval)
    print("suggestions for %s (nothing applied; %s):" % (a.target, "audit order, rows cumulative" if a.target.lower() == "audit" else "ranked by dscore, lower is better"))
    for n in notes:
        print("  # " + n)
    print("\n".join(evaluate.format_table(res)))
    st.last = [r.label for r in res]
    st.save()
    if res:
        print("apply row N with: badpcb act %s @N" % a.session)
    print("(%.2fs)" % (time.time() - t0), file=sys.stderr)


def cmd_undo(a):
    from . import state
    st = _load(a.session)
    r = st.undo(a.n)
    print("undone: %s  =>  %s" % ("; ".join(r["undone"]), state.fmt_delta(r["delta"])))
    print("METRICS " + state.fmt_metrics(st.metrics()))


def cmd_snapshot(a):
    st = _load(a.session)
    s = st.snapshot(a.name)
    print("snapshot '%s' at step %d" % (a.name, s["step"]))


def cmd_revert(a):
    from . import state
    st = _load(a.session)
    r = st.revert(a.target)
    print("reverted to %s (step %d)  =>  %s" % (a.target, r["step"], state.fmt_delta(r["delta"])))
    print("METRICS " + state.fmt_metrics(st.metrics()))


def cmd_branch(a):
    st = _load(a.session)
    st.branch(a.name)
    print("now on branch '%s' (forked at step %d); main/other branches kept: badpcb switch %s <name>" % (a.name, st.step(), a.session))


def cmd_switch(a):
    from . import state
    st = _load(a.session)
    st.switch(a.name)
    print("on branch '%s', step %d" % (a.name, st.step()))
    print("METRICS " + state.fmt_metrics(st.metrics()))


def cmd_diff(a):
    from . import state
    st = _load(a.session)
    d = st.diff(a.a, a.b)
    print("diff %s -> %s: %d common step(s)" % (a.a, a.b, d["common_steps"]))
    lim = None if a.full else 14
    for tag, L in (("-", d["only_a"]), ("+", d["only_b"])):
        for x in L[:lim]:
            print("  %s %s" % (tag, x))
        if lim and len(L) > lim:
            print("  %s ... %d more (--full)" % (tag, len(L) - lim))
    print("  A: " + state.fmt_metrics(d["a"]))
    print("  B: " + state.fmt_metrics(d["b"]))
    print("  delta: " + state.fmt_delta(d["delta"]))


def cmd_log(a):
    from . import state
    st = _load(a.session)
    print("session %s branch %s step %d" % (st.name, st.branch_name, st.step()))
    for i, lab, m in st.history():
        print("%2d %-50s %s" % (i, lab[:50], state.fmt_metrics(m)))
    for k, v in st.snapshots.items():
        print("snapshot %-10s step %d" % (k, v["step"]))
    if st.branches:
        print("other branches: " + ", ".join("%s(%d)" % (k, len(v)) for k, v in st.branches.items()))


def cmd_show(a):
    from . import state, actions, evaluate
    st = _load(a.session)
    b = st.world.board
    if not a.what:
        print("session %s branch %s step %d  board %s" % (st.name, st.branch_name, st.step(), os.path.basename(st.board_path)))
        print("METRICS " + state.fmt_metrics(st.metrics()))
        for h in actions.describe_hints(st.world):
            print("  hint: " + h)
        from . import intent as _in
        for h in _in.describe(st.world):
            print("  intent: " + h)
        return
    w = a.what
    if w in b.parts:
        p = b.parts[w]
        print("%s %s %s at (%.2f, %.2f) rot %g side %s%s  courtyard %.1f..%.1f x %.1f..%.1f" % (
            w, p.value, p.fpid, p.pos[0], p.pos[1], p.rot, p.side, " LOCKED" if p.locked else "", *p.bbox))
        for pd in p.pads:
            print("  pad %-3s (%.2f, %.2f) net %s" % (pd.num, pd.pos[0], pd.pos[1], pd.net or "-"))
        for net, pad, pt, ln, lab in evaluate.neighbours(st, w):
            print("  %s: %s -> %s at (%.1f, %.1f)  %.1f mm" % (net, pad, lab, pt[0], pt[1], ln))
    elif w in st.eng.nets or w in b.nets:
        cx = st.eng.crossings_by_net().get(w, 0)
        print("net %s: %d pads, %d islands, %d conns, X %d%s" % (
            w, len(b.pads_of_net(w)), len(st.eng.nets.get(w, {}).get("islands", [])), len(st.eng.nets.get(w, {}).get("cids", [])), cx,
            " (plane net)" if w in st.eng.planes else ""))
        for pd in b.pads_of_net(w)[:40]:
            print("  %-8s (%.1f, %.1f)" % (pd.id, *pd.pos))
        for cid in st.eng.nets.get(w, {}).get("cids", []):
            c = st.eng.recs[cid].conn
            print("  conn %s -> %s  %.1f mm  (%.1f,%.1f)-(%.1f,%.1f) X=%d" % (c.a, c.b, c.length, *c.pa, *c.pb, len(st.eng.recs[cid].x)))
    else:
        print("no part or net '%s'" % w)


def cmd_spots(a):
    from . import evaluate, actions
    st = _load(a.session)
    b = st.world.board
    if a.ref not in b.parts:
        raise actions.ActionError("no part '%s'" % a.ref)
    p = b.parts[a.ref]
    pad = [pd for pd in p.pads if pd.num == a.pad][0] if a.pad else p.pads[0]
    print("legal placements of %s putting pad %s within a few mm of (%.1f, %.1f):" % (a.ref, pad.num, a.x, a.y))
    cands = evaluate._spots(st, a.ref, pad.id, (a.x, a.y), keep=8)
    acts = [[actions.Action("move", ref=a.ref, x=x, y=y, **({"rot": rot} if abs(actions._norm(rot - p.rot)) > 1e-6 else {}))] for r, x, y, rot in cands]
    res = evaluate.evaluate(st, acts)
    print("\n".join(evaluate.format_table(res)))
    st.last = [r.label for r in res]
    st.save()


def cmd_report_session(a, st):
    from . import state, actions
    c = Ctx(st.world.board, st.assume, st.cell)
    print("session %s branch %s step %d  (report recomputes regions/capacities from the current board; act/eval METRICS use the frozen session grid)" % (
        st.name, st.branch_name, st.step()))
    print("METRICS " + state.fmt_metrics(st.metrics()))
    c.intent_world = st.world
    hs = actions.describe_hints(st.world)
    if hs:
        print("  %d hint(s) recorded for groute (badpcb show %s)" % (len(hs), st.name))
    return c


def main(argv=None):
    try:                                   # progress lines must show up when piped (droute passes run minutes; found by the board lane)
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="badpcb", description="routing kernel CLI. Project config: badpcb.json (cwd or --project); state in .badpcb/ (sessions, work, cache; BADPCB_HOME overrides)")
    ap.add_argument("--project", default=None, metavar="PATH", help="badpcb.json (or the directory holding it); default: badpcb.json in the cwd or a parent, else $BADPCB_PROJECT, else defaults")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="print the topology report for a board file or a session")
    r.add_argument("board", help=".kicad_pcb / model.json path, or a session name")
    r.add_argument("--json", action="store_true")
    r.add_argument("--section", choices=sorted(SECTIONS))
    r.add_argument("--full", action="store_true", help="no truncation (default for sections is the same short form as the full report)")
    r.add_argument("--assume-planes", default="", help="comma list of nets to treat as plane nets when the board has no zones, e.g. GND,+3V3,+3V3A")
    r.add_argument("--rules-pro", default=None, help=".kicad_pro (+ sibling .kicad_dru) giving the effective rules (default: rules.project in badpcb.json, else the board's own .kicad_pro)")
    r.add_argument("--cell", type=float, default=21.0, help="target region size in mm")
    s = sub.add_parser("session", help="new NAME BOARD | ls | rm NAME")
    s.add_argument("sub", choices=["new", "ls", "rm"])
    s.add_argument("name", nargs="?")
    s.add_argument("board", nargs="?")
    s.add_argument("--force", action="store_true")
    s.add_argument("--assume-planes", default="")
    s.add_argument("--cell", type=float, default=21.0)
    x = sub.add_parser("act", help="apply actions: badpcb act S 'move R17 152 40.5' ['rot C27 90' ...] (each arg = one step; ';' = atomic group)")
    x.add_argument("session")
    x.add_argument("specs", nargs="+")
    x.add_argument("--force", action="store_true", help="override locked-part checks")
    x = sub.add_parser("eval", help="what-if: badpcb eval S 'move R17 152 40' 'rot C27 90; move C27 150 20' ... (nothing applied)")
    x.add_argument("session")
    x.add_argument("cands", nargs="+")
    x = sub.add_parser("suggest", help="ranked candidates for a net / part / group (adc,en) / cluster K1 / region R8 / planes / the MCU ref")
    x.add_argument("session")
    x.add_argument("target")
    x.add_argument("-n", type=int, default=10)
    x.add_argument("--max-eval", type=int, default=40)
    x.add_argument("--gscore", nargs="?", const=60.0, type=float, default=0.0, metavar="SECONDS", help="target 'audit': rank fixes by a groute score (badpcb/gscore.py)")
    x = sub.add_parser("audit", help="placement linter: badpcb audit <board|session> [--json] [--full] [--roles] [--groute] [--only ESCAPE,CHANNEL,PLANE,..]")
    x.add_argument("target", help=".kicad_pcb / model.json path or a session name")
    x.add_argument("--json", action="store_true")
    x.add_argument("--full", action="store_true")
    x.add_argument("--roles", action="store_true", help="print the inferred part roles (decap, filter cap, crystal load, ...)")
    x.add_argument("--groute", action="store_true", help="also run groute (1 mm, short) and compare its hot spots with the audit's")
    x.add_argument("--only", default="", help="comma list of detectors: ESCAPE,CHANNEL,PLANE,BOWTIE,ORIENT,TENSION,ROLES,INTENT,BUSORDER,COURTYARD,DENSITY")
    x.add_argument("--gscore", nargs="?", const=60.0, type=float, default=0.0, metavar="SECONDS", help="re-rank each finding's fixes by a short groute run on its region (badpcb/gscore.py), within this time budget")
    x.add_argument("--joint", nargs="?", const="", default=None, metavar="REF[,REF]", help="instead of the audit: one joint placement plan for the IC's fan intruders + decaps/filter caps (badpcb/joint.py)")
    x.add_argument("--edge", type=float, default=1.5, help="--joint: keep parts' courtyards this far (mm) from the board edge")
    x.add_argument("--minimal", action="store_true", help="--joint: move only the parts that block a signal stub + their filter caps")
    x = sub.add_parser("plan", help="joint placement plan for an IC: badpcb plan <session|board> [REF[,REF]] [--edge 1.5]  (= audit --joint)")
    x.add_argument("target")
    x.add_argument("refs", nargs="?", default="")
    x.add_argument("--edge", type=float, default=1.5)
    x.add_argument("--minimal", action="store_true", help="move only the parts that block a signal stub of the fan + their filter caps")
    x = sub.add_parser("styles", help="resolved net style per net (badpcb.json net_styles, KiCad netclasses): badpcb styles BOARD [--json]")
    x.add_argument("board")
    x.add_argument("--json", action="store_true")
    x = sub.add_parser("floorplan", help="step-2 view: blocks, fill, block nets/crossings, connectors, what faces each fine-pitch IC side, long links: badpcb floorplan <board|session> [--json] [--depth 8]")
    x.add_argument("target", help=".kicad_pcb / model.json path or a session name")
    x.add_argument("--json", action="store_true")
    x.add_argument("--depth", type=float, default=8.0, help="mm: corridor out from each fine-pitch IC side in which blocks 'face' it (the fan zone itself is 2-3 mm)")
    x = sub.add_parser("groute", help="global router (PathFinder) on a session's current state + its hints, or a board: badpcb groute S [--region R8] [--nets EN*] [--time 30] [--no-map]")
    x.add_argument("target")
    x.add_argument("--cell", type=float, default=1.0)
    x.add_argument("--iters", type=int, default=30)
    x.add_argument("--time", type=float, default=30.0)
    x.add_argument("--hints")
    x.add_argument("--nets")
    x.add_argument("--region")
    x.add_argument("--json", dest="json_out")
    x.add_argument("--via-cost", type=float, default=6.0)
    x.add_argument("--stall", type=int, default=8)
    x.add_argument("--esc-cap", type=int, default=2)
    x.add_argument("--max-blocked", type=int, default=25)
    x.add_argument("--no-map", action="store_true")
    x.add_argument("-v", "--verbose", action="store_true")
    x = sub.add_parser("undo")
    x.add_argument("session")
    x.add_argument("n", nargs="?", type=int, default=1)
    x = sub.add_parser("snapshot")
    x.add_argument("session")
    x.add_argument("name")
    x = sub.add_parser("revert", help="revert to step N or a snapshot name (or 'base')")
    x.add_argument("session")
    x.add_argument("target")
    x = sub.add_parser("branch", help="fork the current state into a new named branch")
    x.add_argument("session")
    x.add_argument("name")
    x = sub.add_parser("switch", help="switch to another branch")
    x.add_argument("session")
    x.add_argument("name")
    x = sub.add_parser("diff", help="compare two refs: base | current | N | snapshot | branch")
    x.add_argument("session")
    x.add_argument("a", nargs="?", default="base")
    x.add_argument("b", nargs="?", default="current")
    x.add_argument("--full", action="store_true")
    x = sub.add_parser("log", help="trajectory: metrics after each step")
    x.add_argument("session")
    x = sub.add_parser("spots", help="legal places for a part near a point: badpcb spots S R26 157 14 [--pad 2]")
    x.add_argument("session")
    x.add_argument("ref")
    x.add_argument("x", type=float)
    x.add_argument("y", type=float)
    x.add_argument("--pad", default="")
    x = sub.add_parser("show", help="session status; or details of a part (R17) or net (ADC0)")
    x.add_argument("session")
    x.add_argument("what", nargs="?")
    x = sub.add_parser("apply", help="write a session's final state to a .kicad_pcb: badpcb apply S out.kicad_pcb [--in board] [--no-verify]")
    x.add_argument("session")
    x.add_argument("out")
    x.add_argument("--in", dest="src", default=None)
    x.add_argument("--no-verify", action="store_true")
    x = sub.add_parser("droute", help="detailed routing with KiCadRoutingTools: badpcb droute <session|board> [--nets ..] [--component R] [--region R8] [--preview] [--time S]")
    x.add_argument("target", help="session name or .kicad_pcb")
    x.add_argument("--out", default=None, help="output board (default: <work dir>/droute/<name>/passN.kicad_pcb, chained by default)")
    x.add_argument("--nets", nargs="*", default=[])
    x.add_argument("--component", nargs="*", default=[])
    x.add_argument("--region", default=None, help="region handle (R8) or x0,y0,x1,y1 mm: route the nets with a pad inside")
    x.add_argument("--preview", action="store_true", help="route but write no board; report what would happen")
    x.add_argument("--time", type=float, default=300.0, help="wall-clock cap in seconds")
    x.add_argument("--extra", default="", help="extra raw args for KRT route.py")
    x.add_argument("--fanout", default=None, metavar="REF", help="run KRT qfn_fanout on REF's in-scope nets first (fine-pitch escape stubs)")
    x.add_argument("--rules-pro", default=None, help=".kicad_pro whose rules DRC is graded against (default: rules.project in badpcb.json, else the board's own .kicad_pro)")
    x.add_argument("--grid", default="auto", help="KRT --grid-step mm; auto = coarsest grid that resolves the fine-pitch pads in the scope (badpcb feasibility)")
    x.add_argument("--strip", action="store_true", help="first delete the existing copper of the scope's non-plane nets (so --fanout escapes them all together)")
    x.add_argument("--repair", action="store_true", help="incremental repair of a routed board: rip only the unlocked copper within --radius of the open connection ends, re-route that group (badpcb/repair.py)")
    x.add_argument("--radius", type=float, default=4.0, help="--repair: disk radius (mm) around both ends of each open connection")
    x.add_argument("--fresh", action="store_true", help="restart from the freshly applied session board (drop earlier passes)")
    x.add_argument("--measure", dest="measure_only", action="store_true", help="only print the METRICS line (badpcb + DRC) of the board `target`")
    x.add_argument("--from-board", default=None, help="start from this board instead of the session-applied one")
    sub.add_parser("project", help="show the resolved project config, state directory and external tools")
    a = ap.parse_args(argv)
    config.load(a.project)
    from . import actions
    try:
        if a.cmd == "project":
            return cmd_project(a)
        if a.cmd == "apply":
            from . import apply as _ap
            return _ap.run(a.session, a.out, a.src, not a.no_verify)
        if a.cmd == "droute":
            from . import droute as _dr
            return _dr.main_args(a)
        if a.cmd == "report":
            return report_main(a)
        if a.cmd == "styles":
            from . import styles as _st
            return _st.main_args(a)
        if a.cmd == "plan" or (a.cmd == "audit" and a.joint is not None):
            from . import joint as _jt
            return _jt.main_args(a.target, a.refs if a.cmd == "plan" else a.joint, a.edge, a.minimal)
        if a.cmd == "floorplan":
            from . import floorplan as _fp
            return _fp.main_args(a)
        if a.cmd == "groute":
            from . import gscore as _gs
            return _gs.main_args(a)
        if a.cmd == "audit":
            from . import audit as _au
            return _au.main_args(a)
        fn = {"session": cmd_session, "act": cmd_act, "eval": cmd_eval, "suggest": cmd_suggest, "undo": cmd_undo,
              "snapshot": cmd_snapshot, "revert": cmd_revert, "branch": cmd_branch, "switch": cmd_switch, "diff": cmd_diff,
              "log": cmd_log, "show": cmd_show, "spots": cmd_spots}[a.cmd]
        fn(a)
    except actions.ActionError as e:
        print("ERROR: " + "\n       ".join(e.errors), file=sys.stderr)
        sys.exit(2)


def cmd_project(a):
    P = config.current()
    print("project file : %s" % (P.path or "(none: defaults; state under the cwd)"))
    print("project root : %s" % P.root)
    print("state dir    : %s  (sessions, work, cache)" % P.home)
    print("board        : %s" % (P.board or "-"))
    print("rules project: %s" % (P.rules_pro or "(each board's own .kicad_pro)"))
    print("mcu          : %s    fine-pitch parts: %s" % (P.mcu or "-", ",".join(P.data.get("fine_pitch_parts") or []) or "(auto-detect)"))
    print("swap groups  : %s" % (", ".join(g["id"] for g in P.swaps()["groups"]) or "none"))
    lc = P.lock_classes()
    if not lc:
        print("lock classes : no lock classes (badpcb.json has no \"locks\"; all copper is free, `lock`/`unlock`/`trim` have nothing to act on)")
    else:
        print("lock classes : %s" % ", ".join("%s=%s%s%s" % (k, os.path.basename(v[0]), "".join(" +" + f for f in ("protect", "trim") if P.lock_props(k)[f]),
                                               "" if os.path.exists(v[0]) else " (FILE MISSING: class is empty)") for k, v in lc.items()))
    print("intent       : %s" % (", ".join("%s %d" % (k, n) for k, n in sorted(__import__("collections").Counter(e["kind"] for e in P.intent().values()).items())) or "none"))
    from . import audit
    print("audit        : %s" % (" ".join("%s=%g%s" % (k, v, "*" if k in P.audit() else "") for k, v in audit.params().items()) + "   (* = overridden)"))
    print("kicad-cli    : %s %s" % (config.kicad_cli(), "" if config.kicad_cli_available() else "(NOT FOUND)"))
    from . import droute
    print("KiCadRoutingTools: %s | %s %s" % (droute.krt_dir(), droute.krt_py(), "" if droute.available() else "(not installed: droute unavailable)"))


def report_main(a):
    t0 = time.time()
    st = None
    if os.path.exists(a.board):
        board = load_board(a.board)
        c = Ctx(board, [x for x in a.assume_planes.split(",") if x], a.cell, a.rules_pro)
    else:
        st = _load(a.board)
        c = cmd_report_session(a, st)
    if a.json:
        print(json.dumps(as_json(c), default=list, indent=1))
        return
    names = [a.section] if a.section else DEFAULT
    for n in names:
        print("== %s ==" % n)
        print("\n".join(SECTIONS[n](c, a.full)))
    print("(badpcb report %.1fs)" % (time.time() - t0), file=sys.stderr)


if __name__ == "__main__":
    main()
