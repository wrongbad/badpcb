"""groute as a command (`rk groute`) and as the fast routability score behind audit/suggest ranking.

    rk groute <session|board.kicad_pcb|model.json> [--cell 1.0] [--iters 30] [--time 30] [--nets EN*,SENSE*] [--region R8] [--hints h.json] [--json out]
        a session is routed in its CURRENT state (placement after all `act`s) with the session's topology hints
        (corridor / layer / priority / freeze; `side`, `reserve`, `near` are not understood by groute and are listed as ignored).
    rk audit S --gscore [SECONDS]  /  rk suggest S audit --gscore [SECONDS]
        top-3 legal fixes of every ERR/WARN finding are re-scored by a short groute run on the finding's region (fix applied
        temporarily): overflow + 5 x no-path + 0.1 x history. Fixes are then ranked by that delta first, evaluate dscore second,
        and each shows `groute R8: score 12.3 -> 4.1 (ovf 9 -> 2)`. ~2 s per run on a 1 mm grid.
A routed board is scored on a copy stripped of its unlocked copper (groute has no incremental import); locked lanes stay.
"""
import dataclasses
import sys
import time

from . import geom, ratsnest


def hints_from_world(world):
    """Session topology hints -> (groute hint list, notes about hints groute ignores)."""
    h = world.hints
    out, notes = [], []
    for n, via in sorted(h.get("corridor", {}).items()):
        out.append({"type": "corridor", "net": n, "via": list(via)})
    for t, lay in sorted(h.get("layer", {}).items()):
        out.append({"type": "layer", "net": t, "layer": lay})
    for n, lvl in sorted(h.get("priority", {}).items()):
        out.append({"type": "priority", "net": n, "level": lvl})
    for r in sorted(h.get("freeze", {})):
        out.append({"type": "freeze", "region": r} if r.startswith("R") else {"type": "freeze", "bbox": [float(v) for v in r.split(",")]})
    for sec in ("side", "reserve", "near"):
        if h.get(sec):
            notes.append("%s hints (%d) are not used by groute" % (sec, len(h[sec])))
    return out, notes


def prepare(board, assume=()):
    """(board for groute, note): a routed board is stripped of its unlocked non-plane copper."""
    plane = set(board.plane_nets()) | set(assume)
    nsig = sum(1 for t in board.tracks if not t.locked and t.net not in plane)
    if nsig <= 200:
        return board, ""
    b = dataclasses.replace(board, tracks=[t for t in board.tracks if t.locked or t.net in plane],
                            vias=[v for v in board.vias if v.locked or v.net in plane])
    return b, "board is routed: groute ran on a copy stripped of its %d unlocked signal tracks" % nsig


def score(board, assume=(), region=None, nets=None, cell=1.0, tlimit=6.0, iters=12, hints=()):
    """Short groute run -> {'score', 'overflow', 'no_path', 'hist', 'vias', 'wl', 'conns', 'region'}; None if the scope is empty/unknown."""
    from . import groute
    b, _note = prepare(board, assume)
    rn = ratsnest.build(b, list(assume))
    try:
        res = groute.run(b, rn, cell=cell, iters=iters, tlimit=tlimit, hints=list(hints), nets=nets, region=region, stall=3)
    except SystemExit:
        if region:
            return score(board, assume, None, nets, cell, tlimit, iters, hints) if nets else None
        return None
    st = res.st
    if not st["connections"]:
        return None
    hist = sum(d["hist"] for _h, d in res.gr.hot_spots(res.ov, res.xv))
    return {"score": round(st["total_overflow"] + 5.0 * st["no_path"] + 0.1 * hist, 2), "overflow": st["total_overflow"], "no_path": st["no_path"],
            "hist": round(hist, 1), "vias": st["vias"], "wl": round(st["wirelength_mm"]), "conns": st["connections"], "region": region or nets}


def fmt(g0, g1):
    return "groute %s: score %.1f -> %.1f (ovf %d -> %d, no-path %d -> %d)" % (g1.get("region") or "board", g0["score"], g1["score"],
                                                                         g0["overflow"], g1["overflow"], g0["no_path"], g1["no_path"])


def rank_fixes(ac, findings, budget_s=60.0, per=3, cell=None):
    """Attach groute before/after to the top `per` legal fixes of each ERR/WARN finding and re-rank them. Returns #runs."""
    from . import actions as A
    from .audit import Trial
    from .state import parse_specs
    import os
    cell = cell or float(os.environ.get("RK_GSCORE_CELL", "1.0"))
    t_end = time.time() + budget_s
    hints, _n = hints_from_world(ac.w)
    cache, runs = {}, 0
    for f in findings:
        if f.sev == "NOTE" or time.time() > t_end:
            continue
        cands = [x for x in f.fixes if x.ok][:per]
        if not cands:
            continue
        reg = f.handles.get("region") if isinstance(f.handles.get("region"), str) else (ac.region(f.where) if f.where else None)
        nets = None if reg else ",".join(f.handles.get("nets") or []) or None
        key = reg or nets
        if key is None:
            continue
        if key not in cache:
            cache[key] = score(ac.b, ac.st.assume, reg, nets, cell, 5.0, hints=hints)
            runs += 1
        g0 = cache[key]
        if g0 is None:
            continue
        for x in cands:
            if time.time() > t_end:
                break
            with Trial(ac.w) as tr:
                if not all(tr.apply(a) for a in parse_specs(x.spec)):
                    continue
                g1 = score(ac.b, ac.st.assume, reg, nets, cell, 5.0, hints=hints)
                runs += 1
            if g1 is not None:
                x.g0, x.g1 = g0, g1
        f.fixes.sort(key=lambda x: (not x.ok, 0 if getattr(x, "g1", None) else 1,
                                    round(x.g1["score"] - x.g0["score"], 1) if getattr(x, "g1", None) else 0.0,
                                    x.res.dscore if x.res else 0.0))
    return runs


def main_args(a):
    from . import groute, state
    import os
    t0 = time.time()
    target = a.target
    notes = []
    hints = []
    if os.path.exists(target) and not os.path.isdir(target):
        board = groute.load_board(target)
        assume = []
    else:
        st = state.State.load(target)
        board, assume = st.world.board, st.assume
        hints, notes = hints_from_world(st.world)
        notes.insert(0, "session %s at step %d: %d hint(s) from the session" % (st.name, st.step(), len(hints)))
    b, note = prepare(board, assume)
    if note:
        notes.append(note)
    hints += groute.load_hints(a.hints)
    rn = ratsnest.build(b, list(assume))
    res = groute.run(b, rn, cell=a.cell, iters=a.iters, tlimit=a.time, hints=hints, nets=a.nets, region=a.region,
                     via_cost=a.via_cost, esc_cap=a.esc_cap, verbose=a.verbose, stall=a.stall)
    for n in notes:
        print("# " + n)
    print(res.text(max_blocked=a.max_blocked, with_map=not a.no_map))
    if a.json_out:
        import json
        with open(a.json_out, "w") as fh:
            json.dump(res.to_json(), fh)
        print("wrote %s" % a.json_out)
    print("(rk groute %.1fs)" % (time.time() - t0), file=sys.stderr)
    return 0
