"""Board model: plain dataclasses loaded from model.json (written by extract.py).

All units mm, +Y down. Net name "" means no net. Handles: parts by refdes, pads as "REF.num",
nets by name. Everything downstream (ratsnest, topo, ...) takes a `Board`.
"""
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Pt = Tuple[float, float]


@dataclass
class Rules:
    clearance: float = 0.2
    track_width: float = 0.2
    via_dia: float = 0.6
    via_drill: float = 0.3
    min_track: float = 0.15
    edge_clearance: float = 0.3

    @property
    def pitch(self):
        """Centre-to-centre spacing of parallel default-width tracks."""
        return self.track_width + self.clearance


@dataclass
class Pad:
    ref: str
    num: str
    pos: Pt
    layers: List[str]          # copper layers the pad is on (all of them for through pads)
    shape: str
    size: Pt
    rot: float
    net: str
    drill: float = 0.0
    smd: bool = True

    @property
    def id(self):
        return "%s.%s" % (self.ref, self.num)

    @property
    def through(self):
        return self.drill > 0


@dataclass
class Part:
    ref: str
    value: str
    fpid: str
    pos: Pt
    rot: float
    side: str                  # "F" / "B"
    locked: bool
    bbox: Tuple[float, float, float, float]    # courtyard
    pads: List[Pad] = field(default_factory=list)

    @property
    def area(self):
        return (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1])


@dataclass
class Track:
    a: Pt
    b: Pt
    w: float
    layer: str
    net: str
    locked: bool
    id: str = ""
    cls: str = ""


@dataclass
class Via:
    pos: Pt
    net: str
    dia: float
    drill: float
    locked: bool
    layers: List[str]          # [top, bottom]
    id: str = ""
    cls: str = ""


@dataclass
class Zone:
    layers: List[str]
    net: str
    priority: int
    polys: List[dict]          # [{'outer': [[x,y],...], 'holes': [...]}]
    keepout: Optional[dict] = None   # flags for rule areas: no_tracks/no_vias/no_pads/no_copper


@dataclass
class Hole:
    pos: Pt
    drill: float
    ref: str


@dataclass
class Board:
    bbox: Tuple[float, float, float, float]
    layers: List[str]
    rules: Rules
    nets: List[str]
    parts: Dict[str, Part]
    tracks: List[Track]
    vias: List[Via]
    zones: List[Zone]
    keepouts: List[Zone]
    holes: List[Hole]
    source: str = ""

    # --- queries -----------------------------------------------------------------
    def pads(self):
        for p in self.parts.values():
            yield from p.pads

    def pads_of_net(self, net):
        return [p for p in self.pads() if p.net == net]

    def pad(self, ref, num):
        """All pad objects of REF.num (a pad number can have several copper pieces, e.g. via pads)."""
        return [p for p in self.parts[ref].pads if p.num == str(num)]

    def outline_area(self):
        """Board outline area in mm2 (bounding box of the Edge.Cuts: the extract keeps only boxes of the outline shapes)."""
        return max(1e-6, (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1]))

    @staticmethod
    def _zone_area(z):
        from . import geom
        a = 0.0
        for zp in z.polys:
            a += geom.poly_area(zp["outer"]) - sum(geom.poly_area(h) for h in zp.get("holes", []))
        return max(a, 0.0)

    def plane_layer_info(self, min_frac=None):
        """{layer: {"area": mm2 of netted zone outlines, "frac": area / outline area, "zones": [(net, mm2)], "plane": bool}} for every layer
        that carries a netted zone. Outlines, not fills: an unfilled zone counts. A layer is a plane layer when those zones cover more than
        `min_frac` of the board outline (default: badpcb.json planes.min_coverage, 0.30), so a few-mm2 outer pour (regulator LX / +1V1 ring)
        does not turn the layer into a plane (droute would then route single-layer)."""
        if min_frac is None:
            from . import config
            min_frac = config.current().plane_min_coverage()
        key = (min_frac, tuple((id(z), z.net, tuple(z.layers), tuple((id(zp), id(zp["outer"]), len(zp["outer"]), len(zp.get("holes", ()))) for zp in z.polys))
                               for z in self.zones))
        hit = self.__dict__.get("_pl_cache")
        if hit and hit[0] == key:
            return hit[1]
        tot = self.outline_area()
        info = {}
        for z in self.zones:
            if not z.net:
                continue
            a = self._zone_area(z)
            for l in z.layers:
                d = info.setdefault(l, {"area": 0.0, "zones": []})
                d["area"] += a
                d["zones"].append((z.net, a))
        for l, d in info.items():
            d["frac"] = d["area"] / tot
            d["plane"] = d["frac"] > min_frac
        self.__dict__["_pl_cache"] = (key, info)
        return info

    def plane_layer_report(self, min_frac=None):
        """Text lines saying which netted zones made each layer a plane layer (or not)."""
        info = self.plane_layer_info(min_frac)
        out = []
        for l in sorted(info, key=self.layers.index):
            d = info[l]
            byn = {}
            for n, a in d["zones"]:
                byn[n] = byn.get(n, 0.0) + a
            zs = " ".join("%s %.0fmm2" % (n, a) for n, a in sorted(byn.items(), key=lambda kv: -kv[1]))
            out.append("%s: %s (%.1f%% of the board, %s)" % (l, "PLANE layer" if d["plane"] else "not a plane layer (small pour, routed as signal)",
                                                          100 * d["frac"], zs))
        return out

    def plane_zones(self):
        """Netted zones on plane layers (see plane_layer_info): the copper that serves plane nets."""
        pl = {l for l, d in self.plane_layer_info().items() if d["plane"]}
        return [z for z in self.zones if z.net and set(z.layers) & pl]

    def plane_layers(self):
        return sorted((l for l, d in self.plane_layer_info().items() if d["plane"]), key=self.layers.index)

    def plane_nets(self):
        return sorted({z.net for z in self.plane_zones()})

    def signal_layers(self):
        pl = set(self.plane_layers())
        return [l for l in self.layers if l not in pl]

    @property
    def width(self):
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self):
        return self.bbox[3] - self.bbox[1]


def from_dict(d) -> Board:
    parts = {}
    for p in d["parts"]:
        pads = [Pad(ref=p["ref"], num=q["num"], pos=tuple(q["pos"]), layers=q["layers"], shape=q["shape"],
                    size=tuple(q["size"]), rot=q["rot"], net=q["net"], drill=q["drill"], smd=q["smd"])
                for q in p["pads"]]
        parts[p["ref"]] = Part(p["ref"], p["value"], p["fpid"], tuple(p["pos"]), p["rot"], p["side"],
                               p["locked"], tuple(p["bbox"]), pads)
    b = Board(
        bbox=tuple(d["bbox"]), layers=d["layers"], rules=Rules(**d["rules"]), nets=d["nets"], parts=parts,
        tracks=[Track(tuple(t["a"]), tuple(t["b"]), t["w"], t["layer"], t["net"], t["locked"], t.get("id", "")) for t in d["tracks"]],
        vias=[Via(tuple(v["pos"]), v["net"], v["dia"], v["drill"], v["locked"], v["layers"], v.get("id", "")) for v in d["vias"]],
        zones=[Zone(z["layers"], z["net"], z["priority"], z["polys"]) for z in d["zones"]],
        keepouts=[Zone(z["layers"], z["net"], z["priority"], z["polys"],
                       {k: z[k] for k in ("no_tracks", "no_vias", "no_pads", "no_copper")}) for z in d["keepouts"]],
        holes=[Hole(tuple(h["pos"]), h["drill"], h["ref"]) for h in d["holes"]],
        source=d.get("source", ""))
    from . import locks
    return locks.classify(b)


def load(path) -> Board:
    with open(path) as fh:
        return from_dict(json.load(fh))
