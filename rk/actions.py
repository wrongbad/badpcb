"""Typed, reversible actions on a Board (+ topology hints for the router).

    w = World(board)                       # board is mutated in place; keep a pristine copy elsewhere
    a = parse("move R17 152 40.5")[0]      # compact text -> Action (JSON also accepted; ';' separates actions)
    errs, warns = check(w, a)              # legality, no mutation
    inv, nets = apply(w, a)                # mutate; inv is the inverse Action, nets = nets whose ratsnest changed
    apply(w, inv)                          # undo

Geometry actions (all coordinates mm, +Y down, rotation degrees CCW on screen as in KiCad):
  move REF X Y            absolute position of the part origin
  nudge REF DX DY         relative move
  rot REF DEG             absolute orientation (`rot C27 90`); with a sign it is relative (`rot C27 +90`, `rot C27 -90`)
  flip REF                other board side (mirrors the footprint)
Pin actions on the MCU (groups from the project's swap config, rk.json "swaps"; example ids adc, en):
  swap GROUP NETA NETB    exchange the MCU pins of two nets (`swap adc ADC0 ADC1`; group optional if the nets name it)
  assign GROUP NET PIN    move NET to another (free) pin (`assign en EN3 U1.12` or `assign en EN3 12`)
  keys PART_A PART_B      swap the key-group nets of two parts (needs a key_assignment group with net_regex in the swap config)
  via NET X Y [pad=U1.5]  add a plane via; with pad= a short stub track joins it to that pad
Plane zone edit (rk/actions.py reshape; apply rewrites the zone outlines, then refills):
  reshape NET add X0,Y0,X1,Y1 [layer=In2.Cu]   box becomes NET's: added to NET's zone and carved out of the other zones on that layer
  reshape NET sub X0,Y0,X1,Y1 [layer=In2.Cu]   box removed from NET's zone only
Copper lock actions (rk/locks.py; `rk apply` writes the lock flags, KRT treats locked copper as unrippable):
  unlock TARGET   TARGET = class (lanes|mimic|fanout|all), net or glob (SENSE4, EN*), region (R8) or box x0,y0,x1,y1
  lock TARGET     inverse of unlock
  trim-lanes      delete lane segments past the last key of each lane and lane ends sitting on another net's pad/via
Topology hints (stored in World.hints for groute; they never change the ratsnest):
  corridor NET via=C60,C97      side NET of=U1 dir=S        reserve CUT NET [NET ...]
  layer NET|GLOB LAYER          freeze R8|x0,y0,x1,y1       prio NET [LEVEL]
  near REF TO MAXMM             (extra) keep-within constraint: later moves of REF/TO that break it are refused
Append `force` to override locked-part / track warnings. `clear` removes a hint.
Legality: part inside the board, no *new* courtyard overlap with another part on the same side
(bbox approximation), no *new* pad-level conflict (pad-to-pad incl. through-hole, pad-to-foreign-copper, holes, edge; rk/legal.py), locked parts refused unless forced, swap-group rules from swaps.py, vias clear of
foreign copper and inside the plane zone of their net. Moving a part by <= 1 mm stretches the tracks attached to its pads
(unless the end is a junction/via or the stretched segment would violate clearance); larger moves leave them in place (warning, apply rips them).
"""
import fnmatch
import json
import math
import re
import shlex

from . import geom, swaps

HINT_OPS = ("corridor", "side", "reserve", "layer", "freeze", "priority", "near")
GEOM_OPS = ("move", "nudge", "rotate", "flip")
LOCK_OPS = ("unlock", "lock", "trim_lanes", "add_tracks")
ALL_OPS = GEOM_OPS + ("swap_pins", "assign", "reassign_keys", "add_via", "del_via", "reshape") + LOCK_OPS + HINT_OPS

# op -> (positional argument names, types).  Types: s str, f float, l list of str
SPEC = {
    "move": (("ref", "s"), ("x", "f"), ("y", "f")),
    "nudge": (("ref", "s"), ("dx", "f"), ("dy", "f")),
    "rotate": (("ref", "s"), ("deg", "r")),
    "flip": (("ref", "s"),),
    "swap_pins": (("group", "s"), ("a", "s"), ("b", "s")),
    "assign": (("group", "s"), ("net", "s"), ("pin", "s")),
    "reassign_keys": (("a", "s"), ("b", "s")),
    "add_via": (("net", "s"), ("x", "f"), ("y", "f")),
    "del_via": (("net", "s"), ("x", "f"), ("y", "f")),
    "reshape": (("net", "s"), ("mode", "s"), ("box", "l")),
    "corridor": (("net", "s"), ("via", "l")),
    "side": (("net", "s"), ("of", "s"), ("dir", "s")),
    "reserve": (("cut", "s"), ("nets", "l")),
    "layer": (("target", "s"), ("layer", "s")),
    "freeze": (("region", "s"),),
    "priority": (("net", "s"), ("level", "f")),
    "near": (("ref", "s"), ("to", "s"), ("max", "f")),
    "unlock": (("target", "s"),),
    "lock": (("target", "s"),),
    "trim_lanes": (),
    "add_tracks": (),
}
ALIASES = {"mv": "move", "place": "move", "nd": "nudge", "rot": "rotate", "r": "rotate", "swap": "swap_pins",
           "sw": "swap_pins", "asg": "assign", "pin": "assign", "keys": "reassign_keys", "swapkeys": "reassign_keys",
           "via": "add_via", "zone": "reshape", "plane": "reshape", "addvia": "add_via", "delvia": "del_via", "cor": "corridor", "res": "reserve",
           "trim": "trim_lanes", "trimlanes": "trim_lanes", "unl": "unlock", "lay": "layer", "prio": "priority", "pri": "priority", "keep": "near"}
FILLER = {"to", "by", "at", "->", "@", "=", "of:", "on", "the"}


class ActionError(Exception):
    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


class Action:
    """op + args; JSON-serialisable via to_dict/from_dict; compact() is the text form an agent can type."""

    def __init__(self, op, force=False, **args):
        if op not in ALL_OPS:
            raise ActionError("unknown action '%s' (known: %s)" % (op, ", ".join(ALL_OPS)))
        self.op, self.force, self.a = op, bool(force), args

    def to_dict(self):
        d = {"op": self.op}
        d.update(self.a)
        if self.force:
            d["force"] = True
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        return cls(d.pop("op"), d.pop("force", False), **d)

    def __eq__(self, o):
        return isinstance(o, Action) and self.to_dict() == o.to_dict()

    def __repr__(self):
        return "Action(%s)" % self.compact()

    def get(self, k, default=None):
        return self.a.get(k, default)

    def compact(self):
        a, op = self.a, self.op
        f = lambda v: ("%.3f" % v).rstrip("0").rstrip(".")
        if op == "move":
            s = "move %s %s %s" % (a["ref"], f(a["x"]), f(a["y"])) + (" rot=%s" % f(a["rot"]) if a.get("rot") is not None else "")
        elif op == "nudge":
            s = "nudge %s %s %s" % (a["ref"], f(a["dx"]), f(a["dy"]))
        elif op == "rotate":
            s = "rot %s %s%s" % (a["ref"], "+" if a.get("rel") and a["deg"] >= 0 else "", f(a["deg"]))
        elif op == "flip":
            s = "flip %s" % a["ref"]
        elif op == "swap_pins":
            s = "swap %s %s %s" % (a["group"], a["a"], a["b"])
        elif op == "assign":
            s = "assign %s %s %s" % (a["group"], a["net"], a["pin"])
        elif op == "reassign_keys":
            s = "keys %s %s" % (a["a"], a["b"])
        elif op == "add_via":
            s = "via %s %s %s%s" % (a["net"], f(a["x"]), f(a["y"]), " pad=%s" % a["pad"] if a.get("pad") else "")
        elif op == "del_via":
            s = "delvia %s %s %s%s" % (a["net"], f(a["x"]), f(a["y"]), " stub" if a.get("stub") else "")
        elif op == "reshape":
            if a.get("mode") == "restore":
                s = "reshape %s restore (%d zone(s))" % (a["net"], len(a.get("state", [])))
            else:
                s = "reshape %s %s %s%s" % (a["net"], a["mode"], ",".join(f(v) for v in a["box"]), " layer=%s" % a["layer"] if a.get("layer") else "")
        elif op == "corridor":
            s = "corridor %s via=%s" % (a["net"], ",".join(a.get("via") or []))
        elif op == "side":
            s = "side %s of=%s dir=%s" % (a["net"], a["of"], a["dir"])
        elif op == "reserve":
            s = "reserve %s %s" % (a["cut"], " ".join(a.get("nets") or []))
        elif op == "layer":
            s = "layer %s %s" % (a["target"], a["layer"])
        elif op == "freeze":
            s = "freeze %s" % a["region"]
        elif op == "near":
            s = "near %s %s %s" % (a["ref"], a["to"], f(a["max"]))
        elif op in ("unlock", "lock"):
            s = "%s %s" % (op, a.get("target") or "<%d items>" % len(a.get("ids", [])))
        elif op == "trim_lanes":
            s = "trim-lanes"
        elif op == "add_tracks":
            s = "restore %d tracks" % len(a.get("tracks", []))
        else:
            s = "prio %s %s" % (a["net"], f(a.get("level", 1)))
        if a.get("clear"):
            s += " clear"
        return s + (" force" if self.force else "")


# ----------------------------------------------------------------------------- parsing
def _num(tok, what):
    t = re.sub(r"(mm|deg|°)$", "", tok.strip().rstrip(","), flags=re.I)
    try:
        return float(t)
    except ValueError:
        raise ActionError("%s: expected a number, got '%s'" % (what, tok))


def parse_one(text):
    text = text.strip()
    if text.startswith("{"):
        return Action.from_dict(json.loads(text))
    toks = [t for t in shlex.split(text.replace("(", " ").replace(")", " ")) if t.lower() not in FILLER]
    if not toks:
        raise ActionError("empty action")
    op = toks[0].lower().replace("-", "_")
    op = ALIASES.get(op, op)
    if op not in SPEC:
        raise ActionError("unknown action '%s'. Syntax: move REF X Y | nudge REF DX DY | rot REF DEG | flip REF | "
                          "swap GROUP NETA NETB | assign GROUP NET PIN | keys SA SB | via NET X Y [pad=U1.5] | "
                          "corridor NET via=C1,C2 | side NET of=U1 dir=S | reserve CUT NET.. | layer NET LAYER | "
                          "freeze R8 | prio NET [N] | unlock|lock CLASS|NET|R8 | trim-lanes" % toks[0])
    force = clear = stub = False
    kw, pos = {}, []
    for t in toks[1:]:
        tl = t.lower()
        if tl == "stub" and op == "del_via":
            stub = True
        elif tl in ("force", "!"):
            force = True
        elif tl == "clear":
            clear = True
        elif "=" in t and not t.startswith("="):
            k, v = t.split("=", 1)
            kw[k.lower()] = v
        else:
            pos.append(t)
    spec = SPEC[op]
    names = [n for n, _ in spec]
    if op == "swap_pins" and len(pos) == 2 and "group" not in kw:
        pos = [""] + pos                       # group inferred later from the nets
    args = {}
    for k, v in list(kw.items()):
        if k in names:
            args[k] = v
            kw.pop(k)
    if kw and op == "add_via" and "pad" in kw:
        args["pad"] = kw.pop("pad")
    if kw and op == "reshape" and "layer" in kw:
        args["layer"] = kw.pop("layer")
    mrot = None
    if op == "move" and "rot" in kw:
        mrot = _num(kw.pop("rot"), "rot")
    if kw:
        raise ActionError("%s: unknown option(s) %s" % (op, ",".join(kw)))
    free = [(n, t) for n, t in spec if n not in args and not (op == "reshape" and n == "layer")]
    if op in ("corridor", "reserve", "layer", "freeze", "priority", "side"):
        pass
    for (n, t), tok in zip(free, pos):
        args[n] = tok
    extra = pos[len(free):]
    if extra:
        if op == "reserve" or op == "reshape" or (op == "corridor" and "via" in names):
            args[names[-1]] = ([args[names[-1]]] if names[-1] in args else []) + extra   # variadic tail
        else:
            raise ActionError("%s: too many arguments (%s)" % (op, " ".join(extra)))
    out = {}
    for n, t in spec:
        if n not in args:
            if (op == "priority" and n == "level"):
                out[n] = 1.0
                continue
            if op == "freeze" and n == "region" or n in ("via", "nets", "to", "max") and clear:
                out[n] = [] if t == "l" else ""
                continue
            raise ActionError("%s: missing argument '%s'" % (op, n))
        v = args[n]
        if t == "f":
            out[n] = _num(v, n)
        elif t == "r":
            tok = v.strip()
            out["rel"] = tok[:1] in "+-" or tok.lower().startswith("cw") or tok.lower().startswith("ccw")
            out[n] = _num(re.sub(r"^c?cw", "-" if tok.lower().startswith("cw") else "", tok, flags=re.I), n)
        elif t == "l":
            out[n] = [x for x in (v if isinstance(v, list) else re.split(r"[,\s]+", v)) if x]
        else:
            out[n] = v
    if mrot is not None:
        out["rot"] = mrot
    if op == "add_via" and "pad" in args:
        out["pad"] = args["pad"]
    if op == "reshape":
        if args.get("layer"):
            out["layer"] = args["layer"]
        if out["mode"].lower() not in ("add", "sub"):
            raise ActionError("reshape: mode must be add or sub (got '%s')" % out["mode"])
        out["mode"] = out["mode"].lower()
        if len(out["box"]) != 4:
            raise ActionError("reshape: box is X0,Y0,X1,Y1 (4 numbers, got %d)" % len(out["box"]))
        out["box"] = [_num(v, "box") for v in out["box"]]
    if op in ("move", "nudge", "flip", "rotate", "near"):
        out["ref"] = out["ref"].upper()
    if op == "near":
        out["to"] = out["to"].upper()
    if op == "side":
        out["dir"] = out["dir"].upper()
        out["of"] = out["of"].upper()
    if op in ("corridor", "reserve", "freeze"):
        for k in ("via", "cut", "region"):
            if k in out and isinstance(out[k], str):
                out[k] = out[k].upper()
        if op == "corridor":
            out["via"] = [x.upper() for x in out["via"]]
    if op == "add_via" and "pad" in out:
        out["pad"] = out["pad"].upper()
    if clear:
        out["clear"] = True
    if stub:
        out["stub"] = True
    return Action(op, force, **out)


def parse(text):
    """One string -> [Action]. Accepts compact syntax (';' or newline separated), a JSON object, or a JSON list."""
    t = text.strip()
    if t.startswith("["):
        return [Action.from_dict(d) for d in json.loads(t)]
    if t.startswith("{"):
        return [Action.from_dict(json.loads(t))]
    out = [parse_one(x) for x in re.split(r"[;\n]", t) if x.strip()]
    if not out:
        raise ActionError("empty action")
    return out


# ----------------------------------------------------------------------------- world
class _Loc:
    """Part geometry in its own frame at rot 0, original side: lets any (pos, rot, side) be recomputed exactly."""
    __slots__ = ("pos0", "rot0", "side0", "lbbox", "pads")


def _to_local(dx, dy, rot):          # world offset -> unrotated local offset
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    return dx * c - dy * s, dx * s + dy * c


def _to_world(lx, ly, rot):
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    return lx * c + ly * s, -lx * s + ly * c


def _norm(a):
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def _swap_side(layers):
    m = {"F.Cu": "B.Cu", "B.Cu": "F.Cu"}
    return [m.get(l, l) for l in layers]


class World:
    def __init__(self, board, cfg=None):
        self.board = board
        self.cfg = cfg if cfg is not None else swaps.load_config(board=board)
        self.hints = {"corridor": {}, "side": {}, "reserve": {}, "layer": {}, "freeze": {}, "priority": {}, "near": {}}
        self.cuts = None        # set of valid cut ids / region ids when known (validates hints)
        self.regions = None
        self.loc = {}
        for ref, p in board.parts.items():
            L = _Loc()
            L.pos0, L.rot0, L.side0 = p.pos, p.rot, p.side
            corners = [(p.bbox[0], p.bbox[1]), (p.bbox[2], p.bbox[1]), (p.bbox[2], p.bbox[3]), (p.bbox[0], p.bbox[3])]
            loc = [_to_local(x - p.pos[0], y - p.pos[1], p.rot) for x, y in corners]
            L.lbbox = (min(x for x, _ in loc), min(y for _, y in loc), max(x for x, _ in loc), max(y for _, y in loc))
            L.pads = []
            for pd in p.pads:
                lx, ly = _to_local(pd.pos[0] - p.pos[0], pd.pos[1] - p.pos[1], p.rot)
                L.pads.append((lx, ly, pd.rot - p.rot, list(pd.layers)))
            self.loc[ref] = L

    # -- hint store
    def hints_dict(self):
        return json.loads(json.dumps(self.hints))

    def groups(self):
        return swaps.groups(self.board, self.cfg) if self.cfg["mcu"] in self.board.parts else {}

    def signal_layers(self):
        return self.board.signal_layers()

    def net_set(self):
        s = getattr(self, "_netset", None)
        if s is None or s[0] is not self.board.nets:
            s = (self.board.nets, set(self.board.nets) | {pd.net for pd in self.board.pads() if pd.net})
            self._netset = s
        return s[1]


def place_geom(world, ref, pos, rot, side):
    """Recomputed (bbox, [(pad_pos, pad_rot, pad_layers)]) of part `ref` at (pos, rot, side)."""
    L = world.loc[ref]
    flipped = side != L.side0
    bx0, by0, bx1, by1 = L.lbbox
    if flipped:
        bx0, bx1 = -bx1, -bx0
    cs = [_to_world(x, y, rot) for x, y in ((bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1))]
    bbox = (pos[0] + min(c[0] for c in cs), pos[1] + min(c[1] for c in cs),
            pos[0] + max(c[0] for c in cs), pos[1] + max(c[1] for c in cs))
    pads = []
    part = world.board.parts[ref]
    for (lx, ly, lrot, layers), pd in zip(L.pads, part.pads):
        if flipped:
            lx, lrot = -lx, -lrot
        wx, wy = _to_world(lx, ly, rot)
        smd_layers = _swap_side(layers) if flipped and not pd.through else layers
        pads.append(((pos[0] + wx, pos[1] + wy), _norm(lrot + rot), smd_layers))
    return bbox, pads


def _set_place(world, ref, pos, rot, side):
    part = world.board.parts[ref]
    bbox, pads = place_geom(world, ref, pos, rot, side)
    part.pos, part.rot, part.side, part.bbox = (pos[0], pos[1]), _norm(rot), side, bbox
    world.place_ver = getattr(world, "place_ver", 0) + 1
    for pd, (pp, pr, pl) in zip(part.pads, pads):
        pd.pos, pd.rot, pd.layers = pp, pr, pl


# ----------------------------------------------------------------------------- legality helpers
def fits(world, ref, pos, rot, side=None):
    """Fast placement test: courtyard inside the board and no *new* same-side courtyard overlap (no locked/track checks)."""
    p = world.board.parts[ref]
    side = side or p.side
    bbox, _ = place_geom(world, ref, pos, rot, side)
    if not _inside_board(world.board, bbox):
        return False
    new = _conflicts(world, ref, bbox, side)
    if new:
        was = set(_conflicts(world, ref, p.bbox, p.side))
        if not all(r in was for r in new):
            return False
    from . import legal
    return not legal.errors(legal.new_pad_conflicts(world, ref, pos, rot, side))


def _pt_rect_dist(pt, pd):
    lx, ly = geom.to_local(pt, pd.pos, pd.rot)
    dx, dy = max(abs(lx) - pd.size[0] / 2, 0.0), max(abs(ly) - pd.size[1] / 2, 0.0)
    return math.hypot(dx, dy)


def _inside_board(board, bb, margin=0.0):
    x0, y0, x1, y1 = board.bbox
    return bb[0] >= x0 + margin and bb[1] >= y0 + margin and bb[2] <= x1 - margin and bb[3] <= y1 - margin


def _overlap(a, b, tol=0.05):
    return min(a[2], b[2]) - max(a[0], b[0]) > tol and min(a[3], b[3]) - max(a[1], b[1]) > tol


def _through(part):
    return any(pd.through for pd in part.pads)


def _conflicts(world, ref, bbox, side):
    """Parts whose courtyard overlaps `bbox` when part `ref` sits there (same side, or through-hole)."""
    parts = world.board.parts
    mt = None
    x0, y0, x1, y1 = bbox
    out = []
    for r, p in parts.items():
        bb = p.bbox
        if bb[2] - x0 <= 0.05 or x1 - bb[0] <= 0.05 or bb[3] - y0 <= 0.05 or y1 - bb[1] <= 0.05 or r == ref or not p.pads:
            continue
        if p.side != side:
            if mt is None:
                mt = _through(parts[ref])
            if not (mt or _through(p)):
                continue
        out.append(r)
    return out


def _pad_tracks(board, pd, tracks):
    hx, hy = pd.size[0] / 2, pd.size[1] / 2
    r = math.hypot(hx, hy) + 0.5
    out = []
    for i, t in enumerate(tracks):
        if t.net != pd.net or t.layer not in pd.layers:
            continue
        if min(t.a[0], t.b[0]) > pd.pos[0] + r or max(t.a[0], t.b[0]) < pd.pos[0] - r or \
           min(t.a[1], t.b[1]) > pd.pos[1] + r or max(t.a[1], t.b[1]) < pd.pos[1] - r:
            continue
        if geom.seg_hits_rect(t.a, t.b, pd.pos, (hx, hy), pd.rot, t.w / 2):
            out.append(i)
    return out


def attached_tracks(board, ref, pads=None):
    """Indices of tracks (same net, on a pad layer) touching the pads of `ref` (optionally only pad numbers `pads`)."""
    out = set()
    for pd in board.parts[ref].pads:
        if pd.net and (pads is None or pd.num in pads):
            out.update(_pad_tracks(board, pd, board.tracks))
    return sorted(out)


def _part(world, ref, errs):
    p = world.board.parts.get(ref)
    if p is None:
        errs.append("no part '%s'" % ref)
    return p


def _check_place(world, act, ref, pos, rot, side, errs, warns):
    p = _part(world, ref, errs)
    if p is None:
        return
    if p.locked and not act.force:
        errs.append("%s is locked (add 'force' to override)" % ref)
    bbox, pads = place_geom(world, ref, pos, rot, side)
    if not _inside_board(world.board, bbox):
        errs.append("%s would leave the board (courtyard %.1f,%.1f..%.1f,%.1f outside %s)" % (
            ref, *bbox, "%.0f,%.0f..%.0f,%.0f" % tuple(world.board.bbox)))
    was = set(_conflicts(world, ref, p.bbox, p.side))
    new = [r for r in _conflicts(world, ref, bbox, side) if r not in was]
    if new:
        errs.append("%s would overlap the courtyard of %s" % (ref, ",".join(sorted(new)[:6])))
    from . import legal
    pc = legal.new_pad_conflicts(world, ref, pos, rot, side)
    errs += legal.errors(pc)[:6]
    warns += legal.warnings(pc)[:4]
    for r, nh in world.hints["near"].items():          # keep-near constraints: `near R21 U1 14`
        if r == ref or nh["to"] == ref:
            a_, b_ = (pos, world.board.parts[nh["to"]].pos) if r == ref else (world.board.parts[r].pos, pos)
            if geom.dist(a_, b_) > nh["max"] + 1e-9 and not act.force:
                errs.append("%s must stay within %.1f mm of %s (hint `near`; now %.1f). Add 'force' or `near %s %s clear`" % (
                    r, nh["max"], nh["to"], geom.dist(a_, b_), r, nh["to"]))
    n = len(attached_tracks(world.board, ref))
    if n:
        mv, stay = drag_plan(world, ref, pos, rot, side)
        if mv:
            warns.append("%s: %d attached track(s) are stretched to follow the part%s" % (
                ref, len(mv), ("; %d stay in place (rip up in apply)" % len(stay)) if stay else ""))
        elif stay:
            warns.append("%s has %d attached track(s); they stay in place (rip up in apply)" % (ref, len(stay)))


def _norm_pin(world, pin):
    pin = str(pin)
    ref = world.cfg["mcu"]
    if pin.upper().startswith(ref + "."):
        pin = pin[len(ref) + 1:]
    return pin


def _group(world, gid, errs):
    gs = world.groups()
    if gid not in gs:
        errs.append("unknown group '%s' (have: %s)" % (gid, ",".join(g for g, G in gs.items() if G.kind in ("pin_permutation", "pin_assignment"))))
        return None
    return gs[gid]


def _infer_group(world, a, b):
    for gid, G in world.groups().items():
        if a in G.nets and b in G.nets:
            return gid
    return None


def local_obstacles(board, center, radius):
    """Pads / vias / tracks / holes within `radius` of `center` (speeds up repeated clearance probes)."""
    x0, y0, x1, y1 = center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius
    inb = lambda a, b: not (max(a[0], b[0]) < x0 or min(a[0], b[0]) > x1 or max(a[1], b[1]) < y0 or min(a[1], b[1]) > y1)
    return {"pads": [p for p in board.pads() if x0 - 3 <= p.pos[0] <= x1 + 3 and y0 - 3 <= p.pos[1] <= y1 + 3],
            "vias": [v for v in board.vias if x0 - 1 <= v.pos[0] <= x1 + 1 and y0 - 1 <= v.pos[1] <= y1 + 1],
            "tracks": [t for t in board.tracks if inb(t.a, t.b)],
            "holes": [h for h in board.holes if x0 - 3 <= h.pos[0] <= x1 + 3 and y0 - 3 <= h.pos[1] <= y1 + 3]}


def _clearance_issues(world, net, pt, r, layers=None, seg=None, w=0.0, obs=None):
    """Foreign copper too close to a via (pt, radius r) or a stub segment (seg, width w) on `layers`."""
    b = world.board
    clr = b.rules.clearance
    bad = []
    pts = [pt] if seg is None else [(seg[0][0] + (seg[1][0] - seg[0][0]) * i / 10.0, seg[0][1] + (seg[1][1] - seg[0][1]) * i / 10.0) for i in range(11)]
    rr = (r if seg is None else w / 2) + clr
    obs = obs or {"pads": b.pads(), "vias": b.vias, "tracks": b.tracks, "holes": b.holes}
    for pd in obs["pads"]:
        if pd.net == net or (layers is not None and not (set(layers) & set(pd.layers))):
            continue
        rad = max(pd.size) / 2 + rr + 0.01
        for p in pts:
            if abs(p[0] - pd.pos[0]) <= rad and abs(p[1] - pd.pos[1]) <= rad and _pt_rect_dist(p, pd) < rr:
                bad.append("pad %s" % pd.id)
                break
    for v in obs["vias"]:
        if v.net != net and any(geom.dist(p, v.pos) < v.dia / 2 + rr for p in pts):
            bad.append("via of %s at %.2f,%.2f" % (v.net or "-", *v.pos))
    for t in obs["tracks"]:
        if t.net == net or (layers is not None and t.layer not in layers):
            continue
        if any(geom.pt_seg_dist(p, t.a, t.b) < t.w / 2 + rr for p in pts):
            bad.append("track %s" % t.net)
    for h in obs["holes"]:
        if any(geom.dist(p, h.pos) < h.drill / 2 + rr for p in pts):
            bad.append("hole %s" % h.ref)
    return sorted(set(bad))


def _in_zone(board, net, pt):
    for z in board.zones:
        if z.net == net:
            for zp in z.polys:
                if geom.point_in_poly(pt, zp["outer"]) and not any(geom.point_in_poly(pt, h) for h in zp["holes"]):
                    return True
    return False


def _pad_by_id(board, pid):
    ref, _, num = pid.partition(".")
    if ref in board.parts:
        for pd in board.parts[ref].pads:
            if pd.num == num:
                return pd
    return None


def _key_rx(world):
    """Net regex of the key-assignment group in the swap config ({"kind": "key_assignment", "net_regex": "^EN\\d+$"}), else None."""
    for g in world.cfg.get("groups", []):
        if g.get("kind") == "key_assignment" and g.get("net_regex"):
            return re.compile(g["net_regex"])
    return None


def _key_pad(board, ref, world):
    p = board.parts.get(ref)
    rx = _key_rx(world)
    if p and rx:
        for pd in p.pads:
            if rx.match(pd.net or ""):
                return pd
    return None




# ----------------------------------------------------------------------------- dragging attached copper with a small move
DRAG_MAX = 1.0        # mm: pads that move farther than this leave their tracks in place (apply rips them)


def _end_on_pad(q, pd, tol=0.02):
    lx, ly = geom.to_local(q, pd.pos, pd.rot)
    return abs(lx) <= pd.size[0] / 2 + tol and abs(ly) <= pd.size[1] / 2 + tol


def drag_plan(world, ref, pos, rot, side):
    """Tracks whose pad end can follow the part: -> (moves {track index: {'a'|'b': new point}}, stay [track index]).
    Pure. A track follows when its pad moved <= DRAG_MAX, the end is not shared with other copper of the net (junction / via)
    and the stretched segment keeps clearance to foreign copper; otherwise it stays (apply rips it, as before)."""
    b = world.board
    p = b.parts[ref]
    allt = attached_tracks(b, ref)
    if side != p.side:
        return {}, allt
    _bb, newpads = place_geom(world, ref, pos, rot, side)
    moves = {}
    for pd, (pp, _pr, _pl) in zip(p.pads, newpads):
        if not pd.net:
            continue
        shift = (pp[0] - pd.pos[0], pp[1] - pd.pos[1])
        if abs(shift[0]) < 1e-9 and abs(shift[1]) < 1e-9:
            continue
        for ti in _pad_tracks(b, pd, b.tracks):
            t = b.tracks[ti]
            for end in ("a", "b"):
                q = getattr(t, end)
                if _end_on_pad(q, pd):
                    if math.hypot(*shift) > DRAG_MAX:
                        continue
                    moves.setdefault(ti, {})[end] = (round(q[0] + shift[0], 4), round(q[1] + shift[1], 4))
    stay = [ti for ti in allt if ti not in moves]
    for ti in list(moves):
        t = b.tracks[ti]
        ok = True
        for end, np_ in moves[ti].items():
            q = getattr(t, end)
            for o in b.vias:                                   # junction with a via / another track end of the net: cannot follow
                if o.net == t.net and geom.dist(o.pos, q) < 0.02:
                    ok = False
            for j, o in enumerate(b.tracks):
                if j != ti and o.net == t.net and (geom.dist(o.a, q) < 0.02 or geom.dist(o.b, q) < 0.02) and not (j in moves):
                    ok = False
        na = moves[ti].get("a", t.a)
        nb = moves[ti].get("b", t.b)
        if ok:
            obs = local_obstacles(b, ((na[0] + nb[0]) / 2, (na[1] + nb[1]) / 2), geom.dist(na, nb) / 2 + 2.0)
            if _clearance_issues(world, t.net, None, 0, layers=[t.layer], seg=(na, nb), w=t.w, obs=obs):
                ok = False
        if not ok:
            del moves[ti]
            stay.append(ti)
    return moves, sorted(set(stay))


# ----------------------------------------------------------------------------- plane zone edits (reshape)
def _poly_pieces(pz, box):
    """One zone polygon {'outer','holes'} minus box -> list of polygons (same dict shape)."""
    outer = pz["outer"]
    bb = geom.bbox_of(outer)
    if bb[2] <= box[0] or bb[0] >= box[2] or bb[3] <= box[1] or bb[1] >= box[3]:
        return [pz]
    corners = geom.box_poly(box)
    if all(geom.point_in_poly(c, outer) for c in corners) and not any(box[0] < v[0] < box[2] and box[1] < v[1] < box[3] for v in outer):
        return [{"outer": outer, "holes": [list(h) for h in pz["holes"]] + [corners]}]
    pieces = geom.poly_minus_box(outer, box)
    out = []
    for pc in pieces:
        hs = []
        for h in pz["holes"]:
            c = (sum(v[0] for v in h) / len(h), sum(v[1] for v in h) / len(h))
            if geom.point_in_poly(c, pc):
                hs.append(h)
        out.append({"outer": pc, "holes": hs})
    return out


def _reshape_plan(world, net, mode, box, layer):
    """-> (edits [(zone index, new polys)], errs, warns).  Pure: nothing is mutated."""
    b = world.board
    errs, warns = [], []
    box = [min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])]
    mine = [(i, z) for i, z in enumerate(b.zones) if z.net == net and (not layer or layer in z.layers)]
    if not any(z.net == net for z in b.zones):
        return [], ["reshape: no plane zone for net '%s' (zone nets: %s)" % (net, ", ".join(sorted({z.net for z in b.zones if z.net})) or "none")], warns
    if layer and not mine:
        return [], ["reshape: net %s has no zone on %s (it has %s)" % (net, layer, "/".join(sorted({l for z in b.zones if z.net == net for l in z.layers})))], warns
    if box[2] - box[0] < 0.2 or box[3] - box[1] < 0.2:
        errs.append("reshape: box %.2f x %.2f mm is degenerate" % (box[2] - box[0], box[3] - box[1]))
    x0, y0, x1, y1 = b.bbox
    if box[0] < x0 - 1 or box[1] < y0 - 1 or box[2] > x1 + 1 or box[3] > y1 + 1:
        errs.append("reshape: box lies outside the board")
    if errs:
        return [], errs, warns
    lays = set(layer and [layer] or [l for _, z in mine for l in z.layers])
    edits = []
    if mode == "add":
        for i, z in mine:
            polys = z.polys
            if all(_in_zone_of(z, c) for c in geom.box_poly(box)):
                continue
            edits.append((i, [dict(outer=p["outer"], holes=p["holes"]) for p in polys] + [{"outer": geom.box_poly(box), "holes": []}]))
        if not edits:
            errs.append("reshape: the box is already inside the %s zone; nothing to add" % net)
        for i, z in enumerate(b.zones):
            if z.net == net or not z.net or not (set(z.layers) & lays):
                continue
            new = []
            for pz in z.polys:
                new += _poly_pieces(pz, box)
            if new != z.polys:
                edits.append((i, new))
        gone = sorted({pd.id for pd in b.pads() if pd.net in {b.zones[i].net for i, _ in edits if b.zones[i].net != net} and
                       box[0] <= pd.pos[0] <= box[2] and box[1] <= pd.pos[1] <= box[3]}, key=lambda s: (len(s), s))
        if gone:
            warns.append("reshape: %d pad(s) of the carved-out plane(s) lie in the box and lose their plane (%s): move them or give them their own route" % (
                len(gone), " ".join(gone[:6])))
    else:
        for i, z in mine:
            new = []
            for pz in z.polys:
                new += _poly_pieces(pz, box)
            if new != z.polys:
                edits.append((i, new))
        if not edits:
            errs.append("reshape: the box does not touch the %s zone; nothing to subtract" % net)
        lost = sorted({pd.id for pd in b.pads() if pd.net == net and box[0] <= pd.pos[0] <= box[2] and box[1] <= pd.pos[1] <= box[3]}, key=lambda s: (len(s), s))
        if lost:
            warns.append("reshape: %d %s pad(s) lie in the box and lose their plane (%s)" % (len(lost), net, " ".join(lost[:6])))
    return edits, errs, warns


def _in_zone_of(z, pt):
    return any(geom.point_in_poly(pt, zp["outer"]) and not any(geom.point_in_poly(pt, h) for h in zp["holes"]) for zp in z.polys)


# ----------------------------------------------------------------------------- check
def check(world, act):
    """(errors, warnings) for applying `act` to the current world state. Does not mutate."""
    errs, warns = [], []
    b, op, a = world.board, act.op, act.a
    if op in ("move", "nudge", "rotate", "flip"):
        p = _part(world, a["ref"], errs)
        if p is None:
            return errs, warns
        if op == "move":
            pos, rot, side = (a["x"], a["y"]), (a["rot"] if a.get("rot") is not None else p.rot), p.side
        elif op == "nudge":
            pos, rot, side = (p.pos[0] + a["dx"], p.pos[1] + a["dy"]), p.rot, p.side
        elif op == "rotate":
            pos, rot, side = p.pos, (p.rot + a["deg"] if a.get("rel") else a["deg"]), p.side
        else:
            pos, rot, side = p.pos, p.rot, ("F" if p.side == "B" else "B")
        _check_place(world, act, a["ref"], pos, rot, side, errs, warns)
    elif op in ("swap_pins", "assign"):
        gid = a.get("group") or (_infer_group(world, a["a"], a["b"]) if op == "swap_pins" else None)
        G = _group(world, gid, errs) if gid else None
        if gid is None and op == "swap_pins":
            errs.append("cannot infer a group for %s,%s" % (a["a"], a["b"]))
        if G is None:
            return errs, warns
        cur = dict(G.current)
        ref = world.cfg["mcu"]
        if op == "swap_pins":
            na = a["a"] if a["a"] in cur else {p: n for n, p in cur.items()}.get(_norm_pin(world, a["a"]))
            nb = a["b"] if a["b"] in cur else {p: n for n, p in cur.items()}.get(_norm_pin(world, a["b"]))
            if na is None or nb is None:
                errs.append("nets/pins %s,%s are not both in group %s (%s)" % (a["a"], a["b"], gid, " ".join(G.nets)))
                return errs, warns
            if na == nb:
                errs.append("same net twice")
                return errs, warns
            new = dict(cur)
            new[na], new[nb] = cur[nb], cur[na]
            touched = [cur[na], cur[nb]]
        else:
            if a["net"] not in cur:
                errs.append("net %s is not on %s in group %s (it has: %s)" % (a["net"], ref, gid, " ".join(sorted(cur))))
                return errs, warns
            pin = _norm_pin(world, a["pin"])
            holder = {p: n for n, p in cur.items()}.get(pin)
            if holder == a["net"]:
                errs.append("%s is already on pin %s" % (a["net"], pin))
                return errs, warns
            if holder:
                errs.append("pin %s already carries %s (use: swap %s %s %s)" % (pin, holder, gid, a["net"], holder))
                return errs, warns
            new = dict(cur)
            new[a["net"]] = pin
            touched = [cur[a["net"]], pin]
        errs += swaps.check(b, world.cfg, gid, new)
        n = len(attached_tracks(b, ref, set(touched)))
        if n and not errs:
            warns.append("%s has %d attached track(s) (e.g. pin stubs); they stay in place" % (ref, n))
    elif op == "reassign_keys":
        pa, pb = _key_pad(b, a["a"], world), _key_pad(b, a["b"], world)
        if _key_rx(world) is None:
            errs.append("keys: the project has no key_assignment group with a net_regex in its swap config (rk.json)")
        elif pa is None or pb is None:
            errs.append("%s/%s: need two parts with a pad on a key-assignment net" % (a["a"], a["b"]))
        elif pa.net == pb.net:
            errs.append("both keys are already on %s" % pa.net)
        else:
            for ref, pd in ((a["a"], pa), (a["b"], pb)):
                n = len(attached_tracks(b, ref))
                if n and not act.force:
                    errs.append("%s has %d routed track(s) on its key-assignment pad (add 'force' to leave them)" % (ref, n))
    elif op == "add_via":
        net, pt = a["net"], (a["x"], a["y"])
        if net not in world.net_set():
            errs.append("no net '%s'" % net)
            return errs, warns
        rl = b.rules
        if not _inside_board(b, (pt[0] - rl.via_dia / 2, pt[1] - rl.via_dia / 2, pt[0] + rl.via_dia / 2, pt[1] + rl.via_dia / 2), rl.edge_clearance):
            errs.append("via outside the board")
        if net in b.plane_nets() and not _in_zone(b, net, pt):
            errs.append("(%.1f,%.1f) is outside the %s plane zone: a via there reaches no plane" % (pt[0], pt[1], net))
        bad = _clearance_issues(world, net, pt, rl.via_dia / 2)
        if bad:
            errs.append("via too close to " + ", ".join(bad[:4]))
        if a.get("pad"):
            pd = _pad_by_id(b, a["pad"])
            if pd is None or pd.net != net:
                errs.append("pad %s is not a pad of %s" % (a["pad"], net))
            else:
                lay = _stub_layer(world, pd)
                bad = _clearance_issues(world, net, None, 0, layers=[lay], seg=(pd.pos, pt), w=rl.track_width)
                if bad:
                    errs.append("stub from %s blocked by %s" % (a["pad"], ", ".join(bad[:4])))
    elif op == "reshape":
        if a.get("mode") == "restore":
            return errs, warns
        _e, errs, warns = _reshape_plan(world, a["net"], a["mode"], a["box"], a.get("layer"))
    elif op == "del_via":
        if not any(v.net == a["net"] and geom.dist(v.pos, (a["x"], a["y"])) < 1e-6 for v in b.vias):
            errs.append("no via of %s at %.3f,%.3f" % (a["net"], a["x"], a["y"]))
    elif op in ("unlock", "lock"):
        if a.get("ids"):
            return errs, warns
        from . import locks
        try:
            items, what = locks.resolve(b, a["target"], getattr(world, "region_boxes", None))
        except ValueError as e:
            errs.append(str(e))
            return errs, warns
        todo = [i for i in items if i.locked == (op == "unlock")]
        if not todo:
            errs.append("%s %s: nothing to do (%d item(s) match %s, none is %s)" % (op, a["target"], len(items), what,
                                                                                  "locked" if op == "unlock" else "unlocked"))
        else:
            warns.append("%s %s: %d of %d item(s) (%s)" % (op, a["target"], len(todo), len(items),
                                                           " ".join("%s:%d" % kv for kv in sorted(_by_cls(todo).items()))))
    elif op == "trim_lanes":
        from . import locks
        c = locks.trim_candidates(b)
        if not c:
            errs.append("trim-lanes: no lane segment to trim")
        else:
            past = sum(1 for _, r in c if "past" in r or "before" in r or "no key" in r)
            warns.append("trim-lanes: %d lane segment(s) go: %d beyond the last/first key of their lane, %d on another net's pad/via%s" % (
                len(c), past, len(c) - past, "".join("; " + r for _, r in c if not ("past" in r or "before" in r or "no key" in r))[:300]))
    elif op == "add_tracks":
        pass
    elif op in HINT_OPS:
        _check_hint(world, act, errs)
    return errs, warns


def _by_cls(items):
    from . import locks
    d = {}
    for i in items:
        d[locks.cls_of(i)] = d.get(locks.cls_of(i), 0) + 1
    return d


def _stub_layer(world, pd):
    sig = world.signal_layers()
    for l in pd.layers:
        if l in sig:
            return l
    return pd.layers[0]


def _check_hint(world, act, errs):
    a, op = act.a, act.op
    nets = world.net_set()
    if a.get("clear"):
        return
    if op == "corridor":
        if a["net"] not in nets:
            errs.append("no net '%s'" % a["net"])
        for c in a.get("via") or []:
            if world.cuts is not None and c not in world.cuts:
                errs.append("no cut '%s'" % c)
    elif op == "side":
        if a["net"] not in nets:
            errs.append("no net '%s'" % a["net"])
        if a["of"] not in world.board.parts:
            errs.append("no part '%s'" % a["of"])
        if a["dir"] not in ("N", "E", "S", "W", "NE", "NW", "SE", "SW"):
            errs.append("dir must be N/E/S/W (or diagonals)")
    elif op == "reserve":
        if world.cuts is not None and a["cut"] not in world.cuts:
            errs.append("no cut '%s'" % a["cut"])
        for n in a.get("nets") or []:
            if n not in nets:
                errs.append("no net '%s'" % n)
    elif op == "layer":
        t = a["target"]
        if t not in nets and not any(fnmatch.fnmatch(n, t) for n in nets):
            errs.append("'%s' matches no net (globs like EN* allowed)" % t)
        if a["layer"] not in world.board.layers:
            errs.append("no layer '%s' (have %s)" % (a["layer"], ",".join(world.board.layers)))
        elif a["layer"] not in world.board.signal_layers():
            errs.append("layer %s is a plane layer" % a["layer"])
    elif op == "freeze":
        r = a.get("region", "")
        if not a.get("clear") and not re.match(r"^R\d+$", r) and not re.match(r"^-?[\d.]+(,-?[\d.]+){3}$", r):
            errs.append("region must be an id like R8 or 'x0,y0,x1,y1'")
        elif world.regions is not None and re.match(r"^R\d+$", r) and r not in world.regions:
            errs.append("no region '%s'" % r)
    elif op == "priority":
        if a["net"] not in nets:
            errs.append("no net '%s'" % a["net"])
    elif op == "near":
        for k in ("ref", "to"):
            if a[k] not in world.board.parts:
                errs.append("no part '%s'" % a[k])


# ----------------------------------------------------------------------------- apply
def _part_nets(part):
    return {pd.net for pd in part.pads if pd.net}


def apply(world, act, check_first=True):
    """Mutate the world. Returns (inverse Action, set of nets whose ratsnest changed). Raises ActionError."""
    if check_first:
        errs, _ = check(world, act)
        if errs:
            raise ActionError(errs)
    b, op, a = world.board, act.op, act.a
    if op in GEOM_OPS:
        p = b.parts[a["ref"]]
        nets = _part_nets(p)
        old = (p.pos, p.rot, p.side)
        pos, rot, side = p.pos, p.rot, p.side
        if op == "move":
            pos = (a["x"], a["y"])
            if a.get("rot") is not None:
                rot = a["rot"]
        elif op == "nudge":
            pos = (p.pos[0] + a["dx"], p.pos[1] + a["dy"])
        elif op == "rotate":
            rot = p.rot + a["deg"] if a.get("rel") else a["deg"]
        else:
            side = "F" if p.side == "B" else "B"
        undo = a.get("drag_undo")
        mv = {} if undo is not None else drag_plan(world, a["ref"], pos, rot, side)[0]
        saved = [[ti, list(b.tracks[ti].a), list(b.tracks[ti].b)] for ti in sorted(mv)]
        _set_place(world, a["ref"], pos, rot, side)
        if undo is not None:                               # inverse of a dragging move: put the stretched ends back exactly
            for ti, ta, tb in undo:
                b.tracks[ti].a, b.tracks[ti].b = tuple(ta), tuple(tb)
        for ti, ends in mv.items():
            t = b.tracks[ti]
            if "a" in ends:
                t.a = ends["a"]
            if "b" in ends:
                t.b = ends["b"]
        if op == "flip":
            return Action("flip", ref=a["ref"], force=True), nets
        if op == "rotate":
            return Action("rotate", force=True, ref=a["ref"], deg=_norm(old[1]), rel=False, **({"drag_undo": saved} if saved else {})), nets
        return Action("move", force=True, ref=a["ref"], x=old[0][0], y=old[0][1], rot=_norm(old[1]), **({"drag_undo": saved} if saved else {})), nets
    if op in ("swap_pins", "assign"):
        gid = a.get("group") or _infer_group(world, a["a"], a["b"])
        G = world.groups()[gid]
        cur = dict(G.current)
        inv_pin = {p: n for n, p in cur.items()}
        ref = world.cfg["mcu"]
        if op == "swap_pins":
            na = a["a"] if a["a"] in cur else inv_pin[_norm_pin(world, a["a"])]
            nb = a["b"] if a["b"] in cur else inv_pin[_norm_pin(world, a["b"])]
            pa, pb = cur[na], cur[nb]
            for pd in b.parts[ref].pads:
                if pd.num == pa:
                    pd.net = nb
                elif pd.num == pb:
                    pd.net = na
            return Action("swap_pins", force=True, group=gid, a=na, b=nb), {na, nb}
        pin = _norm_pin(world, a["pin"])
        old = cur[a["net"]]
        for pd in b.parts[ref].pads:
            if pd.num == old:
                pd.net = ""
            elif pd.num == pin:
                pd.net = a["net"]
        return Action("assign", force=True, group=gid, net=a["net"], pin=old), {a["net"]}
    if op == "reassign_keys":
        pa, pb = _key_pad(b, a["a"], world), _key_pad(b, a["b"], world)
        na, nb = pa.net, pb.net
        pa.net, pb.net = nb, na
        return Action("reassign_keys", force=True, a=a["a"], b=a["b"]), {na, nb}
    if op == "add_via":
        rl = b.rules
        from .model import Track, Via
        pt = (a["x"], a["y"])
        b.vias.append(Via(pt, a["net"], rl.via_dia, rl.via_drill, False, [b.layers[0], b.layers[-1]]))
        stub = False
        if a.get("pad"):
            pd = _pad_by_id(b, a["pad"])
            b.tracks.append(Track(tuple(pd.pos), pt, rl.track_width, _stub_layer(world, pd), a["net"], False))
            stub = True
        return Action("del_via", force=True, net=a["net"], x=a["x"], y=a["y"], stub=stub), {a["net"]}
    if op == "del_via":
        v = [v for v in b.vias if v.net == a["net"] and geom.dist(v.pos, (a["x"], a["y"])) < 1e-6][0]
        b.vias.remove(v)
        pad = None
        if a.get("stub"):
            for t in list(b.tracks):
                if t.net == a["net"] and not t.locked and (geom.dist(t.b, v.pos) < 1e-6):
                    for pd in b.pads():
                        if pd.net == a["net"] and geom.dist(pd.pos, t.a) < 1e-6:
                            pad = pd.id
                    b.tracks.remove(t)
                    break
        d = {"net": a["net"], "x": a["x"], "y": a["y"]}
        if pad:
            d["pad"] = pad
        return Action("add_via", force=True, **d), {a["net"]}
    if op == "reshape":
        import copy
        if a.get("mode") == "restore":
            cur = [[i, copy.deepcopy(b.zones[i].polys)] for i, _ in a["state"]]
            nets = set()
            for i, polys in a["state"]:
                b.zones[i].polys = copy.deepcopy(polys)
                nets.add(b.zones[i].net)
            return Action("reshape", force=True, net=a["net"], mode="restore", state=cur), nets
        edits, _e, _w = _reshape_plan(world, a["net"], a["mode"], a["box"], a.get("layer"))
        state = [[i, copy.deepcopy(b.zones[i].polys)] for i, _ in edits]
        nets = {a["net"]}
        for i, new in edits:
            b.zones[i].polys = new
            nets.add(b.zones[i].net)
        return Action("reshape", force=True, net=a["net"], mode="restore", state=state), nets
    if op in ("unlock", "lock"):
        return _apply_lock(world, act)
    if op == "trim_lanes":
        return _apply_trim(world)
    if op == "add_tracks":
        return _apply_add_tracks(world, act)
    return _apply_hint(world, act), set()


def _apply_lock(world, act):
    from . import locks
    b, a, op = world.board, act.a, act.op
    want = (op == "lock")                                  # target state of the matching items
    if a.get("ids"):
        ids = set(a["ids"])
        items = [i for i in list(b.tracks) + list(b.vias) if i.id in ids]
    else:
        items, _ = locks.resolve(b, a["target"], getattr(world, "region_boxes", None))
    items = [i for i in items if i.locked != want]
    for i in items:
        i.locked = want
    b.lock_ver = getattr(b, "lock_ver", 0) + 1
    inv = Action("unlock" if want else "lock", force=True, ids=sorted(i.id for i in items))
    return inv, {i.net for i in items if i.net}


def _apply_trim(world):
    from . import locks
    b = world.board
    cands = locks.trim_candidates(b)
    dead = {id(t) for t, _ in cands}
    gone = [t for t in b.tracks if id(t) in dead]
    b.tracks[:] = [t for t in b.tracks if id(t) not in dead]
    b.lock_ver = getattr(b, "lock_ver", 0) + 1
    rec = [{"a": list(t.a), "b": list(t.b), "w": t.w, "layer": t.layer, "net": t.net, "locked": t.locked, "id": t.id, "cls": t.cls} for t in gone]
    world.last_trim = [(t, r) for t, r in cands]
    return Action("add_tracks", force=True, tracks=rec), {t.net for t in gone}


def _apply_add_tracks(world, act):
    from .model import Track
    b = world.board
    new = []
    for d in act.a.get("tracks", []):
        t = Track(tuple(d["a"]), tuple(d["b"]), d["w"], d["layer"], d["net"], d["locked"], d.get("id", ""), d.get("cls", ""))
        b.tracks.append(t)
        new.append(t)
    b.lock_ver = getattr(b, "lock_ver", 0) + 1
    ids = [t.id for t in new]
    return _del_tracks_action(ids), {t.net for t in new}


def _del_tracks_action(ids):
    return Action("trim_lanes", force=True)


def _hint_slot(act):
    a, op = act.a, act.op
    if op == "corridor":
        return "corridor", a["net"], list(a.get("via") or [])
    if op == "side":
        return "side", a["net"], {"of": a["of"], "dir": a["dir"]}
    if op == "reserve":
        return "reserve", a["cut"], list(a.get("nets") or [])
    if op == "layer":
        return "layer", a["target"], a["layer"]
    if op == "freeze":
        return "freeze", a["region"], True
    if op == "near":
        return "near", a["ref"], {"to": a["to"], "max": a["max"]}
    return "priority", a["net"], a.get("level", 1.0)


def _hint_action(op, key, value, clear):
    kw = {"corridor": lambda: dict(net=key, via=value or []), "side": lambda: dict(net=key, **(value or {"of": "", "dir": ""})),
          "reserve": lambda: dict(cut=key, nets=value or []), "layer": lambda: dict(target=key, layer=value or ""),
          "freeze": lambda: dict(region=key), "near": lambda: dict(ref=key, **(value or {"to": "", "max": 0.0})),
          "priority": lambda: dict(net=key, level=value if value is not None else 1.0)}[op]()
    if clear:
        kw["clear"] = True
    return Action(op, force=True, **kw)


def _apply_hint(world, act):
    sec, key, val = _hint_slot(act)
    store = world.hints[sec]
    had = key in store
    old = store.get(key)
    if act.a.get("clear"):
        store.pop(key, None)
    else:
        store[key] = val
    return _hint_action(act.op, key, old, not had)


def describe_hints(world):
    h = world.hints
    out = []
    for n, v in sorted(h["corridor"].items()):
        out.append("corridor %s via %s" % (n, ",".join(v)))
    for n, v in sorted(h["side"].items()):
        out.append("side %s of %s dir %s" % (n, v["of"], v["dir"]))
    for c, v in sorted(h["reserve"].items()):
        out.append("reserve %s for %s" % (c, " ".join(v)))
    for t, v in sorted(h["layer"].items()):
        out.append("layer %s -> %s" % (t, v))
    for r, v in sorted(h["near"].items()):
        out.append("near %s within %g mm of %s" % (r, v["max"], v["to"]))
    for r in sorted(h["freeze"]):
        out.append("freeze %s" % r)
    for n, v in sorted(h["priority"].items(), key=lambda kv: -kv[1]):
        out.append("priority %s = %g" % (n, v))
    return out
