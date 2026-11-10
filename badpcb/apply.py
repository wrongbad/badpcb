"""Session -> .kicad_pcb.   `python3 -m badpcb apply SESSION out.kicad_pcb [--in board] [--no-verify]`

Two stages so that all badpcb logic stays in plain python3 and only the board edit needs pcbnew:
  plan(state)            python3: diff of the session's final board vs. its base -> JSON plan
  write(plan, in, out)   $KIPY (`$KIPY -m badpcb.apply --write plan.json in.kicad_pcb out.kicad_pcb report.json`)
The orchestrator `run()` does both, then verifies: re-extracts the written board and compares badpcb metrics with the
session's METRICS, and diffs kicad-cli DRC (input vs output) so that new clearance/courtyard errors are visible.

Plan contents: part placements (pos, rot, side), MCU pad net changes, plane vias (+stub tracks), deleted vias.
Writer: moves/rotates/flips footprints (checked against badpcb's predicted pad positions, flip is guarded: the footprint
must belong to a live board), re-nets pads, adds copper, then RIPS UP copper made wrong by the changes:
  * touching a changed pad (moved part or re-netted pin) of another net / of no net  -> removed (short)
  * attached (track end / via) to a pad of a moved part and no longer landing on a same-net pad -> removed (dangling)
  * unprotected copper closer than the board clearance to a changed pad of another net   -> removed
  * cascade: unprotected stubs/vias that lost their only attachment through a removal    -> removed
Copper of `protect` lock classes (uuid -> class carried in the plan) is kept unless shorted/broken by a changed pad.
Zones are refilled. Everything removed is listed in the report. swaps_applied.md lists the netlist/firmware changes.
"""
import json
import math
import os
import subprocess
import sys

from . import config, locks


# ============================================================================== plan (python3, no pcbnew)
def _k(*v):
    return tuple(round(x, 3) if isinstance(x, float) else x for x in v)


def plan(st):
    """Diff st.world.board (final) against st.base (original)."""
    base, fin = st.base, st.world.board
    P = {"session": st.name, "base_board": st.board_path, "parts": [], "pads": [], "vias_add": [], "tracks_add": [],
         "vias_del": [], "tracks_del": [], "hints": st.world.hints}
    for ref, p in fin.parts.items():
        b0 = base.parts[ref]
        if p.side != b0.side or abs(p.pos[0] - b0.pos[0]) > 1e-6 or abs(p.pos[1] - b0.pos[1]) > 1e-6 or abs(((p.rot - b0.rot + 180) % 360) - 180) > 1e-6:
            P["parts"].append({"ref": ref, "pos": list(p.pos), "rot": p.rot, "side": p.side, "old_pos": list(b0.pos),
                               "old_rot": b0.rot, "old_side": b0.side,
                               "pads": [{"num": pd.num, "pos": list(pd.pos)} for pd in p.pads]})
    for ref, p in fin.parts.items():
        for pd, p0 in zip(p.pads, base.parts[ref].pads):
            if pd.net != p0.net:
                P["pads"].append({"ref": ref, "num": pd.num, "old": p0.net, "net": pd.net})
    bv = {_k(*v.pos, v.net): v for v in base.vias}
    fv = {_k(*v.pos, v.net): v for v in fin.vias}
    for k, v in fv.items():
        if k not in bv:
            P["vias_add"].append({"pos": list(v.pos), "net": v.net, "dia": v.dia, "drill": v.drill, "layers": v.layers})
    for k, v in bv.items():
        if k not in fv:
            P["vias_del"].append({"pos": list(v.pos), "net": v.net})
    bt = {_k(*t.a, *t.b, t.layer, t.net) for t in base.tracks}
    ft = {_k(*t.a, *t.b, t.layer, t.net) for t in fin.tracks}
    for t in fin.tracks:
        if _k(*t.a, *t.b, t.layer, t.net) not in bt:
            P["tracks_add"].append({"a": list(t.a), "b": list(t.b), "w": t.w, "layer": t.layer, "net": t.net})
    for t in base.tracks:
        if _k(*t.a, *t.b, t.layer, t.net) not in ft:
            P["tracks_del"].append({"a": list(t.a), "b": list(t.b), "layer": t.layer, "net": t.net})
    P["clearance"] = fin.rules.clearance
    P["zones"] = [{"idx": i, "net": z.net, "layers": z.layers, "polys": z.polys}
                  for i, (z, z0) in enumerate(zip(fin.zones, base.zones)) if z.polys != z0.polys]
    b_lock = {i.id: i.locked for i in list(base.tracks) + list(base.vias) if i.id}
    f_lock = {i.id: i.locked for i in list(fin.tracks) + list(fin.vias) if i.id}
    P["protect"] = locks.protected_ids(base)
    P["unlock"] = sorted(i for i, l in b_lock.items() if l and f_lock.get(i) is False)
    P["lock"] = sorted(i for i, l in b_lock.items() if not l and f_lock.get(i) is True)
    return P


def swaps_md(plan_, cfg):
    """Markdown: what the schematic/netlist and firmware must change to match the board (pin swaps applied to the MCU's pads)."""
    names = cfg["pin_names"]
    ch = {}
    for c in plan_["pads"]:
        if c["ref"] != cfg["mcu"]:
            continue
        if c["net"]:
            ch.setdefault(c["net"], {})["new"] = c["num"]
        if c["old"]:
            ch.setdefault(c["old"], {})["old"] = c["num"]
    gp = lambda n: names.get(n, "?")
    L = ["# swaps_applied: board vs schematic divergence (session %s)" % plan_["session"], "",
         "The routed board differs from the netlist / schematic. Either regenerate the board from an updated netlist, or",
         "update the netlist and firmware as below (badpcb applied these to pad nets of %s)." % (cfg["mcu"] or "the MCU"), "",
         "| net | old pin | old name | new pin | new name |", "|---|---|---|---|---|"]
    moved = sorted(ch, key=lambda n: (n.rstrip("0123456789"), int(n[len(n.rstrip("0123456789")):] or 0)))
    for n in moved:
        o, w = ch[n].get("old"), ch[n].get("new")
        if o == w:
            continue
        L.append("| %s | %s | %s | %s | %s |" % (n, o or "-", gp(o) if o else "-", w or "-", gp(w) if w else "-"))
    newly = sorted({v["new"] for v in ch.values() if "new" in v} - {v["old"] for v in ch.values() if "old" in v}, key=int)
    freed = sorted({v["old"] for v in ch.values() if "old" in v} - {v["new"] for v in ch.values() if "new" in v}, key=int)
    L += ["", "## Netlist", "",
          "- pins now USED (were unconnected / spare): " + (", ".join("%s (pin %s)" % (gp(n), n) for n in newly) or "none"),
          "- pins now FREE (connect to a no-connect): " + (", ".join("%s (pin %s)" % (gp(n), n) for n in freed) or "none"),
          "", "## Firmware", ""]
    for n in moved:
        v = ch[n]
        if v.get("old") != v.get("new"):
            L.append("- net %s: now on %s (was %s)" % (n, gp(v.get("new")) if v.get("new") else "-", gp(v["old"]) if v.get("old") else "-"))
    L += ["", "Part moves in the plan (placement only, no netlist change): " + (", ".join(
        p["ref"] for p in plan_["parts"]) or "none"), ""]
    return "\n".join(L)


# ============================================================================== geometry (pure; used by the writer)
def _seg_pt(p, a, b):
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _seg_seg(a, b, c, d):
    def cr(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    if (cr(a, b, c) * cr(a, b, d) < 0) and (cr(c, d, a) * cr(c, d, b) < 0):
        return 0.0
    return min(_seg_pt(a, c, d), _seg_pt(b, c, d), _seg_pt(c, a, b), _seg_pt(d, a, b))


def pad_dist(pad, a, b):
    """Distance (mm, 0 = overlapping) from segment a-b to the pad body (rect / round approximations)."""
    cx, cy = pad["pos"]
    w, h = pad["size"]
    r = math.radians(pad["rot"])
    c, s = math.cos(r), math.sin(r)

    def loc(p):
        dx, dy = p[0] - cx, p[1] - cy
        return (dx * c + dy * s, -dx * s + dy * c)
    la, lb = loc(a), loc(b)
    if pad["shape"] in ("circle", "oval", "roundrect"):
        # capsule along the long axis (exact for circle/oval, conservative-ish for roundrect)
        rad = min(w, h) / 2.0
        ext = (max(w, h) - min(w, h)) / 2.0
        p0, p1 = ((-ext, 0), (ext, 0)) if w >= h else ((0, -ext), (0, ext))
        return max(0.0, _seg_seg(la, lb, p0, p1) - rad)
    hx, hy = w / 2.0, h / 2.0
    cor = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    best = min(_seg_seg(la, lb, cor[i], cor[(i + 1) % 4]) for i in range(4))
    inside = lambda p: abs(p[0]) <= hx and abs(p[1]) <= hy
    return 0.0 if (inside(la) or inside(lb)) else best


def _pt_in_pad(pad, p, tol=0.02):
    return pad_dist(pad, p, p) <= tol


# ============================================================================== writer ($KIPY)
def write(plan_, src, dst):
    import pcbnew as p
    MM, TOMM = p.FromMM, p.ToMM
    board = p.LoadBoard(src)            # keep `board` alive for the whole run (footprints hold a raw pointer to it)
    zones0 = [z for z in board.Zones() if not z.GetIsRuleArea()] if plan_.get("zones") else []   # (typed wrappers are gone after the edits below)
    rep = {"moved": [], "renet": [], "vias_added": 0, "tracks_added": 0, "removed": [], "kept_protected_warnings": [],
           "placement_mismatch": [], "flip_skipped": []}
    # zone outlines first: pcbnew's typed wrappers (Zones(), Cast_to_ZONE) stop working after footprints/tracks are edited
    if plan_.get("zones"):
        zs = zones0                                                          # same order as badpcb.extract
        rep["zones_reshaped"] = []
        for zc in plan_["zones"]:
            z = zs[zc["idx"]]
            if z.GetNetname() != zc["net"]:
                rep["zones_reshaped"].append("SKIPPED zone %d: net %s != %s" % (zc["idx"], z.GetNetname(), zc["net"]))
                continue
            def outline(polys):
                ps = p.SHAPE_POLY_SET()
                for poly in polys:
                    o = ps.NewOutline()
                    for x, y in poly["outer"]:
                        ps.Append(MM(x), MM(y), o)
                    for h in poly["holes"]:
                        hi = ps.NewHole(o)
                        for x, y in h:
                            ps.Append(MM(x), MM(y), o, hi)
                ps.thisown = False          # ZONE::SetOutline takes ownership of the pointer (python freeing it too = double free)
                return ps
            sibs = [p.Cast_to_ZONE(z.Duplicate(False)) for _ in zc["polys"][1:]]     # a saved zone holds ONE polygon: further pieces become sibling zones
            z.SetOutline(outline(zc["polys"][:1]))
            for nz, extra in zip(sibs, zc["polys"][1:]):
                nz.SetOutline(outline([extra]))
                board.Add(nz)
            rep["zones_reshaped"].append("%s %s: %d polygon(s)" % (zc["net"], "/".join(zc["layers"]), len(zc["polys"])))
    prot = dict(plan_.get("protect", {}))                  # uuid -> lock class name (protect-enabled classes)
    lname = lambda lid: board.GetLayerName(lid)
    lid = board.GetLayerID

    def pad_info(pd):
        sz = pd.GetSize()
        shp = {p.PAD_SHAPE_CIRCLE: "circle", p.PAD_SHAPE_OVAL: "oval", p.PAD_SHAPE_ROUNDRECT: "roundrect"}.get(pd.GetShape(), "rect")
        lays = {lname(l) for l in pd.GetLayerSet().CuStack()}
        pos = pd.GetPosition()
        return {"id": "%s.%s" % (pd.GetParentFootprint().GetReference(), pd.GetNumber()), "ref": pd.GetParentFootprint().GetReference(),
                "pos": (TOMM(pos.x), TOMM(pos.y)), "size": (TOMM(sz.x), TOMM(sz.y)), "rot": pd.GetOrientationDegrees(), "shape": shp,
                "layers": lays, "net": pd.GetNetname()}

    def all_pads():
        return [pad_info(pd) for f in board.GetFootprints() for pd in f.Pads()]

    def item_info(t):
        if t.GetClass() == "PCB_VIA":
            pos = t.GetPosition()
            return {"obj": t, "kind": "via", "a": (TOMM(pos.x), TOMM(pos.y)), "b": (TOMM(pos.x), TOMM(pos.y)),
                    "w": TOMM(t.GetWidth(0)), "layers": {lname(l) for l in t.GetLayerSet().CuStack()}, "net": t.GetNetname(),
                    "uuid": t.m_Uuid.AsString(), "locked": bool(t.IsLocked())}
        a, b = t.GetStart(), t.GetEnd()
        return {"obj": t, "kind": "track", "a": (TOMM(a.x), TOMM(a.y)), "b": (TOMM(b.x), TOMM(b.y)), "w": TOMM(t.GetWidth()),
                "layers": {lname(t.GetLayer())}, "net": t.GetNetname(), "uuid": t.m_Uuid.AsString(), "locked": bool(t.IsLocked())}

    def touch(it, pads):
        """pad ids that a track end / via centre lies inside (same layer)."""
        out = {}
        for end in ("a", "b"):
            pt = it[end]
            for pd in pads:
                if (pd["layers"] & it["layers"]) and _pt_in_pad(pd, pt):
                    out.setdefault(end, set()).add(pd["id"])
        return out

    unlock, lock = set(plan_.get("unlock", [])), set(plan_.get("lock", []))
    for u in unlock:                      # unlocked copper is ordinary copper for the rip-up analysis below
        prot.pop(u, None)
    for t in board.GetTracks():
        u = t.m_Uuid.AsString()
        if u in unlock:
            t.SetLocked(False)
        elif u in lock:
            t.SetLocked(True)
    rep["unlocked"], rep["locked"] = len(unlock), len(lock)
    pads0 = all_pads()
    items0 = [item_info(t) for t in board.GetTracks()]
    for it in items0:
        it["prot"] = prot.get(it["uuid"], "")
        it["t0"] = touch(it, pads0)

    # ---- parts
    changed_pads = set()
    for pp in plan_["parts"]:
        f = board.FindFootprintByReference(pp["ref"])
        if f is None:
            rep["placement_mismatch"].append("%s: not on board" % pp["ref"])
            continue
        if (pp["side"] == "B") != f.IsFlipped():
            if f.GetBoard() is None:
                rep["flip_skipped"].append(pp["ref"])
                continue
            f.Flip(f.GetPosition(), p.FLIP_DIRECTION_LEFT_RIGHT)
        want = {pd["num"]: pd["pos"] for pd in pp["pads"]}
        best = None
        for rot in [pp["rot"], -pp["rot"], pp["rot"] + 180, 180 - pp["rot"]]:
            f.SetPosition(p.VECTOR2I(MM(pp["pos"][0]), MM(pp["pos"][1])))
            f.SetOrientationDegrees(rot)
            err = max(math.hypot(TOMM(pd.GetPosition().x) - want[pd.GetNumber()][0], TOMM(pd.GetPosition().y) - want[pd.GetNumber()][1])
                      for pd in f.Pads() if pd.GetNumber() in want) if want else 0.0
            if best is None or err < best[0]:
                best = (err, rot)
            if err < 0.02:
                break
        if best[0] >= 0.02:
            f.SetOrientationDegrees(best[1])
            rep["placement_mismatch"].append("%s: pad error %.3f mm with rot %g" % (pp["ref"], best[0], best[1]))
        rep["moved"].append("%s -> (%.2f, %.2f) rot %g side %s" % (pp["ref"], pp["pos"][0], pp["pos"][1], pp["rot"], pp["side"]))
        for pd in f.Pads():
            changed_pads.add("%s.%s" % (pp["ref"], pd.GetNumber()))
    # ---- pad nets
    for c in plan_["pads"]:
        f = board.FindFootprintByReference(c["ref"])
        for pd in f.Pads():
            if pd.GetNumber() == c["num"]:
                pd.SetNet(board.FindNet(c["net"]) if c["net"] else board.FindNet(0))
        changed_pads.add("%s.%s" % (c["ref"], c["num"]))
        rep["renet"].append("%s.%s %s -> %s" % (c["ref"], c["num"], c["old"] or "-", c["net"] or "-"))
    pads1 = all_pads()
    pad1 = {pd["id"]: pd for pd in pads1}
    clr = plan_.get("clearance", 0.2)

    # ---- rip-up analysis on the original copper
    removed = {}                 # uuid -> reason

    def tag(it):
        return "%s %s %s (%.2f,%.2f)-(%.2f,%.2f)%s" % (it["kind"], it["net"] or "-", "/".join(sorted(it["layers"])), *it["a"], *it["b"],
                                                      (" [%s]" % it["prot"] if it["prot"] else "") + (" [LOCKED]" if it.get("locked") else ""))
    for it in items0:
        reason = None
        t1 = touch(it, pads1)
        # 1. attached before to a changed pad: still on a same-net pad?
        for end, ids in it["t0"].items():
            if ids & changed_pads:
                ok = any(pad1[i]["net"] == it["net"] for i in t1.get(end, ()))
                if not ok and not any(pad1[i]["net"] == it["net"] for i in ids if i not in changed_pads):
                    reason = "was attached to %s, now %s" % ("/".join(sorted(ids & changed_pads)), "no same-net pad there")
        # 2. geometry vs changed pads of a different net
        if not reason:
            for pid in changed_pads:
                pd = pad1.get(pid)
                if not pd or not (pd["layers"] & it["layers"]) or pd["net"] == it["net"]:
                    continue
                d = pad_dist(pd, it["a"], it["b"]) - it["w"] / 2.0
                if it["kind"] == "via":
                    d = pad_dist(pd, it["a"], it["a"]) - it["w"] / 2.0
                if d <= 0:
                    reason = "shorts pad %s (net %s)" % (pid, pd["net"] or "-")
                    break
                if d < clr and not it["prot"]:
                    reason = "clearance %.2f < %.2f to pad %s (net %s)" % (d, clr, pid, pd["net"] or "-")
                    break
                if d < clr and it["prot"]:
                    rep["kept_protected_warnings"].append("%s clearance %.2f to pad %s" % (tag(it), d, pid))
        if reason:
            removed[it["uuid"]] = (it, reason)

    # 3. cascade: unprotected items that lost their only attachment through a removal
    def near(p, q, tol=0.01):
        return abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol
    changed = True
    while changed:
        changed = False
        rem_pts = [(e, it["layers"]) for it, _ in removed.values() for e in ((it["a"], it["b"]) if it["kind"] == "track" else (it["a"],))]
        rem_via = [it["a"] for it, _ in removed.values() if it["kind"] == "via"]
        for it in items0:
            if it["uuid"] in removed or it["prot"]:
                continue
            ends = [it["a"], it["b"]] if it["kind"] == "track" else [it["a"]]
            for e in ends:
                hit = any(near(e, q) and (it["layers"] & ly or it["kind"] == "via") for q, ly in rem_pts) or any(near(e, v) for v in rem_via)
                if not hit:
                    continue
                # anything left that holds this endpoint?
                held = any(pd["layers"] & it["layers"] and _pt_in_pad(pd, e) and pd["net"] == it["net"] for pd in pads1)
                if not held:
                    for o in items0:
                        if o is it or o["uuid"] in removed or o["net"] != it["net"]:
                            continue
                        oe = [o["a"], o["b"]] if o["kind"] == "track" else [o["a"]]
                        if any(near(e, q) for q in oe) and (o["kind"] == "via" or it["kind"] == "via" or o["layers"] & it["layers"]):
                            held = True
                            break
                if not held:
                    removed[it["uuid"]] = (it, "orphaned by removal of neighbouring copper")
                    changed = True
                    break
    for u, (it, reason) in removed.items():
        board.Remove(it["obj"])
        it["obj"] = None
        rep["removed"].append({"what": tag(it), "net": it["net"], "reason": reason, "protected": it["prot"]})

    # ---- new copper
    # explicit deletions: match against the items listed at the start (pcbnew's typed wrappers stop iterating after the first board.Remove)
    for v in plan_["vias_del"]:
        for it in items0:
            if it["obj"] is not None and it["kind"] == "via" and it["net"] == v["net"] and abs(it["a"][0] - v["pos"][0]) < 1e-3 and abs(it["a"][1] - v["pos"][1]) < 1e-3:
                board.Remove(it["obj"])
                it["obj"] = None
    for t in plan_["tracks_del"]:
        for it in items0:
            if it["obj"] is not None and it["kind"] == "track" and it["net"] == t["net"] and abs(it["a"][0] - t["a"][0]) < 1e-3 and abs(it["a"][1] - t["a"][1]) < 1e-3 \
                    and abs(it["b"][0] - t["b"][0]) < 1e-3 and abs(it["b"][1] - t["b"][1]) < 1e-3:
                board.Remove(it["obj"])
                it["obj"] = None
    for v in plan_["vias_add"]:
        via = p.PCB_VIA(board)
        via.SetPosition(p.VECTOR2I(MM(v["pos"][0]), MM(v["pos"][1])))
        via.SetViaType(p.VIATYPE_THROUGH)
        via.SetWidth(MM(v["dia"]))
        via.SetDrill(MM(v["drill"]))
        via.SetLayerPair(lid(v["layers"][0]), lid(v["layers"][-1]))
        via.SetNet(board.FindNet(v["net"]))
        board.Add(via)
        rep["vias_added"] += 1
    for t in plan_["tracks_add"]:
        tr = p.PCB_TRACK(board)
        tr.SetStart(p.VECTOR2I(MM(t["a"][0]), MM(t["a"][1])))
        tr.SetEnd(p.VECTOR2I(MM(t["b"][0]), MM(t["b"][1])))
        tr.SetWidth(MM(t["w"]))
        tr.SetLayer(lid(t["layer"]))
        tr.SetNet(board.FindNet(t["net"]))
        board.Add(tr)
        rep["tracks_added"] += 1
    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    p.SaveBoard(dst, board)
    return rep


# ============================================================================== orchestration (python3)
def default_rules_pro(board_path=None):
    """The .kicad_pro that grades a board: badpcb.json rules.project, else the board's own sibling .kicad_pro."""
    return config.current().rules_pro_for(board_path)


def copy_rules(board_path, pro=None):
    """Put the project rules (.kicad_pro + .kicad_dru) beside a board (KiCad 10 keeps rules there, not in the .kicad_pcb)."""
    import shutil
    pro = pro or default_rules_pro(board_path)
    base = board_path.rsplit(".", 1)[0]
    if not pro or not os.path.exists(pro) or os.path.abspath(pro) == os.path.abspath(base + ".kicad_pro"):
        return
    shutil.copyfile(pro, base + ".kicad_pro")
    dru = pro.rsplit(".", 1)[0] + ".kicad_dru"
    if os.path.exists(dru):
        shutil.copyfile(dru, base + ".kicad_dru")
    elif os.path.exists(base + ".kicad_dru"):
        os.remove(base + ".kicad_dru")


def drc(board, out_json, kicad_cli=None, pro=None):
    """kicad-cli DRC against the ORIGINAL project rules: KRT rewrites the .kicad_pro it writes beside its output
    (clearance/drill floors lowered to what it routed at, so its own copper 'reads clean'); we grade on a copy of the
    board placed next to the original project file instead."""
    import shutil
    import tempfile
    cli = kicad_cli or config.kicad_cli()
    if pro is None:
        pro = default_rules_pro(board)
    if pro and os.path.exists(pro):
        td = tempfile.mkdtemp(prefix="rkdrc")
        nb = os.path.join(td, "b.kicad_pcb")
        shutil.copyfile(board, nb)
        shutil.copyfile(pro, os.path.join(td, "b.kicad_pro"))
        dru = pro.rsplit(".", 1)[0] + ".kicad_dru"
        if os.path.exists(dru):
            shutil.copyfile(dru, os.path.join(td, "b.kicad_dru"))
        board = nb
    r = subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "--refill-zones", "-o", out_json, board],
                       capture_output=True, text=True)
    if not os.path.exists(out_json):
        r = subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "--units", "mm", "-o", out_json, board],
                           capture_output=True, text=True)
    return json.load(open(out_json))


def drc_summary(d):
    from collections import Counter
    err = Counter(v["type"] for v in d["violations"] if v["severity"] == "error")
    return {"errors": sum(err.values()), "by_type": dict(err), "unconnected": len(d.get("unconnected_items", [])),
            "warnings": sum(1 for v in d["violations"] if v["severity"] != "error")}


def drc_new(d0, d1, types=("clearance", "courtyards_overlap", "shorting_items", "hole_clearance", "copper_edge_clearance")):
    """Violations present in d1 and not in d0 (matched by type + item descriptions)."""
    key = lambda v: (v["type"], tuple(sorted(i["description"] for i in v["items"])))
    s0 = {key(v) for v in d0["violations"]}
    return [v for v in d1["violations"] if v["type"] in types and key(v) not in s0]


def run_lock_delta(session, src):
    """Apply only the copper lock changes / trims of a session (unlock, lock, trim) to an already routed board `src`
    in place (uuids identify the copper), so passes already routed are not thrown away. Returns the writer report."""
    from . import state
    st = state.State.load(session)
    P = plan(st)
    for k in ("parts", "pads", "vias_add", "tracks_add"):
        P[k] = []
    P["vias_del"] = []
    pj = src.rsplit(".", 1)[0] + ".lockplan.json"
    rj = src.rsplit(".", 1)[0] + ".lockplan_report.json"
    json.dump(P, open(pj, "w"), indent=1)
    tmp = src.rsplit(".", 1)[0] + ".tmp_lock.kicad_pcb"
    r = config.kipy_run(["-m", "badpcb.apply", "--write", pj, src, tmp, rj], cwd=config.current().root)
    if r.returncode or not os.path.exists(rj):
        sys.exit("apply writer failed:\n" + (r.stdout + r.stderr)[-3000:])
    os.replace(tmp, src)
    return json.load(open(rj)), len(P["unlock"]), len(P["lock"]), len(P["tracks_del"])


def run(session, out, src=None, verify=True, report_path=None):
    from . import state, cli
    st = state.State.load(session)
    P = plan(st)
    cfg = st.world.cfg
    src = os.path.abspath(src or st.board_path)
    out = os.path.abspath(out)
    pj = out.rsplit(".", 1)[0] + ".plan.json"
    rj = out.rsplit(".", 1)[0] + ".apply_report.json"
    json.dump(P, open(pj, "w"), indent=1)
    r = config.kipy_run(["-m", "badpcb.apply", "--write", pj, src, out, rj], cwd=config.current().root)
    if r.returncode or not os.path.exists(rj):   # writer exits via os._exit(0), so a nonzero code is a real failure
        sys.exit("apply writer failed:\n" + (r.stdout + r.stderr)[-3000:])
    rep = json.load(open(rj))
    md = os.path.join(os.path.dirname(out), "swaps_applied.md")
    open(md, "w").write(swaps_md(P, cfg))
    L = ["applied session %s -> %s" % (session, out)]
    if rep.get("unlocked") or rep.get("locked"):
        L.append("  copper lock changes: %d unlocked, %d locked" % (rep["unlocked"], rep["locked"]))
    L += [
         "  %d parts placed, %d pad nets changed, +%d vias, +%d tracks (stubs); ripped up %d items" % (
             len(rep["moved"]), len(rep["renet"]), rep["vias_added"], rep["tracks_added"], len(rep["removed"]))]
    for z in rep.get("zones_reshaped", []):
        L.append("  zone reshaped: " + z)
    for w in rep["placement_mismatch"] + ["flip skipped: " + x for x in rep["flip_skipped"]]:
        L.append("  WARN placement: " + w)
    for x in rep["removed"]:
        L.append("  ripped: %s -- %s" % (x["what"], x["reason"]))
    for x in rep["kept_protected_warnings"]:
        L.append("  kept protected copper near changed pad: " + x)
    L.append("  swaps_applied: " + md)
    res = {"report": rep}
    if verify:
        m_s = st.metrics()
        b2 = cli.load_board(out)
        st2 = state.State("verify", out, b2, st.assume, st.cell)
        m_o = st2.metrics()
        res["metrics_session"], res["metrics_written"] = m_s, m_o
        L.append("  VERIFY session  METRICS " + state.fmt_metrics(m_s))
        L.append("  VERIFY written  METRICS " + state.fmt_metrics(m_o))
        d0 = drc(src, out.rsplit(".", 1)[0] + ".drc_in.json")
        d1 = drc(out, out.rsplit(".", 1)[0] + ".drc.json")
        new = drc_new(d0, d1)
        res["drc_in"], res["drc_out"], res["drc_new"] = drc_summary(d0), drc_summary(d1), len(new)
        L.append("  DRC input  %s" % json.dumps(res["drc_in"]))
        L.append("  DRC output %s" % json.dumps(res["drc_out"]))
        L.append("  new clearance/courtyard/short errors vs input: %d" % len(new))
        for v in new[:12]:
            L.append("    %s: %s" % (v["type"], " | ".join(i["description"] for i in v["items"])))
    json.dump(res, open(rj, "w"), indent=1, default=list)
    print("\n".join(L))
    return res


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--write":
        from badpcb import kipy_exit
        pj, src, dst, rj = argv[1:5]
        try:
            rep = write(json.load(open(pj)), src, dst)
            json.dump(rep, open(rj, "w"), indent=1)
            print("wrote", dst)
        except BaseException:
            import traceback
            traceback.print_exc()
            kipy_exit.done(1)
        kipy_exit.done(0)
    import argparse
    ap = argparse.ArgumentParser(prog="badpcb apply")
    ap.add_argument("session")
    ap.add_argument("out")
    ap.add_argument("--in", dest="src", default=None, help="input board (default: the session's base board)")
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args(argv)
    run(a.session, a.out, a.src, not a.no_verify)


if __name__ == "__main__":
    main()
