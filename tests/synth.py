"""Tiny synthetic boards as model dicts (what extract.py would write), for unit tests."""
from badpcb import model


def pad(num, pos, net, layers=("F.Cu",), size=(0.5, 0.5), drill=0.0):
    return {"num": num, "pos": list(pos), "layers": list(layers), "shape": "rect", "size": list(size), "rot": 0.0,
            "net": net, "drill": drill, "npth": False, "smd": drill == 0}


def part(ref, pads, pos=(0, 0), bbox=None):
    return {"ref": ref, "value": "", "fpid": "T", "pos": list(pos), "rot": 0.0, "side": "F", "locked": False,
            "bbox": bbox or [pos[0] - 1, pos[1] - 1, pos[0] + 1, pos[1] + 1], "pads": pads}


def track(a, b, net, layer="F.Cu", w=0.2, locked=True):
    return {"a": list(a), "b": list(b), "w": w, "layer": layer, "net": net, "locked": locked}


def via(pos, net, layers=("F.Cu", "B.Cu")):
    return {"pos": list(pos), "net": net, "dia": 0.6, "drill": 0.3, "locked": True, "layers": list(layers)}


def board(parts, tracks=(), vias=(), zones=(), size=(100, 50), layers=("F.Cu", "B.Cu")):
    nets = sorted({p["net"] for pt in parts for p in pt["pads"] if p["net"]})
    d = {"source": "synthetic", "bbox": [0, 0, size[0], size[1]], "edge": [], "layers": list(layers),
         "rules": {"clearance": 0.2, "track_width": 0.2, "via_dia": 0.6, "via_drill": 0.3, "min_track": 0.15, "edge_clearance": 0.3},
         "nets": nets, "parts": list(parts), "tracks": list(tracks), "vias": list(vias), "zones": list(zones),
         "keepouts": [], "holes": []}
    return model.from_dict(d)


def zone(net, layer, size=(100, 50)):
    w, h = size
    return {"layers": [layer], "net": net, "priority": 0, "polys": [{"outer": [[0, 0], [w, 0], [w, h], [0, h]], "holes": []}]}


def singles(spec):
    """spec: [(ref, (x, y), net)] -> one-pad parts."""
    return [part(r, [pad("1", p, n)], pos=p) for r, p, n in spec]
