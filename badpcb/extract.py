"""Dump a .kicad_pcb to model.json (pure stdlib: reads the file directly, no KiCad needed).

    python3 -m badpcb.extract board.kicad_pcb [model.json]

All coordinates mm, KiCad convention (+Y down). Layers are names ("F.Cu", "In1.Cu", ...).
"""
import json
import os
import sys
import time

from .kicad_pcb import Board, norm180


def r4(v):
    return round(v, 4)


def pt(p):
    return [r4(p[0]), r4(p[1])]


def extract(path):
    b = Board.load(path)
    rules = b.rules()
    edge = b.edge_shapes()
    bb = b.edges_bbox()
    parts, holes = [], []
    for f in b.footprints:
        pads = []
        for pd in f.pads:
            pos = pt(pd.world())
            npth = pd.attr == "np_thru_hole"
            lays = b.expand_cu(pd.layer_names)
            rec = {"num": pd.num, "pos": pos, "layers": lays,
                   "shape": pd.shape if pd.shape in ("circle", "rect", "oval", "roundrect", "trapezoid", "custom") else "other",
                   "size": [r4(pd.size[0]), r4(pd.size[1])],
                   "rot": round(pd.abs_angle, 2),
                   "net": pd.net, "drill": r4(pd.drill),
                   "npth": npth, "smd": pd.attr == "smd"}
            if npth:
                holes.append({"pos": pos, "drill": rec["drill"], "ref": f.ref})
            else:
                pads.append(rec)
        parts.append({"ref": f.ref, "value": f.value, "fpid": f.fpid, "pos": pt(f.pos), "rot": round(norm180(f.rot), 2),
                      "side": f.side, "locked": f.locked, "bbox": f.bbox(), "pads": pads})
    tracks, vias = [], []
    for it in b.items:
        if it.kind == "via":
            vias.append({"pos": pt(it.a), "net": it.net, "dia": it.dia, "drill": it.drill, "locked": it.locked,
                         "layers": [it.layers[0], it.layers[-1]], "id": it.uuid})
        else:
            tracks.append({"a": pt(it.a), "b": pt(it.b), "w": it.w, "layer": it.layer, "net": it.net,
                           "locked": it.locked, "id": it.uuid})
    zones, keepouts = [], []
    for z in b.zones:
        rec = {"layers": z.layers, "net": z.net, "priority": z.priority,
               "polys": [{"outer": [pt(p) for p in o["outer"]], "holes": [[pt(p) for p in h] for h in o["holes"]]} for o in z.polys]}
        if z.is_rule_area:
            rec.update(z.keepout())
            keepouts.append(rec)
        else:
            zones.append(rec)
    return {"source": path, "bbox": bb, "edge": edge, "layers": list(b.cu_layers), "rules": rules,
            "nets": b.net_names(), "parts": parts, "tracks": tracks, "vias": vias, "zones": zones,
            "keepouts": keepouts, "holes": holes}


def main(argv):
    t0 = time.time()
    m = extract(argv[1])
    if len(argv) > 2:
        out = argv[2]
    else:                                      # default: the nested work dir, not beside the board
        from . import config
        out = os.path.join(config.current().work_dir("extract", create=True), os.path.basename(argv[1]).rsplit(".", 1)[0] + ".model.json")
    with open(out, "w") as fh:
        json.dump(m, fh, separators=(",", ":"))
    print("%s: %d parts, %d tracks, %d vias, %d zones, %d holes -> %s (%.1fs)" % (
        argv[1], len(m["parts"]), len(m["tracks"]), len(m["vias"]), len(m["zones"]), len(m["holes"]), out, time.time() - t0))


if __name__ == "__main__":
    main(sys.argv)
