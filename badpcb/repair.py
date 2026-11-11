"""Incremental repair of a routed board: rip only the copper around the still-open connections, then re-route that group.

    badpcb droute <session|board> --repair [--radius 4] [--nets ...]

Why: scoped passes on a routed board either leave the blockers alone (KRT only rips what it may and the log shows the same
blockers again) or `--strip` throws away the whole bundle (and re-creates the same congestion elsewhere). `plan()` takes the
still-open connections, draws a disk of `radius` mm around both ends of each, and selects every *unlocked, non-plane* track
and via touching a disk (the failing nets' own dead-end stubs included). Locked copper in a disk is never touched; it is
counted per net so the report can say `unlock NET`. The nets of the ripped copper join the scope of the next KRT pass, which
then re-routes the whole neighbourhood together (the failing nets get first claim because they are ordered first by KRT's
own congestion ordering; the ripped bystanders re-route around them).

`strategy()` turns the pass history (passes.jsonl) into a concrete next step for the ping-pong warning:
plain scoped pass -> repair -> strip+fanout of the group -> change the placement / unlock (the board needs a decision, not another pass).
"""
from collections import Counter

from . import geom


def _near(pt, a, b, r):
    return geom.pt_seg_dist(pt, a, b) <= r


def plan(board, conns, radius=4.0, scope=None, plane_nets=()):
    """conns: ratsnest conns (pa, pb, net). -> dict(ids, nets, tracks, vias, locked {net: n}, ends, failing)."""
    plane = set(plane_nets) or set(board.plane_nets())
    ends = []
    failing = set()
    for c in conns:
        if c.net in plane or (scope and c.net not in scope):
            continue
        failing.add(c.net)
        ends += [c.pa, c.pb]
    ids, nets, locked = set(), set(), Counter()
    nt = nv = 0
    for t in board.tracks:
        if t.net in plane or not t.net:
            continue
        r = radius + t.w / 2
        if any(_near(e, t.a, t.b, r) for e in ends):
            if t.locked:
                locked[t.net] += 1
            elif t.id:
                ids.add(t.id)
                nets.add(t.net)
                nt += 1
    for v in board.vias:
        if v.net in plane or not v.net:
            continue
        if any(geom.dist(e, v.pos) <= radius + v.dia / 2 for e in ends):
            if v.locked:
                locked[v.net] += 1
            elif v.id:
                ids.add(v.id)
                nets.add(v.net)
                nv += 1
    return {"ids": sorted(ids), "nets": sorted(nets | failing), "tracks": nt, "vias": nv, "locked": dict(locked),
            "ends": len(ends), "failing": sorted(failing), "ripped_nets": sorted(nets), "end_pts": [list(e) for e in ends]}


def scope_report(p, requested, bb=None, radius=4.0):
    """Lines telling what scope --repair chose when it differs from the request: the requested nets/region versus the nets it will route
    (open connections of those nets reach far ends outside the region; ripped bystanders join the scope). [] when it stays inside the request."""
    requested = set(requested or ())
    final = set(p["nets"])
    extra = sorted(final - requested)
    out_pts = [e for e in p.get("end_pts", []) if bb is not None and not (bb[0] <= e[0] <= bb[2] and bb[1] <= e[1] <= bb[3])]
    L = []
    if not (extra or out_pts):
        return L
    why = []
    if out_pts:
        xs, ys = [e[0] for e in out_pts], [e[1] for e in out_pts]
        why.append("%d of %d open-connection end(s) lie outside the requested region (x %.0f..%.0f, y %.0f..%.0f)" % (
            len(out_pts), len(p.get("end_pts", [])), min(xs), max(xs), min(ys), max(ys)))
    if extra:
        bystanders = sorted(set(extra) & set(p["ripped_nets"]) - set(p["failing"]))
        why.append("+%d net(s) beyond the request: %s%s" % (len(extra), " ".join(extra[:14]), " ..." if len(extra) > 14 else ""))
        if bystanders:
            why.append("%d of them are bystanders whose copper lies within %.1f mm of an open end" % (len(bystanders), radius))
    L.append("repair scope WIDENED: requested %d net(s)%s -> routing %d net(s). %s. Narrow it with --radius or --nets if that is not what you want." % (
        len(requested), (" in region %.1f,%.1f..%.1f,%.1f" % tuple(bb)) if bb else "", len(final), "; ".join(why)))
    return L


def describe(p, radius):
    s = "repair: %d open conn end(s) of %d net(s); ripped %d track(s) + %d via(s) of %d net(s) within %.1f mm: %s" % (
        p["ends"], len(p["failing"]), p["tracks"], p["vias"], len(p["ripped_nets"]), radius, " ".join(p["ripped_nets"][:14]) or "-")
    if p["locked"]:
        lk = sorted(p["locked"].items(), key=lambda kv: -kv[1])
        s += "\n  locked copper inside the disks (kept; KRT cannot move it): %s -> `unlock %s` in the session to let the repair rip it" % (
            " ".join("%s x%d" % kv for kv in lk[:8]), lk[0][0])
    return s


def open_cuts(ctx):
    """{cut handle: {"id": C12, "nets": [...], "n": open conns, "dem": d, "cap": c}} for the cuts the still-open connections cross.
    The handle is the cut's label ('U1 ring S') or 'regionA|regionB': stable across passes while the placement does not change,
    unlike the names of the nets that fail on it."""
    T, rn = ctx.T, ctx.rn
    out = {}
    for cid, cut in T.cuts.items():
        crossing = T.cut_cross.get(cid, {})
        hit = [i for i in crossing if i < len(rn.conns)]
        if not hit:
            continue
        h = cut.label or "%s|%s" % (cut.a, cut.b)
        e = out.setdefault(h, {"id": cid, "nets": [], "n": 0, "dem": T.demand(cid), "cap": cut.cap_total})
        e["n"] += len(hit)
        e["nets"] = sorted(set(e["nets"]) | {rn.conns[i].net for i in hit})
    return out


def cut_cycle(history, window=3, max_open=8):
    """Ping-pong keyed on the cut handle. history: [{"n": open conns, "cuts": {handle: {"nets": [...], "n": k}}}] oldest first, this pass last.
    Fires when, in each of the last `window` passes, a few connections (<= max_open) are open, ONE cut handle carries at least half of them and
    the nets failing on it are not the same in every pass (the failure rotates over the same cut). -> {"handle", "id", "nets", "per_pass"} or None."""
    h = [x for x in history[-window:] if x.get("cuts") is not None]
    if len(h) < window or any(not (0 < x.get("n", 0) <= max_open) for x in h):
        return None
    common = set(h[0]["cuts"])
    for x in h[1:]:
        common &= set(x["cuts"])
    best = None
    for k in sorted(common):
        if all(x["cuts"][k]["n"] * 2 >= x["n"] for x in h):
            sets = [frozenset(x["cuts"][k]["nets"]) for x in h]
            if len(set(sets)) > 1 and (best is None or sum(x["cuts"][k]["n"] for x in h) > best[0]):
                best = (sum(x["cuts"][k]["n"] for x in h), k, sets)
    if not best:
        return None
    _, k, sets = best
    return {"handle": k, "id": h[-1]["cuts"][k].get("id"), "nets": sorted(set().union(*sets)), "per_pass": [sorted(x) for x in sets],
            "dem": h[-1]["cuts"][k].get("dem"), "cap": h[-1]["cuts"][k].get("cap")}


def cut_cycle_advice(c, target="S", fan_ref=None):
    return ("WARNING ping-pong on cut '%s'%s: the last passes each left a few open connections across it but with different nets (%s). The nets rotate, "
            "the cut is the constant: it cannot take its bundle%s. Another pass will not converge: ESCALATE to placement (PLAYBOOK step 2/3): "
            "clear that corridor (`badpcb audit %s --only CHANNEL,ESCAPE`, `badpcb plan %s %s`), move the parts sitting in it, or change the pin map." % (
                c["handle"], (" (%s, dem %s/cap %s)" % (c["id"], c["dem"], c["cap"])) if c.get("id") else "",
                " | ".join(",".join(x) for x in c["per_pass"]),
                " (demand %s vs capacity %s)" % (c["dem"], c["cap"]) if c.get("cap") is not None else "", target, target, fan_ref or ""))


LADDER = ["plain", "repair", "strip"]


def strategy(history, cur_names, fan_ref=None, session="S"):
    """history: [{'mode': str, 'names': [net...]}] oldest first (this pass last). -> one concrete advice line."""
    union = sorted(set().union(*[set(h["names"]) for h in history[-3:]]))
    modes = [h.get("mode", "plain") for h in history[-3:]]
    fan = (" --fanout %s" % fan_ref) if fan_ref else ""
    nets = " ".join(union[:8])
    if "strip" in modes:
        return ("the group %s kept flipping even after --strip --fanout: stop routing, change the board: `badpcb suggest %s mcu` / `badpcb plan %s` "
                "(placement), swap the pins of the group, or `unlock` the locked copper that walls it in" % (nets, session, session))
    if "repair" in modes:
        return "next: `badpcb droute %s --strip%s --nets %s` (rip the whole group and re-escape it together)" % (session, fan, nets)
    return "next: `badpcb droute %s --repair --radius 4%s --nets %s` (rip only the copper around the open ends, route the group again)" % (session, fan, nets)


def escape_advice(target, nets, fan_ref=None):
    """Ping-pong on a fine-pitch part: ask the audit what still blocks the fan for these nets. -> list of advice lines (may be empty).
    The pattern that closed the last bundle of the case study: move the intruder(s) the ESCAPE finding names, THEN strip and re-escape the whole bundle together."""
    try:
        from . import audit
        st = audit.load_state(target)
        rep = audit.run(st, ["ESCAPE"], budget=20)
    except Exception as e:          # advice must never break a routing pass
        return ["(audit ESCAPE unavailable: %s)" % e]
    out = []
    nets = set(nets)
    for f in rep.findings:
        h = f.handles
        if f.det != "ESCAPE" or (fan_ref and h.get("part") != fan_ref):
            continue
        hit = sorted(set(h.get("nets") or []) & nets)
        if not hit:
            continue
        part = h.get("part")
        out.append("audit ESCAPE %s.%s: %d signal stub(s) of %s are blocked by %s (fan %d%% used). Move those first: `badpcb plan %s %s --minimal` "
                   "(or `badpcb eval %s 'move ...'` by hand), `badpcb act`, then `badpcb droute %s --strip --fanout %s --nets %s`" % (
                       part, h.get("side"), f.ev.get("signal_blocked", 0), " ".join(hit[:6]), " ".join(h.get("intruders", [])[:6]),
                       f.ev.get("used_pct", 0), target, part, target, target, part, " ".join(sorted(nets)[:8])))
    return out
