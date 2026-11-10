"""Dump a .kicad_pcb to model.json (run with $KIPY; the only module that needs pcbnew).

    "$KIPY" -m badpcb.extract board.kicad_pcb [model.json]     (PYTHONPATH must contain the directory holding the badpcb package)

All coordinates mm, KiCad convention (+Y down). Layers are names ("F.Cu", "In1.Cu", ...).
"""
import json
import sys
import time

import pcbnew as p

TOMM = p.ToMM
COPPER = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def pt(v):
    return [round(TOMM(v.x), 4), round(TOMM(v.y), 4)]


def lname(b, lid):
    return b.GetLayerName(lid)


def cu_layers(b, lset):
    """Copper layers of a layer set, restricted to layers enabled on this board."""
    on = {lname(b, l) for l in b.GetEnabledLayers().CuStack()}
    return [n for n in (lname(b, l) for l in lset.CuStack()) if n in on]


def poly_of(shape_poly):
    """SHAPE_POLY_SET -> list of outlines, each {'outer': [...], 'holes': [[...]]}."""
    out = []
    for i in range(shape_poly.OutlineCount()):
        o = shape_poly.Outline(i)
        outer = [pt(o.CPoint(k)) for k in range(o.PointCount())]
        holes = []
        for h in range(shape_poly.HoleCount(i)):
            hh = shape_poly.Hole(i, h)
            holes.append([pt(hh.CPoint(k)) for k in range(hh.PointCount())])
        out.append({"outer": outer, "holes": holes})
    return out


SHAPES = {p.PAD_SHAPE_CIRCLE: "circle", p.PAD_SHAPE_RECT: "rect", p.PAD_SHAPE_OVAL: "oval",
          p.PAD_SHAPE_ROUNDRECT: "roundrect", p.PAD_SHAPE_TRAPEZOID: "trapezoid",
          p.PAD_SHAPE_CUSTOM: "custom"}


def extract(path):
    b = p.LoadBoard(path)
    ds = b.GetDesignSettings()
    nc = ds.m_NetSettings.GetDefaultNetclass()
    rules = {"clearance": TOMM(nc.GetClearance()), "track_width": TOMM(nc.GetTrackWidth()),
             "via_dia": TOMM(nc.GetViaDiameter()), "via_drill": TOMM(nc.GetViaDrill()),
             "min_track": TOMM(ds.m_TrackMinWidth), "edge_clearance": TOMM(ds.m_CopperEdgeClearance)}
    bb = b.GetBoardEdgesBoundingBox()
    edge = []
    for d in b.GetDrawings():
        if d.GetLayer() == p.Edge_Cuts:
            bx = d.GetBoundingBox()
            edge.append({"kind": d.GetShapeStr(), "bbox": [TOMM(bx.GetLeft()), TOMM(bx.GetTop()),
                                                            TOMM(bx.GetRight()), TOMM(bx.GetBottom())]})
    parts, holes = [], []
    for f in b.GetFootprints():
        side = "B" if f.IsFlipped() else "F"
        pads = []
        for pd in f.Pads():
            sz = pd.GetSize()
            lays = cu_layers(b, pd.GetLayerSet())
            npth = pd.GetAttribute() == p.PAD_ATTRIB_NPTH
            drill = pd.GetDrillSize()
            rec = {"num": pd.GetNumber(), "pos": pt(pd.GetPosition()), "layers": lays,
                   "shape": SHAPES.get(pd.GetShape(), "other"),
                   "size": [round(TOMM(sz.x), 4), round(TOMM(sz.y), 4)],
                   "rot": round(pd.GetOrientationDegrees(), 2),
                   "net": pd.GetNetname(), "drill": round(TOMM(drill.x), 4),
                   "npth": npth, "smd": pd.GetAttribute() == p.PAD_ATTRIB_SMD}
            if npth:
                holes.append({"pos": rec["pos"], "drill": rec["drill"], "ref": f.GetReference()})
            else:
                pads.append(rec)
        cy = f.GetCourtyard(p.B_CrtYd if side == "B" else p.F_CrtYd)
        cyb = cy.BBox() if cy.OutlineCount() else f.GetBoundingBox(False)
        parts.append({"ref": f.GetReference(), "value": f.GetValue(), "fpid": str(f.GetFPID().GetLibItemName()),
                      "pos": pt(f.GetPosition()), "rot": round(f.GetOrientationDegrees(), 2), "side": side,
                      "locked": bool(f.IsLocked()),
                      "bbox": [TOMM(cyb.GetLeft()), TOMM(cyb.GetTop()), TOMM(cyb.GetRight()), TOMM(cyb.GetBottom())],
                      "pads": pads})
    tracks, vias = [], []
    for t in b.GetTracks():
        if t.GetClass() == "PCB_VIA":
            vias.append({"pos": pt(t.GetPosition()), "net": t.GetNetname(), "dia": TOMM(t.GetWidth(0)),
                         "drill": TOMM(t.GetDrillValue()), "locked": bool(t.IsLocked()),
                         "layers": [lname(b, t.TopLayer()), lname(b, t.BottomLayer())], "id": t.m_Uuid.AsString()})
        else:
            tracks.append({"a": pt(t.GetStart()), "b": pt(t.GetEnd()), "w": TOMM(t.GetWidth()),
                           "layer": lname(b, t.GetLayer()), "net": t.GetNetname(), "locked": bool(t.IsLocked()), "id": t.m_Uuid.AsString()})
    zones, keepouts = [], []
    for z in b.Zones():
        rec = {"layers": cu_layers(b, z.GetLayerSet()), "net": z.GetNetname(),
               "priority": z.GetAssignedPriority(), "polys": poly_of(z.Outline())}
        if z.GetIsRuleArea():
            rec.update(no_tracks=z.GetDoNotAllowTracks(), no_vias=z.GetDoNotAllowVias(),
                       no_pads=z.GetDoNotAllowPads(), no_copper=z.GetDoNotAllowZoneFills())
            keepouts.append(rec)
        else:
            zones.append(rec)
    nets = sorted({str(n) for n in b.GetNetsByName().keys()} - {""})
    return {"source": path, "bbox": [TOMM(bb.GetLeft()), TOMM(bb.GetTop()), TOMM(bb.GetRight()), TOMM(bb.GetBottom())],
            "edge": edge, "layers": cu_layers(b, b.GetEnabledLayers()),
            "rules": rules, "nets": nets, "parts": parts, "tracks": tracks, "vias": vias,
            "zones": zones, "keepouts": keepouts, "holes": holes}


def main(argv):
    t0 = time.time()
    m = extract(argv[1])
    out = argv[2] if len(argv) > 2 else argv[1].rsplit(".", 1)[0] + ".model.json"
    with open(out, "w") as fh:
        json.dump(m, fh, separators=(",", ":"))
    print("%s: %d parts, %d tracks, %d vias, %d zones, %d holes -> %s (%.1fs)" % (
        argv[1], len(m["parts"]), len(m["tracks"]), len(m["vias"]), len(m["zones"]), len(m["holes"]), out, time.time() - t0))


if __name__ == "__main__":
    from badpcb import kipy_exit
    try:
        main(sys.argv)
    except BaseException:
        import traceback
        traceback.print_exc()
        kipy_exit.done(1)
    kipy_exit.done(0)
