"""Design intent: soft constraints that bias placement, never forbid it.

Declared in badpcb.json ("intent": {...} or a path), edited in a session with `intent add <json>` / `intent rm <id>` /
`intent capture <block>`, scored as the `intent` metric (sum of weighted penalties, lower is better, 0 = every intent met).
Every penalty is `weight x violation` (mm, or intruders); nothing is ever refused, a broken intent just costs and is reported
(`badpcb audit`: INTENT; `badpcb floorplan`).  Entry kinds (config.py normalises them):

  near   A B[.PAD] MM     gap between the courtyard box of each part of A and B's box (or pad point) <= MM; cost = excess mm
  apart  A B MM           gap >= MM; cost = shortfall mm per pair
  edge   REF SIDE [MAX]   courtyard within MAX mm of the board edge (front = +Y/bottom, back = top, left, right, any)
  clear  ZONE except[]    no part (but the excepted ones and the zone's own IC) inside ZONE = "escape:U1[:N]" or a box
  block  NAME parts[]     parts that belong together: max_span_mm (diagonal of the part centres) and, with `ref`, the drift from
                          a reference arrangement (reference .kicad_pcb + anchor, or captured from the board)

Selectors: refdes, glob (C1*, U1??), or a role `decaps:U1` (supply-to-ground caps <= 1.5 uF whose nearest IC pin on their supply net is
on U1), `bulk:U1` (the same above 1.5 uF), `caps:U1` (any cap on a non-ground net whose nearest IC pin is on U1), `passives:U1` (any
<=3-pad R/C/L/FB/D/F/TP part likewise).  Roles are resolved from the netlist and the placement at compile time (when the intent set changes).

Everything here is a pure function of the part poses: `penalty(world)` after an incremental engine refresh equals a fresh
recompute (tests check it), so deltas in evaluate/suggest/audit are consistent.  Cost: O(entries x members).
"""
import fnmatch
import json
import math
import re
from collections import defaultdict

from . import geom

REF_TOL_MM = 1.0           # drift below this (mm RMS) is free
ROT_MISS_MM = 0.25         # each member with the wrong orientation (mod 180) counts as this many mm of drift
SPAN_W = 0.5               # block span: weight x this per mm over max_span_mm
DRIFT_W = 0.3              # reference drift: weight x this per mm over REF_TOL_MM
CLEAR_AREA_CAP = 25.0      # mm2 of overlap after which an intruder stops costing more
GND_RX = re.compile(r"^(A|D|P|S)?GND\w*$|^VSS\w*$", re.I)
SUPPLY_RX = re.compile(r"^[+-]?\d+V\d*\w*$|VDD|VCC|VREG|VBUS|VBAT|VIN|^\+\d", re.I)
PASSIVE_RX = re.compile(r"^(R|C|L|FB|D|F|TP)\d")
BULK_FARADS = 1.5e-6       # supply caps above this are "bulk", not "decaps" (bypass)
ROLE_FORMS = ("decaps", "bulk", "caps", "passives")


# ----------------------------------------------------------------------------- small geometry
def gap(a, b):
    """Distance between two boxes (x0, y0, x1, y1); 0 when they touch or overlap."""
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    return math.hypot(dx, dy)


def overlap_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _rot_miss(a, b):
    """Orientation differs by about 90 degrees (mod 180: a 180-degree turned passive is the same footprint)."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d) > 45.0


# ----------------------------------------------------------------------------- selectors and roles
class _Nets:
    """Netlist views for role resolution (built once per compile)."""

    def __init__(self, board, planes=()):
        self.b = board
        self.pads = defaultdict(list)
        cnt = defaultdict(int)
        for d in board.pads():
            if d.net:
                self.pads[d.net].append(d)
                cnt[d.net] += 1
        self.planes = set(board.plane_nets()) | set(planes)
        if not self.planes:
            self.planes = {n for n, k in cnt.items() if k >= 25}

    def gnd(self, n):
        return bool(n) and (bool(GND_RX.match(n)) or (n in self.planes and n.upper().startswith("GND")))

    def supply(self, n):
        return bool(n) and not self.gnd(n) and (n in self.planes or bool(SUPPLY_RX.search(n)))

    def is_ic(self, p):
        return len(p.pads) > 3 and not PASSIVE_RX.match(p.ref) and not p.ref.startswith("SW") and len({d.num for d in p.pads if d.net}) >= 6

    def ic_pads(self, net, exclude):
        return [d for d in self.pads.get(net, ()) if d.ref != exclude and self.is_ic(self.b.parts[d.ref])]


def resolve(board, sel, nets=None):
    """Selector -> sorted refdes list (unknown names resolve to nothing)."""
    sel = sel.strip()
    if ":" in sel and sel.split(":", 1)[0] in ROLE_FORMS:
        role, anchor = sel.split(":", 1)
        return _role_members(board, role, anchor, nets or _Nets(board))
    if any(ch in sel for ch in "*?["):
        return sorted((r for r in board.parts if fnmatch.fnmatchcase(r, sel)), key=_nat)
    return [sel] if sel in board.parts else []


def _nat(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _farads(v):
    m = re.match(r"^\s*([0-9.]+)\s*([pnuµμm]?)", v or "")
    return float(m.group(1)) * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0}[m.group(2)] if m else None


def _role_members(board, role, anchor, N):
    out = []
    if anchor not in board.parts:
        return out
    for ref, p in board.parts.items():
        if ref == anchor or len(p.pads) > 3 or not PASSIVE_RX.match(ref):
            continue
        if role in ("decaps", "bulk", "caps") and not ref.startswith("C"):
            continue
        pads = [d for d in p.pads if d.net]
        if role in ("decaps", "bulk"):
            f = _farads(p.value)
            if not any(N.gnd(d.net) for d in pads) or (f is not None and (f > BULK_FARADS) != (role == "bulk")):
                continue
        for d in pads:
            if N.gnd(d.net) or (role in ("decaps", "bulk") and not N.supply(d.net)):
                continue
            ics = N.ic_pads(d.net, ref)
            if ics and min(ics, key=lambda x: geom.dist(x.pos, d.pos)).ref == anchor:
                out.append(ref)
                break
    return sorted(out, key=_nat)


def _pad_point(board, spec):
    """'U1' -> (part, None); 'U1.5' -> (part, pad number) when U1 has such a pad; (None, None) when unknown."""
    if spec in board.parts:
        return spec, None
    if "." in spec:
        r, n = spec.rsplit(".", 1)
        if r in board.parts and any(d.num == n for d in board.parts[r].pads):
            return r, n
    return None, None


# ----------------------------------------------------------------------------- compile
class Compiled:
    def __init__(self):
        self.items = []             # [dict]: id, kind, e, w, + resolved fields
        self.warnings = []
        self.esc = {}               # (ic, pose) -> {side: rect}
        self.members = defaultdict(list)   # ref -> [item index] for entries that can move it (near/apart/edge/clear/block)


def _pads_by_num(part):
    out = {}
    for d in part.pads:
        if d.num and d.num not in out:
            out[d.num] = (d.pos[0], d.pos[1])
    return out


def fit(P, Q):
    """Best rigid alignment (rotation + translation, mirror allowed) of the anchor's pads in the reference (P: {num: (x, y)}) onto the
    board's (Q) by pad number. -> {"mirror", "th" (rad), "c", "s", "tx", "ty", "rms", "n"} or None with fewer than 2 common pads."""
    keys = sorted(set(P) & set(Q))
    if len(keys) < 2:
        return None
    best = None
    for mirror in (False, True):
        ps = [(-P[k][0] if mirror else P[k][0], P[k][1]) for k in keys]
        qs = [Q[k] for k in keys]
        n = len(keys)
        pcx, pcy = sum(p[0] for p in ps) / n, sum(p[1] for p in ps) / n
        qcx, qcy = sum(q[0] for q in qs) / n, sum(q[1] for q in qs) / n
        sxx = sum((p[0] - pcx) * (q[0] - qcx) + (p[1] - pcy) * (q[1] - qcy) for p, q in zip(ps, qs))
        sxy = sum((p[0] - pcx) * (q[1] - qcy) - (p[1] - pcy) * (q[0] - qcx) for p, q in zip(ps, qs))
        th = math.atan2(sxy, sxx)
        c, s_ = math.cos(th), math.sin(th)
        tx, ty = qcx - (c * pcx - s_ * pcy), qcy - (s_ * pcx + c * pcy)
        rms = math.sqrt(sum((c * p[0] - s_ * p[1] + tx - q[0]) ** 2 + (s_ * p[0] + c * p[1] + ty - q[1]) ** 2 for p, q in zip(ps, qs)) / n)
        if best is None or rms < best["rms"] - 1e-6:
            best = {"mirror": mirror, "th": th, "c": c, "s": s_, "tx": tx, "ty": ty, "rms": rms, "n": n}
    return best


def place(T, x, y, rot):
    """Reference (x, y, rot) -> where the same part belongs on this board under alignment T."""
    px = -x if T["mirror"] else x
    return (T["c"] * px - T["s"] * y + T["tx"], T["s"] * px + T["c"] * y + T["ty"]), (-rot if T["mirror"] else rot) - math.degrees(T["th"])


def _ref_positions(e, board_getter, anchor, members):
    """Reference arrangement of block `e`: {"pads": {num: (x, y)} of the anchor, "pos": {part: (x, y, rot)}} from the captured `ref.pos`
    or from the reference pcb (members mapped by ref.map, else by refdes). -> (refpos | None, warning | None)."""
    r = e.get("ref")
    if not r:
        return None, None
    if r.get("pos"):
        return {"pads": {k: tuple(v) for k, v in (r.get("pads") or {}).items()}, "pos": {k: tuple(v) for k, v in r["pos"].items()}}, None
    try:
        rb = board_getter(r["pcb"])
    except BaseException as ex:                   # KiCad python missing, file gone: the intent still works without drift
        return None, "block %s: reference %s unreadable (%s)" % (e["name"], r["pcb"], str(ex)[:80])
    mp = r.get("map") or {}
    ra = mp.get(anchor, anchor)
    if ra not in rb.parts:
        return None, "block %s: anchor %s (as %s) is not in the reference board" % (e["name"], anchor, ra)
    pos = {}
    for m in members:
        q = rb.parts.get(mp.get(m, m))
        if q is not None and m != anchor:
            pos[m] = (q.pos[0], q.pos[1], q.rot)
    miss = [m for m in members if m != anchor and m not in pos]
    return {"pads": _pads_by_num(rb.parts[ra]), "pos": pos}, (
        "block %s: %d member(s) not in the reference (%s%s); list them in ref.map" % (e["name"], len(miss), " ".join(miss[:6]), " ..." if len(miss) > 6 else "") if miss else None)


def compile_intent(world, board_getter=None):
    b = world.board
    N = _Nets(b)
    C = Compiled()
    cache = {}

    def res(sels):
        out = []
        for s in sels:
            if s not in cache:
                cache[s] = resolve(b, s, N)
                if not cache[s]:
                    C.warnings.append("selector '%s' matches no part" % s)
            out += [r for r in cache[s] if r not in out]
        return out

    getter = board_getter or _default_getter
    for iid, e in sorted(world.intent.items(), key=lambda kv: _nat(kv[0])):
        k = e["kind"]
        it = {"id": iid, "kind": k, "e": e, "w": e["weight"]}
        if k == "near":
            it["A"] = res(e["a"])
            r, pad = _pad_point(b, e["b"])
            if r is None:
                C.warnings.append("%s: no part/pad '%s'" % (iid, e["b"]))
            it["B"], it["pad"] = r, pad
        elif k == "apart":
            it["A"], it["B"] = res(e["a"]), res(e["b"])
        elif k == "edge":
            it["A"] = res(e["ref"])
        elif k == "clear":
            it["except"] = set(res(e["except"])) if e["except"] else set()
            z = e["zone"]
            if isinstance(z, str):
                parts = z.split(":")
                it["ic"], it["side"] = parts[1], (parts[2] if len(parts) > 2 else None)
                if it["ic"] not in b.parts:
                    C.warnings.append("%s: no part '%s' for zone %s" % (iid, it["ic"], z))
            else:
                it["ic"], it["rect"] = None, tuple(z)
        elif k == "block":
            it["A"] = res(e["parts"])
            anchor = (e.get("ref") or {}).get("anchor") or e.get("anchor")
            if not anchor and it["A"]:
                anchor = max(it["A"], key=lambda r: b.parts[r].area)
            if anchor and anchor not in b.parts:
                C.warnings.append("block %s: anchor %s is not on the board" % (e["name"], anchor))
                anchor = None
            it["anchor"] = anchor
            it["refpos"] = None
            if anchor and e.get("ref"):
                it["refpos"], w = _ref_positions(e, getter, anchor, it["A"])
                if w:
                    C.warnings.append(w)
        idx = len(C.items)
        C.items.append(it)
        refs = set(it.get("A") or [])
        B = it.get("B")
        refs |= set(B) if isinstance(B, list) else ({B} if B else set())
        for r in refs:
            C.members[r].append(idx)
    return C


def _default_getter(path):
    from . import cli
    return cli.load_board(path)


def compiled(world):
    """The compiled intent of a world (cached until the intent set changes)."""
    c = getattr(world, "_intent_c", None)
    if c is None or c[0] != world.intent_ver:
        world._intent_c = c = (world.intent_ver, compile_intent(world))
    return c[1]


# ----------------------------------------------------------------------------- scoring
def _escape_rects(world, C, ic, side):
    from . import audit
    p = world.board.parts.get(ic)
    if p is None:
        return []
    key = (ic, p.pos, p.rot, p.side)
    z = C.esc.get(key)
    if z is None:
        class _Sh:
            b = world.board
            layer_of = staticmethod(lambda r: "B.Cu" if world.board.parts[r].side == "B" else "F.Cu")
        z = {x.side: x.rect for x in audit.escape_zones(_Sh, {ic}, any_pitch=True)}
        if len(C.esc) > 64:
            C.esc.clear()
        C.esc[key] = z
    return [r for s, r in sorted(z.items()) if side is None or s == side]


def zone_rects(world, it, C=None):
    C = C or compiled(world)
    if it["ic"] is None:
        return [it["rect"]]
    return _escape_rects(world, C, it["ic"], it["side"])


def _edge_dist(board, bb, side):
    x0, y0, x1, y1 = board.bbox
    d = {"front": y1 - bb[3], "back": bb[1] - y0, "left": bb[0] - x0, "right": x1 - bb[2]}
    return min(d.values()) if side == "any" else d[side]


def eval_item(world, C, it, bbf=None, only=None):
    """-> (penalty, [violation dict]) of one compiled entry. `bbf(ref)` overrides the courtyard box of a part (joint planning);
    `only` restricts to violations of that part."""
    b = world.board
    bbf = bbf or (lambda r: b.parts[r].bbox)
    k, w = it["kind"], it["w"]
    pen, viol = 0.0, []
    if k == "near":
        B = it["B"]
        if B is None:
            return 0.0, []
        if it["pad"] is not None:
            pts = [d.pos for d in b.parts[B].pads if d.num == it["pad"]]
        for a in it["A"]:
            if a == B or (only and a != only and B != only):
                continue
            bb = bbf(a)
            if it["pad"] is not None:
                g = min(gap(bb, (q[0], q[1], q[0], q[1])) for q in pts)
            else:
                g = gap(bb, bbf(B))
            ex = g - it["e"]["mm"]
            if ex > 1e-9:
                pen += w * ex
                viol.append({"part": a, "other": it["e"]["b"], "amount": ex, "have": g, "limit": it["e"]["mm"], "pen": w * ex})
    elif k == "apart":
        for a in it["A"]:
            for c in it["B"]:
                if a == c or (only and a != only and c != only):
                    continue
                g = gap(bbf(a), bbf(c))
                sh = it["e"]["mm"] - g
                if sh > 1e-9:
                    pen += w * sh
                    viol.append({"part": a, "other": c, "amount": sh, "have": g, "limit": it["e"]["mm"], "pen": w * sh})
    elif k == "edge":
        for a in it["A"]:
            if only and a != only:
                continue
            d = _edge_dist(b, bbf(a), it["e"]["side"])
            ex = max(0.0, d) - it["e"]["max_mm"]
            if ex > 1e-9:
                pen += w * ex
                viol.append({"part": a, "amount": ex, "have": d, "limit": it["e"]["max_mm"], "pen": w * ex})
    elif k == "clear":
        rects = zone_rects(world, it, C)
        if not rects:
            return 0.0, []
        ex = it["except"]
        ux = (min(z[0] for z in rects), min(z[1] for z in rects), max(z[2] for z in rects), max(z[3] for z in rects))
        for r in (b.parts if not only else [only]):
            if r in ex or r == it["ic"] or r not in b.parts:
                continue
            bb = bbf(r)
            if bb[2] <= ux[0] or bb[0] >= ux[2] or bb[3] <= ux[1] or bb[1] >= ux[3]:
                continue
            area = sum(overlap_area(bb, z) for z in rects)
            if area > 0.05:
                p_ = w * (1.0 + min(area, CLEAR_AREA_CAP) / 5.0)
                pen += p_
                viol.append({"part": r, "amount": area, "pen": p_})
    elif k == "block":
        A = it["A"]
        mx = it["e"].get("max_span_mm")
        if mx is not None and len(A) > 1 and not only:
            cs = [b.parts[r].pos for r in A]
            xs, ys = [c[0] for c in cs], [c[1] for c in cs]
            span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
            if span - mx > 1e-9:
                p_ = w * SPAN_W * (span - mx)
                pen += p_
                viol.append({"part": None, "kind": "span", "amount": span - mx, "have": span, "limit": mx, "pen": p_})
        if it.get("refpos") and it["anchor"] and not only:
            dr = drift(world, it)
            if dr and dr["cost"] > REF_TOL_MM:
                p_ = w * DRIFT_W * (dr["cost"] - REF_TOL_MM)
                pen += p_
                viol.append({"part": None, "kind": "drift", "amount": dr["cost"] - REF_TOL_MM, "have": dr["rms"], "pen": p_, "drift": dr})
    return pen, viol


def drift(world, it):
    """Reference drift of a block after aligning the anchor's pads: {"rms": mm, "rot": n wrong orientations, "cost": mm, "fit": rms of
    the anchor alignment, "parts": {ref: (offset mm, expected (x, y), expected rot, orientation wrong)}}, or None."""
    rp, anchor = it.get("refpos"), it.get("anchor")
    b = world.board
    if not rp or not anchor or anchor not in b.parts:
        return None
    T = fit(rp["pads"], _pads_by_num(b.parts[anchor]))
    if T is None:
        return None
    parts, ss, nrot = {}, 0.0, 0
    for r, (x, y, rot) in rp["pos"].items():
        p = b.parts.get(r)
        if p is None or r == anchor:
            continue
        pos, erot = place(T, x, y, rot)
        off = geom.dist(p.pos, pos)
        miss = _rot_miss(p.rot, erot)
        ss += off * off
        nrot += miss
        parts[r] = (off, pos, erot, miss)
    n = len(parts)
    if not n:
        return None
    rms = math.sqrt(ss / n)
    return {"rms": rms, "rot": nrot, "n": n, "cost": rms + ROT_MISS_MM * nrot, "fit": T["rms"], "mirror": T["mirror"], "parts": parts}


def penalty(world):
    """(total intent penalty, number of entries). (0.0, 0) without intent."""
    if not world.intent:
        return 0.0, 0
    C = compiled(world)
    return sum(eval_item(world, C, it)[0] for it in C.items), len(C.items)


def report(world):
    """[(item, penalty, [violations])] in id order, for audit / floorplan / `show`."""
    C = compiled(world)
    out = []
    for it in C.items:
        p, v = eval_item(world, C, it)
        out.append((it, p, v))
    return out


def part_cost(world, ref, bbox):
    """Penalty of the entries that involve `ref` (as mover) with its courtyard at `bbox`; others stay where they are. Joint planning."""
    if not world.intent:
        return 0.0
    C = compiled(world)
    b = world.board
    bbf = lambda r: bbox if r == ref else b.parts[r].bbox
    tot = 0.0
    for i in C.members.get(ref, ()):
        it = C.items[i]
        if it["kind"] != "block":
            tot += eval_item(world, C, it, bbf, only=ref)[0]
    for it in C.items:
        if it["kind"] == "clear" and ref not in it["except"]:
            tot += eval_item(world, C, it, bbf, only=ref)[0]
    return tot


def capture(world, name):
    """Snapshot the current arrangement of block `name` as its reference: returns the new block entry (not stored)."""
    e = world.intent.get("block:" + name) or world.intent.get(name)
    if e is None or e["kind"] != "block":
        raise KeyError("no block '%s'" % name)
    C = compiled(world)
    it = [x for x in C.items if x["id"] == e["id"]][0]
    mem = [r for r in it["A"] if r != it["anchor"]]
    if not mem or not it["anchor"]:
        raise ValueError("block %s needs an anchor and at least one other part to capture" % name)
    b = world.board
    new = dict(e)
    new["ref"] = {"anchor": it["anchor"],
                  "pads": {k: [round(v[0], 3), round(v[1], 3)] for k, v in _pads_by_num(b.parts[it["anchor"]]).items()},
                  "pos": {r: [round(b.parts[r].pos[0], 3), round(b.parts[r].pos[1], 3), round(b.parts[r].rot, 3)] for r in mem}}
    return new


def describe(world):
    """One line per entry for `badpcb show` / `badpcb project`."""
    from . import config
    out = []
    for iid, e in sorted(world.intent.items(), key=lambda kv: _nat(kv[0])):
        o = config.intent_to_obj(e)
        k = e["kind"]
        out.append("%-12s %s" % (iid, json.dumps(o[k], separators=(",", ":"))[:160]))
    return out
