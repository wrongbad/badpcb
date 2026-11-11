"""Read and edit a .kicad_pcb without KiCad (pure stdlib, KiCad 8/9/10 file formats).

`Board.load(path)` wraps a lossless `sexpr.Doc`: queries read the original text, edits (`Footprint.place`,
`Board.set_pad_net`, `Item.delete`, `Board.add_via`, ...) are recorded as small text patches and `Board.save` writes
the original text with only those patches applied. Nodes this module does not understand are never touched.

Geometry conventions (checked against pcbnew):
  * footprint `(at x y rot)`; a pad's / text's `(at x y a)` x,y are footprint-local (not rotated), the angle `a` is ABSOLUTE
    (includes the footprint rotation). World = footprint at + rotate(local, rot) with KiCad's y-down rotation.
  * a B.Cu footprint is stored mirrored: flipping (pcbnew Flip about the footprint origin, then SetOrientation) leaves the
    footprint at the given angle, negates the local y of every child, negates pad angles relative to the footprint, swaps
    F./B. layer names and toggles `(justify mirror)` (verified against pcbnew 10 for 0/90/180/270 degrees).
  * nets: KiCad 10 names them in place (`(net "GND")`, nothing to declare); KiCad 8/9 declare `(net N "GND")` at top level
    and items refer by number (`(net N)`, pads `(net N "GND")`, zones `(net N) (net_name "GND")`). Both are supported.
"""
import json
import math
import os
import uuid as _uuid

from .sexpr import Atom, Doc, Node, Q, dumps, num

# ---------------------------------------------------------------------------------------------------------- geometry


def norm360(a):
    a = math.fmod(a, 360.0)
    if a < 0:
        a += 360.0
    return 0.0 if abs(a - 360.0) < 1e-9 else a


def norm180(a):
    a = norm360(a)
    return a - 360.0 if a > 180.0 + 1e-9 else a


def rotate(x, y, deg):
    """KiCad RotatePoint (y down): positive angles turn counter-clockwise on screen."""
    d = norm360(deg)
    if d == 0.0:
        return x, y
    if d == 90.0:
        return y, -x
    if d == 180.0:
        return -x, -y
    if d == 270.0:
        return -y, x
    r = math.radians(d)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def _flip_layer(name, n_inner):
    if name.startswith("F."):
        return "B." + name[2:]
    if name.startswith("B."):
        return "F." + name[2:]
    if name.startswith("In") and name.endswith(".Cu") and name[2:-3].isdigit() and n_inner:
        return "In%d.Cu" % (n_inner + 1 - int(name[2:-3]))
    return name


def _arc_bbox(p0, p1, p2):
    """bbox of the arc through three points (start, mid, end)."""
    (x1, y1), (x2, y2), (x3, y3) = p0, p1, p2
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    xs, ys = [x1, x3], [y1, y3]
    if abs(d) > 1e-12:
        ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
        uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
        r = math.hypot(x1 - ux, y1 - uy)
        a = [math.atan2(p[1] - uy, p[0] - ux) for p in (p0, p1, p2)]
        two = 2 * math.pi

        def between(t, s, m, e):          # is angle t on the arc that runs s -> m -> e
            ccw = ((m - s) % two) < ((e - s) % two)
            if ccw:
                return ((t - s) % two) <= ((e - s) % two)
            return ((s - t) % two) <= ((s - e) % two)
        for k in range(4):
            t = k * math.pi / 2
            if between(t, a[0], a[1], a[2]):
                xs.append(ux + r * math.cos(t))
                ys.append(uy + r * math.sin(t))
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------------------------------------- helpers

def _f(node, name, default=None):
    n = node.node(name)
    return n.floats() if n is not None else default


def _str(node, name, default=""):
    n = node.node(name)
    if n is None:
        return default
    v = n.vals()
    return v[0] if v else default


def _stroke_w(node):
    s = node.node("stroke")
    if s is not None:
        w = s.node("width")
        if w is not None:
            return w.floats()[0]
    w = node.node("width")
    return w.floats()[0] if w is not None else 0.0


def _xy_list(node):
    pts = node.node("pts")
    out = []
    if pts is not None:
        for c in pts.nodes("xy"):
            f = c.floats()
            out.append((f[0], f[1]))
    return out


def shape_points(node, kind):
    """(points, half_stroke) bounding a drawing shape in its own frame; arcs add their extreme points."""
    w = _stroke_w(node) / 2.0
    if kind in ("line", "rect"):
        s, e = _f(node, "start"), _f(node, "end")
        return [(s[0], s[1]), (e[0], e[1])], w
    if kind == "circle":
        c, e = _f(node, "center"), _f(node, "end")
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)], w
    if kind == "arc":
        s, m, e = _f(node, "start"), _f(node, "mid"), _f(node, "end")
        if m is None:           # legacy (start center) (angle) arcs: use the end points only
            return [(s[0], s[1]), (e[0], e[1])], w
        x0, y0, x1, y1 = _arc_bbox((s[0], s[1]), (m[0], m[1]), (e[0], e[1]))
        return [(x0, y0), (x1, y1)], w
    if kind == "poly":
        return _xy_list(node), w
    if kind == "curve":
        return _xy_list(node), w
    return [], w


_USER_LAYERS = ("Dwgs.User", "Cmts.User", "Eco1.User", "Eco2.User", "Margin")
_SHAPE_KINDS = {"line": "line", "rect": "rect", "circle": "circle", "arc": "arc", "poly": "poly", "curve": "curve"}
_SHAPE_STR = {"line": "Line", "rect": "Rect", "circle": "Circle", "arc": "Arc", "poly": "Polygon", "curve": "Bezier"}


# ---------------------------------------------------------------------------------------------------------- items
class Pad:
    __slots__ = ("fp", "node", "num", "attr", "shape", "lx", "ly", "ang", "size", "_layers", "drill", "_net", "o")

    def __init__(self, fp, node):
        self.fp, self.node = fp, node
        a = node.atoms()
        self.num = a[1].value if len(a) > 1 else ""
        self.attr = a[2].raw if len(a) > 2 else ""
        self.shape = a[3].raw if len(a) > 3 else ""
        at = _f(node, "at", [0.0, 0.0, 0.0])
        self.lx, self.ly = at[0], at[1]
        self.ang = at[2] if len(at) > 2 else 0.0
        self.o = (self.lx, self.ly, self.ang)               # as loaded
        self.size = tuple(_f(node, "size", [0.0, 0.0])[:2])
        self._layers = None
        d = node.node("drill")
        self.drill = 0.0
        if d is not None:
            v = [x for x in d.vals() if x != "oval"]
            try:
                self.drill = float(v[0])
            except (ValueError, IndexError):
                self.drill = 0.0
        self._net = None

    @property
    def layer_names(self):
        n = self.node.node("layers")
        return n.vals() if n is not None else []

    @property
    def net(self):
        if self._net is not None:                 # re-netted by set_pad_net in this session
            return self._net
        n = self.node.node("net")
        if n is None:
            return ""
        return self.fp.board.net_name_of(n)

    def world(self, pos=None, rot=None, flip=False):
        """Absolute pad centre for the footprint at (pos, rot); flip mirrors the local y first (pcbnew Flip)."""
        pos = pos if pos is not None else self.fp.pos
        rot = self.fp.rot if rot is None else rot
        x, y = rotate(self.lx, -self.ly if flip else self.ly, rot)
        return pos[0] + x, pos[1] + y

    @property
    def abs_angle(self):
        return norm360(self.ang)


class Footprint:
    def __init__(self, board, node):
        self.board, self.node = board, node
        k = node.kids
        self.fpid = ""
        for c in k[1:]:
            if isinstance(c, Atom):
                self.fpid = c.value
                break
        self.fpid = self.fpid.split(":", 1)[-1]
        self.layer = _str(node, "layer", "F.Cu")
        self.side = "B" if self.layer.startswith("B.") else "F"
        self.at_node = node.node("at")
        at = self.at_node.floats() if self.at_node is not None else [0.0, 0.0]
        self.pos = (at[0], at[1])
        self.rot = at[2] if len(at) > 2 else 0.0
        self.locked = node.has_flag("locked")
        self.o_pose = (self.pos, self.rot, self.side)       # as loaded: place() always computes from this
        self.o_layer = self.layer
        self.ref = ""
        self.value = ""
        for p in node.nodes("property"):
            v = p.vals()
            if v and v[0] == "Reference":
                self.ref = v[1] if len(v) > 1 else ""
            elif v and v[0] == "Value":
                self.value = v[1] if len(v) > 1 else ""
        if not self.ref:                                          # KiCad <= 7 style fp_text
            for t in node.nodes("fp_text"):
                v = t.vals()
                if len(v) > 1 and v[0] == "reference":
                    self.ref = v[1]
                elif len(v) > 1 and v[0] == "value":
                    self.value = v[1]
        self._pads = None

    @property
    def pads(self):
        if self._pads is None:
            self._pads = [Pad(self, n) for n in self.node.nodes("pad")]
        return self._pads

    def to_world(self, x, y):
        dx, dy = rotate(x, y, self.rot)
        return self.pos[0] + dx, self.pos[1] + dy

    # ---- courtyard / bounding box (world)
    def _graphics(self):
        for c in self.node.kids:
            if isinstance(c, Node) and c.head and c.head.startswith("fp_") and c.head[3:] in _SHAPE_KINDS:
                yield c, _SHAPE_KINDS[c.head[3:]], _str(c, "layer")

    def bbox(self):
        side_cy = "B.CrtYd" if self.side == "B" else "F.CrtYd"
        pts = []
        for n, kind, layer in self._graphics():
            if layer == side_cy:             # pcbnew's courtyard outline comes out wider than the raw shape by (stroke width - 0.005)
                p, hw = shape_points(n, kind)
                grow = 2 * hw - 0.005 if hw > 0.0025 else 0.0
                pts += [(x + sx * grow, y + sy * grow) for x, y in (self.to_world(*q) for q in p)
                        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            return [min(xs), min(ys), max(xs), max(ys)]
        xs, ys = [], []                                           # no courtyard: pads + graphics (pcbnew GetBoundingBox(false))
        for pd in self.pads:
            cx, cy = pd.world()
            w, h = pd.size
            r = math.radians(pd.abs_angle)
            ex = abs(w / 2 * math.cos(r)) + abs(h / 2 * math.sin(r))
            ey = abs(w / 2 * math.sin(r)) + abs(h / 2 * math.cos(r))
            xs += [cx - ex, cx + ex]
            ys += [cy - ey, cy + ey]
        for n, kind, layer in self._graphics():
            if layer in _USER_LAYERS or layer.startswith("User."):
                continue
            p, hw = shape_points(n, kind)
            for q in p:
                x, y = self.to_world(*q)
                xs += [x - hw, x + hw]
                ys += [y - hw, y + hw]
        if not xs:
            return [self.pos[0], self.pos[1], self.pos[0], self.pos[1]]
        return [min(xs), min(ys), max(xs), max(ys)]

    # ---- editing
    def pad_world_map(self, pos, rot, side):
        """{pad number: world centre} if this footprint stood at pos/rot on `side` (no edit)."""
        flip = (side == "B") != (self.side == "B")
        return {pd.num: pd.world(pos, rot, flip) for pd in self.pads}

    def place(self, pos, rot, side=None):
        """Move / rotate / flip (pcbnew: Flip about the origin, then SetPosition + SetOrientation) as text patches.
        Always computed from the pose the file was loaded with, so it can be called again to change its mind."""
        doc = self.board.doc
        tag = ("pose", id(self))
        doc.drop(tag)
        o_pos, o_rot, o_side = self.o_pose
        side = side or o_side
        flip = side != o_side
        new_rot = norm180(rot)
        d = new_rot - o_rot
        n_inner = self.board.n_inner
        doc.tag = tag
        try:
            if flip:
                doc.set_atom(self.node.node("layer").kids[1], Q(_flip_layer(self.o_layer, n_inner)))
            if flip or abs(norm180(d)) > 1e-12:
                for c in self.node.kids:
                    if isinstance(c, Node):
                        self._edit_child(c, flip, d, o_rot, new_rot, n_inner)
            if self.at_node is not None and ((pos[0], pos[1]) != o_pos or abs(norm180(d)) > 1e-12 or flip):
                want = [pos[0], pos[1]] + ([new_rot] if abs(new_rot) > 1e-9 else [])
                doc.replace(self.at_node, "(at %s)" % " ".join(num(v) for v in want))
        finally:
            doc.tag = None
        self.pos, self.rot, self.side = (pos[0], pos[1]), new_rot, side
        self.layer = _flip_layer(self.o_layer, n_inner) if flip else self.o_layer
        for pd in self.pads:
            lx, ly, a = pd.o
            if flip:
                pd.lx, pd.ly = lx, -ly
                pd.ang = norm360(new_rot - (a - o_rot))
            else:
                pd.lx, pd.ly = lx, ly
                pd.ang = norm360(a + d)

    def _edit_child(self, c, flip, d, old_rot, new_rot, n_inner):
        h = c.head
        if h in ("property", "fp_text"):
            self._edit_at(c, flip, d, old_rot, new_rot, text=True)
            if flip:
                self._flip_layer_of(c, n_inner)
                self._toggle_mirror(c)
        elif h == "pad":
            self._edit_at(c, flip, d, old_rot, new_rot, text=False)
            if flip:
                self._flip_layers_of(c, n_inner)
                self._flip_pad_extras(c)
        elif h and h.startswith("fp_") and flip:
            self._flip_shape(c, n_inner)
        elif h == "zone" and flip:
            self._flip_shape(c, n_inner)

    def _edit_at(self, c, flip, d, old_rot, new_rot, text):
        at = c.node("at")
        if at is None:
            return
        v = at.floats()
        x, y = v[0], v[1]
        a0 = v[2] if len(v) > 2 else 0.0
        if flip:
            y = -y
            if text:                         # pcbnew's result depends on the old footprint angle only (fitted for 0/90/180/270)
                q = norm360(old_rot)
                a = new_rot + (180.0 if (q >= 270.0 or q == 0.0) else 0.0)
            else:
                a = new_rot - (a0 - old_rot)
        else:
            a = a0 + d
        a = norm360(a)
        if y == v[1] and abs(a - norm360(a0)) < 1e-12:
            return
        parts = [num(x), num(y)]
        if abs(a) > 1e-12 or (text and len(v) > 2):         # pads omit a zero angle, text keeps it
            parts.append(num(a))
        self.board.doc.replace(at, "(at %s)" % " ".join(parts))

    def _flip_layer_of(self, c, n_inner):
        ln = c.node("layer")
        if ln is not None and len(ln.kids) > 1:
            old = ln.kids[1].value
            new = _flip_layer(old, n_inner)
            if new != old:
                self.board.doc.set_atom(ln.kids[1], Q(new))

    def _flip_layers_of(self, c, n_inner):
        for name in ("layers", "layer"):
            ln = c.node(name)
            if ln is not None:
                for a in ln.kids[1:]:
                    if isinstance(a, Atom):
                        new = _flip_layer(a.value, n_inner)
                        if new != a.value:
                            self.board.doc.set_atom(a, Q(new))

    def _toggle_mirror(self, c):
        eff = c.node("effects")
        doc = self.board.doc
        if eff is None:
            doc.insert_into(c, "(effects (justify mirror))")
            return
        j = eff.node("justify")
        if j is None:
            doc.insert_into(eff, "(justify mirror)")
            return
        words = [a.raw for a in j.atoms()[1:]]
        if "mirror" in words:
            words.remove("mirror")
        else:
            words.append("mirror")
        if words:
            doc.replace(j, "(justify %s)" % " ".join(words))
        else:
            doc.delete(j)

    def _flip_pad_extras(self, c):
        doc = self.board.doc
        dr = c.node("drill")
        if dr is not None:
            off = dr.node("offset")
            if off is not None:
                v = off.floats()
                doc.replace(off, "(offset %s %s)" % (num(v[0]), num(-v[1])))
        rd = c.node("rect_delta")
        if rd is not None:
            v = rd.floats()
            doc.replace(rd, "(rect_delta %s %s)" % (num(v[0]), num(-v[1])))
        ch = c.node("chamfer")
        if ch is not None:
            sw = {"top_left": "bottom_left", "bottom_left": "top_left", "top_right": "bottom_right", "bottom_right": "top_right"}
            for a in ch.atoms()[1:]:
                if a.raw in sw:
                    doc.set_atom(a, sw[a.raw])
        prim = c.node("primitives")
        if prim is not None:
            for g in prim.nodes():
                self._flip_shape(g, 0, layer=False)

    def _flip_shape(self, c, n_inner, layer=True):
        """Mirror y of every coordinate in a graphic node (and swap its layer name)."""
        doc = self.board.doc
        if layer:
            self._flip_layer_of(c, n_inner)
            ls = c.node("layers")
            if ls is not None:
                self._flip_layers_of(c, n_inner)
        kind = (c.head or "")
        arc = kind in ("fp_arc", "gr_arc")

        def mir(node):
            v = node.floats()
            doc.replace(node, "(%s %s)" % (node.head, " ".join([num(v[0]), num(-v[1])] + [num(t) for t in v[2:]])))
        for name in ("start", "end", "mid", "center", "control1", "control2"):
            n = c.node(name)
            if n is not None:
                mir(n)
        if arc and c.node("mid") is not None:                       # a mirrored arc keeps its direction: swap start/end
            s, e = c.node("start"), c.node("end")
            sv, ev = s.floats(), e.floats()
            doc.replace(s, "(start %s %s)" % (num(ev[0]), num(-ev[1])))
            doc.replace(e, "(end %s %s)" % (num(sv[0]), num(-sv[1])))
        pts = c.node("pts")
        if pts is not None:
            for xy in pts.nodes("xy"):
                v = xy.floats()
                doc.replace(xy, "(xy %s %s)" % (num(v[0]), num(-v[1])))


class Item:
    """A track segment/arc or a via."""
    __slots__ = ("board", "node", "kind", "a", "b", "w", "layer", "layers", "dia", "drill", "uuid", "_dead", "_lk")

    def __init__(self, board, node):
        self.board, self.node = board, node
        h = node.head
        self._dead = False
        self._lk = None
        self.uuid = _str(node, "uuid") or _str(node, "tstamp")
        if h == "via":
            self.kind = "via"
            at = _f(node, "at")
            self.a = self.b = (at[0], at[1])
            self.dia = _f(node, "size", [0.0])[0]
            self.drill = _f(node, "drill", [0.0])[0]
            lv = node.node("layers")
            self.layers = lv.vals() if lv is not None else ["F.Cu", "B.Cu"]
            self.layer = None
            self.w = self.dia
        else:
            self.kind = "track"
            s, e = _f(node, "start"), _f(node, "end")
            self.a, self.b = (s[0], s[1]), (e[0], e[1])
            self.w = _f(node, "width", [0.0])[0]
            self.layer = _str(node, "layer")
            self.layers = [self.layer]
            self.dia = self.drill = 0.0

    @property
    def net(self):
        n = self.node.node("net")
        return self.board.net_name_of(n) if n is not None else ""

    @property
    def locked(self):
        return self._lk if self._lk is not None else self.node.has_flag("locked")

    def set_locked(self, on):
        doc = self.board.doc
        self._lk = bool(on)
        cur = self.node.node("locked")
        if on and cur is None:
            before = self.node.node("net") if self.kind == "via" else self.node.node("layer")
            before = before or self.node.node("uuid")
            if before is not None:
                doc.insert_before(before, "(locked yes)")
            else:
                doc.insert_into(self.node, "(locked yes)")
        elif not on and cur is not None:
            doc.delete(cur)

    def delete(self):
        if not self._dead:
            self._dead = True
            self.board.doc.delete(self.node)

    @property
    def dead(self):
        return self._dead

    def copper_layers(self):
        """Copper layers this item occupies (a through via: every board copper layer between its two ends)."""
        if self.kind == "track":
            return {self.layer}
        cu = self.board.cu_layers
        if len(self.layers) >= 2 and self.layers[0] in cu and self.layers[-1] in cu:
            i, j = sorted((cu.index(self.layers[0]), cu.index(self.layers[-1])))
            return set(cu[i:j + 1])
        return set(self.layers)


class Zone:
    def __init__(self, board, node, index):
        self.board, self.node, self.index = board, node, index
        self.is_rule_area = node.node("keepout") is not None or (
            node.node("placement") is not None and _str(node.node("placement"), "enabled", "no") == "yes")

    @property
    def net(self):
        n = self.node.node("net")
        if n is None:
            return ""
        if self.board.numbered_nets and n.vals() and n.vals()[0].lstrip("-").isdigit():
            nn = self.node.node("net_name")
            if nn is not None and nn.vals():
                return nn.vals()[0]
        return self.board.net_name_of(n)

    @property
    def layers(self):
        cu = self.board.cu_layers
        names = []
        for key in ("layers", "layer"):
            n = self.node.node(key)
            if n is not None:
                names += n.vals()
        return self.board.expand_cu(names)

    @property
    def priority(self):
        p = self.node.node("priority")
        return int(float(p.vals()[0])) if p is not None and p.vals() else 0

    @property
    def polys(self):
        """[{'outer': [[x, y]...], 'holes': [[[x, y]...]]}]: a saved zone is one outline, its first `(polygon)` is the
        boundary and every further `(polygon)` a hole (how pcbnew reads it)."""
        pgs = [[[x, y] for x, y in _xy_list(pg)] for pg in self.node.nodes("polygon")]
        return [{"outer": pgs[0], "holes": pgs[1:]}] if pgs else []

    def keepout(self):
        k = self.node.node("keepout")
        r = {"no_tracks": False, "no_vias": False, "no_pads": False, "no_copper": False}
        if k is not None:
            for key, name in (("tracks", "no_tracks"), ("vias", "no_vias"), ("pads", "no_pads"), ("copperpour", "no_copper")):
                n = k.node(key)
                r[name] = bool(n is not None and n.vals() and n.vals()[0] == "not_allowed")
        return r


# ---------------------------------------------------------------------------------------------------------- board
class Board:
    def __init__(self, text, path=None):
        self.path = path
        self.doc = Doc(text)
        root = self.doc.root
        if root.head != "kicad_pcb":
            raise ValueError("not a .kicad_pcb file")
        self.root = root
        self.version = int(_f(root, "version", [0])[0])
        self._top = None
        self._fps = None
        self._items = None
        self._zones = None
        self.net_by_id = {}
        self.id_by_net = {}
        for n in self.top("net"):
            v = n.vals()
            if len(v) >= 2:
                self.net_by_id[int(float(v[0]))] = v[1]
                self.id_by_net[v[1]] = int(float(v[0]))
        self.numbered_nets = bool(self.net_by_id)
        self._cu = None

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8", newline="") as fh:
            return cls(fh.read(), path)

    # ---- structure
    def top(self, name=None):
        if self._top is None:
            self._top = [k for k in self.root.kids if isinstance(k, Node)]
        return [k for k in self._top if name is None or k.head == name]

    @property
    def cu_layers(self):
        if self._cu is None:
            names = []
            ls = self.root.node("layers")
            if ls is not None:
                for n in ls.nodes():
                    a = n.atoms()
                    if len(a) >= 3 and a[1].value.endswith(".Cu") and a[2].raw in ("signal", "power", "mixed", "jumper"):
                        names.append(a[1].value)
            self._cu = names or ["F.Cu", "B.Cu"]
        return self._cu

    @property
    def n_inner(self):
        return max(0, len(self.cu_layers) - 2)

    def expand_cu(self, names):
        """Layer names (with *.Cu / F&B.Cu wildcards) -> board copper layers in stack order."""
        cu = self.cu_layers
        out = set()
        for n in names:
            if n == "*.Cu":
                out |= set(cu)
            elif n == "F&B.Cu":
                out |= {cu[0], cu[-1]}
            elif n in cu:
                out.add(n)
        return [c for c in cu if c in out]

    def net_name_of(self, n):
        """Net name from a `(net ...)` node: `(net "X")`, `(net N "X")` or `(net N)`."""
        v = n.vals()
        if not v:
            return ""
        if len(v) >= 2:
            return v[1]
        if v[0].lstrip("-").isdigit() and self.numbered_nets:
            return self.net_by_id.get(int(v[0]), "")
        return v[0]

    @property
    def footprints(self):
        if self._fps is None:
            self._fps = [Footprint(self, n) for n in self.top("footprint")]
        return self._fps

    def footprint(self, ref):
        for f in self.footprints:
            if f.ref == ref:
                return f
        return None

    @property
    def items(self):
        """Track segments, arcs and vias in file order."""
        if self._items is None:
            self._items = [Item(self, n) for n in self.top() if n.head in ("segment", "via", "arc")]
        return self._items

    @property
    def zones(self):
        if self._zones is None:
            self._zones = [Zone(self, n, i) for i, n in enumerate(self.top("zone"))]
        return self._zones

    def drawings(self, layer=None):
        for n in self.top():
            if n.head and n.head.startswith("gr_") and n.head[3:] in _SHAPE_KINDS:
                if layer is None or _str(n, "layer") == layer:
                    yield n, _SHAPE_KINDS[n.head[3:]]

    def net_names(self):
        names = set(self.net_by_id.values())
        for f in self.footprints:
            for pd in f.pads:
                names.add(pd.net)
        for it in self.items:
            names.add(it.net)
        for z in self.zones:
            names.add(z.net)
        names.discard("")
        return sorted(names)

    # ---- edge cuts bounding box
    def edge_shapes(self):
        out = []
        for n, kind in self.drawings("Edge.Cuts"):
            pts, hw = shape_points(n, kind)
            if pts:
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                out.append({"kind": _SHAPE_STR[kind], "bbox": [min(xs) - hw, min(ys) - hw, max(xs) + hw, max(ys) + hw]})
        return out

    def edges_bbox(self):
        boxes = [e["bbox"] for e in self.edge_shapes()]
        for f in self.footprints:
            for n, kind, layer in f._graphics():
                if layer == "Edge.Cuts":
                    pts, hw = shape_points(n, kind)
                    w = [f.to_world(*p) for p in pts]
                    if w:
                        boxes.append([min(p[0] for p in w) - hw, min(p[1] for p in w) - hw,
                                      max(p[0] for p in w) + hw, max(p[1] for p in w) + hw])
        if not boxes:
            xs, ys = [], []
            for f in self.footprints:
                b = f.bbox()
                xs += [b[0], b[2]]
                ys += [b[1], b[3]]
            for it in self.items:
                for p in (it.a, it.b):
                    xs.append(p[0])
                    ys.append(p[1])
            if not xs:
                return [0.0, 0.0, 0.0, 0.0]
            return [min(xs), min(ys), max(xs), max(ys)]
        return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]

    # ---- rules (KiCad >= 7 keeps them in the sibling .kicad_pro)
    def rules(self):
        r = {"clearance": 0.2, "track_width": 0.2, "via_dia": 0.6, "via_drill": 0.3, "min_track": 0.2, "edge_clearance": 0.5}
        pro = self.path.rsplit(".", 1)[0] + ".kicad_pro" if self.path else None
        if pro and os.path.exists(pro):
            try:
                d = json.load(open(pro))
                ns = d.get("net_settings", {})
                cls = next((c for c in ns.get("classes", []) if c.get("name") == "Default"), None)
                if cls:
                    for k, key in (("clearance", "clearance"), ("track_width", "track_width"), ("via_dia", "via_diameter"),
                                   ("via_drill", "via_drill")):
                        if key in cls:
                            r[k] = float(cls[key])
                ds = d.get("board", {}).get("design_settings", {}).get("rules", {})
                if "min_track_width" in ds:
                    r["min_track"] = float(ds["min_track_width"])
                if "min_copper_edge_clearance" in ds:
                    r["edge_clearance"] = float(ds["min_copper_edge_clearance"])
            except (OSError, ValueError):
                pass
        return r

    # ---- nets for new / changed items
    def _declare_net(self, name):
        """Numbered formats only: make sure `(net N "name")` exists at top level; returns N."""
        if name in self.id_by_net:
            return self.id_by_net[name]
        nid = (max(self.net_by_id) + 1) if self.net_by_id else 1
        if not self.net_by_id:
            nid = 1
        self.net_by_id[nid] = name
        self.id_by_net[name] = nid
        text = dumps(["net", nid, Q(name)], 1)
        nets = self.top("net")
        if nets:
            self.doc.insert_after(nets[-1], text)
        else:
            anchor = self.root.node("layers") or self.root.node("setup")
            self.doc.insert_after(anchor, text)
        return nid

    def net_node(self, name, kind):
        """Tree for a net reference: kind 'pad' | 'item'."""
        if not self.numbered_nets:
            return ["net", Q(name)]
        nid = self._declare_net(name) if name else 0
        if kind == "pad":
            return ["net", nid, Q(name)]
        return ["net", nid]

    def set_pad_net(self, pad, name):
        doc = self.doc
        pad._net = name or ""
        cur = pad.node.node("net")
        if not name:
            if cur is not None:
                doc.delete(cur)
            return
        text = dumps(self.net_node(name, "pad"), 3)
        if cur is not None:
            doc.replace(cur, text)
            return
        for anchor in ("pinfunction", "pintype", "uuid"):
            n = pad.node.node(anchor)
            if n is not None:
                doc.insert_before(n, text)
                return
        doc.insert_into(pad.node, text)

    # ---- adding copper
    def _append_top(self, text):
        anchor = None
        for n in self.top():
            if n.head in ("segment", "via", "arc"):
                anchor = n
        if anchor is None:
            for n in self.top():
                if n.head in ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_text", "footprint"):
                    anchor = n
        # insertions after the same anchor keep call order
        if anchor is not None:
            self.doc.insert_after(anchor, text)
        else:
            self.doc.insert_into(self.root, text)

    def add_via(self, pos, dia, drill, layers, net):
        tree = ["via", ["at", pos[0], pos[1]], ["size", dia], ["drill", drill], ["layers", Q(layers[0]), Q(layers[-1])],
                self.net_node(net, "item"), ["uuid", Q(str(_uuid.uuid4()))]]
        self._append_top(dumps(tree, 1))

    def add_track(self, a, b, w, layer, net):
        tree = ["segment", ["start", a[0], a[1]], ["end", b[0], b[1]], ["width", w], ["layer", Q(layer)],
                self.net_node(net, "item"), ["uuid", Q(str(_uuid.uuid4()))]]
        self._append_top(dumps(tree, 1))

    # ---- zones
    def clear_fills(self, zone):
        """Drop the stale fill of a zone (KiCad / kicad-cli refill it)."""
        for fp in zone.node.nodes("filled_polygon"):
            self.doc.delete(fp)
        for fp in zone.node.nodes("fill_segments"):
            self.doc.delete(fp)

    def reshape_zone(self, zone, polys):
        """First polygon replaces the zone outline; every further polygon becomes a sibling zone (a saved zone holds one polygon)."""
        doc = self.doc
        pgs = zone.node.nodes("polygon")
        poly = lambda pts: ["polygon", ["pts"] + [["xy", x, y] for x, y in pts]]
        first = [poly(polys[0]["outer"])] + [poly(h) for h in polys[0]["holes"]]
        if pgs:
            doc.replace(pgs[0], dumps(first[0], 2))
            for extra in pgs[1:]:
                doc.delete(extra)
            for h in first[1:]:
                doc.insert_after(pgs[-1], dumps(h, 2))
        self.clear_fills(zone)
        for p in polys[1:]:
            self._sibling_zone(zone, [poly(p["outer"])] + [poly(h) for h in p["holes"]])

    def _sibling_zone(self, zone, poly_trees):
        node = zone.node
        text = self.doc.render(node.s, node.e)                       # includes patches made so far
        z = Doc(text)
        zr = z.root
        for pg in zr.nodes("polygon"):
            z.delete(pg)
        for fp in zr.nodes("filled_polygon"):
            z.delete(fp)
        u = zr.node("uuid")
        z.replace(u, '(uuid "%s")' % _uuid.uuid4())
        for pt_ in poly_trees:
            z.insert_into(zr, dumps(pt_, 2))
        out = z.render()
        self._append_after_zone(node, out)

    def _append_after_zone(self, node, text):
        self.doc.insert_after(node, text)

    # ---- output
    def text(self):
        return self.doc.render()

    def save(self, path=None):
        path = path or self.path
        data = self.text()
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
        return path
