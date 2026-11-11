"""badpcb floorplan: the step-2 view (blocks before parts). Works on a board with only rough placement.

    badpcb floorplan <board.kicad_pcb | session> [--json] [--depth 8]

Blocks: the ones declared in the intent (badpcb.json "intent".blocks) first, then automatic clustering of the rest:
  * connectors (J*, P*, CN*, USB*) are blocks of their own;
  * big regular arrays (>= 12 identical footprints on a regular lattice, e.g. 58 key cells) are ONE block, arrays that sit on top of
    each other (switch + sensor + cap of every cell) are merged;
  * every IC (and fine-pitch part) seeds a block; passives, crystals, test points, small ESD parts follow the net they share with
    a seed (signal nets first, a decap goes to the IC whose supply pin is nearest), chains (series R -> cap) follow their neighbour;
  * what is left is grouped by shared nets (misc).
Report (kept under ~50 lines): ASCII map, per block bbox / area / parts / fill (courtyard area / bbox area), block-to-block net counts
and the crossing count of the block-level ratsnest, connectors and their edge distance, what faces each fine-pitch IC side (strict fan
zone + an `--depth` mm corridor), long critical links (crystal, flash, other ICs to a fine-pitch IC) and regulator arrangements.
Declared intent that is violated is shown next to its block (span, reference drift) and summed as `intent=`.
"""
import json
import math
import re
import sys
from collections import defaultdict

from . import geom
from . import intent as IN

ARRAY_MIN = 12              # identical footprints needed for an array
ARRAY_CV = 0.35             # nearest-neighbour distance spread (std / mean) allowed for "regular"
ARRAY_NEAR_MM = 25.0        # arrays merge when their members sit this close
ATTACH_FAR_MM = 40.0        # a leftover part joins the nearest block only inside this distance
CONN_RX = re.compile(r"^(J|P|CN|USB|X)\d")
CONN_FP_RX = re.compile(r"Connector|USB|JST|Header|PinHeader|Barrel|Socket", re.I)
FLASH_RX = re.compile(r"W25[QX]|IS25|MX25|GD25|AT25|FLASH|QSPI", re.I)
REG_RX = re.compile(r"LDO|REG|TPS\d|AP\d{3}|LM\d{3,4}|NCP\d|MIC\d|AMS1117|XC6|RT9|MCP17|LD\d|MIC5|TLV7", re.I)
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
LONG_DEFAULT_MM = 20.0
LONG_FLASH_MM = 15.0
LONG_XTAL_MM = 8.0
REG_CAP_MM = 4.0


class Block:
    def __init__(self, name, kind, parts, declared=False):
        self.name, self.kind, self.parts, self.declared = name, kind, list(parts), declared
        self.letter = "?"
        self.note = ""

    def bbox(self, b):
        bbs = [b.parts[r].bbox for r in self.parts]
        return (min(x[0] for x in bbs), min(x[1] for x in bbs), max(x[2] for x in bbs), max(x[3] for x in bbs))


def _nat(s):
    return IN._nat(s)


def _is_conn(p):
    return bool(CONN_RX.match(p.ref)) or (bool(CONN_FP_RX.search(p.fpid)) and not p.ref.startswith(("R", "C", "L", "D", "TP", "H")))


def _arrays(b, free):
    """[[refs]] of regular arrays among the `free` refs (same ref prefix + footprint, many, evenly spaced), merged when co-located."""
    grp = defaultdict(list)
    for r in free:
        p = b.parts[r]
        grp[(re.sub(r"\d+$", "", r), p.fpid)].append(r)
    arrs = []
    for (pre, fp), refs in grp.items():
        if len(refs) < ARRAY_MIN:
            continue
        pts = [b.parts[r].pos for r in refs]
        nn = [min(geom.dist(a, c) for j, c in enumerate(pts) if j != i) for i, a in enumerate(pts)]
        m = sum(nn) / len(nn)
        sd = math.sqrt(sum((x - m) ** 2 for x in nn) / len(nn))
        if m > 0 and sd / m <= ARRAY_CV:
            arrs.append(sorted(refs, key=_nat))
    merged = []
    for a in sorted(arrs, key=lambda x: -len(x)):
        for m_ in merged:
            near = sum(1 for r in a if min(geom.dist(b.parts[r].pos, b.parts[q].pos) for q in m_) <= ARRAY_NEAR_MM)
            if near >= 0.8 * len(a):
                m_.extend(a)
                break
        else:
            merged.append(list(a))
    return merged


def build(world, fine=None, planes=()):
    """-> dict with everything the text/JSON views need. `world` = actions.World (board + intent)."""
    b = world.board
    N = IN._Nets(b, planes)
    fine = list(fine) if fine is not None else []
    C = IN.compiled(world) if world.intent else None
    blocks, owner = [], {}

    def add(blk):
        for r in blk.parts:
            owner[r] = len(blocks)
        blocks.append(blk)

    # --- declared
    if C:
        for it in C.items:
            if it["kind"] != "block":
                continue
            mem = [r for r in it["A"] if r not in owner and r in b.parts]
            if mem:
                blk = Block(it["e"]["name"], "declared", mem, True)
                blk.item = it
                add(blk)
    pool = [r for r, p in b.parts.items() if r not in owner and not (r.startswith("H") and not p.pads) and "MountingHole" not in p.fpid]
    # --- connectors, arrays, IC seeds
    for r in [r for r in pool if _is_conn(b.parts[r])]:
        add(Block(r, "conn", [r]))
    pool = [r for r in pool if r not in owner]
    for a in _arrays(b, pool):
        a = [r for r in a if r not in owner]
        if len(a) >= ARRAY_MIN:
            pre = sorted({re.sub(r"\d+$", "", r) for r in a})
            cells = max(sum(1 for r in a if r.startswith(x)) for x in pre)
            key = max(a, key=lambda r: (len(b.parts[r].pads), b.parts[r].area))           # the busiest part names the cell
            blk = Block("%sx%d" % ((b.parts[key].value or re.sub(r"\d+$", "", key))[:8], cells), "array", a)
            blk.note = "array of %d cells (%s)" % (cells, "+".join(pre))
            add(blk)
    pool = [r for r in pool if r not in owner]
    for r in sorted(pool, key=_nat):
        p = b.parts[r]
        if r in fine or (N.is_ic(p) and not r.startswith("Y")):
            add(Block(r, "ic", [r]))
    pool = [r for r in pool if r not in owner]

    # --- attach the rest to the net-wise nearest block
    def sig(n):
        return bool(n) and not N.gnd(n) and not N.supply(n)

    def score(r):
        sc = defaultdict(float)
        for d in b.parts[r].pads:
            if not d.net or N.gnd(d.net):
                continue
            if not sig(d.net):
                ics = N.ic_pads(d.net, r)
                if ics:
                    q = min(ics, key=lambda x: geom.dist(x.pos, d.pos))
                    if q.ref in owner:
                        sc[owner[q.ref]] += 0.6
                continue
            seen = set()
            for q in N.pads[d.net]:
                if q.ref != r and q.ref in owner and owner[q.ref] not in seen and len(N.pads[d.net]) <= 12:
                    seen.add(owner[q.ref])
                    sc[owner[q.ref]] += 0.8 if blocks[owner[q.ref]].kind == "conn" else 1.0
        return sc

    for _ in range(5):
        moved = []
        for r in pool:
            sc = score(r)
            if sc:
                p = b.parts[r]
                best = max(sc, key=lambda i: (sc[i], -min(geom.dist(p.pos, b.parts[q].pos) for q in blocks[i].parts)))
                moved.append((r, best))
        if not moved:
            break
        for r, i in moved:                      # assign after scoring the round, so chains resolve over rounds
            blocks[i].parts.append(r)
            owner[r] = i
        pool = [r for r in pool if r not in owner]
    left = []
    for r in pool:                              # no shared net with anything: nearest block if close, else group by nets
        p = b.parts[r]
        cand = [(min(geom.dist(p.pos, b.parts[q].pos) for q in blk.parts), i) for i, blk in enumerate(blocks) if blk.kind in ("ic", "declared")]
        if cand and min(cand)[0] <= ATTACH_FAR_MM and not N.is_ic(p) and p.pads and len(p.pads) <= 3:
            i = min(cand)[1]
            blocks[i].parts.append(r)
            owner[r] = i
        else:
            left.append(r)
    uf = {r: r for r in left}

    def find(x):
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    bynet = defaultdict(list)
    for r in left:
        for d in b.parts[r].pads:
            if sig(d.net):
                bynet[d.net].append(r)
    for refs in bynet.values():
        for q in refs[1:]:
            uf[find(q)] = find(refs[0])
    cl = defaultdict(list)
    for r in left:
        cl[find(r)].append(r)
    for k, refs in enumerate(sorted(cl.values(), key=lambda v: _nat(v[0])), 1):
        add(Block("misc%d" % k if len(refs) > 1 else refs[0], "misc", sorted(refs, key=_nat)))
    for blk in blocks:
        blk.parts = sorted(set(blk.parts), key=_nat)
    return {"board": b, "blocks": blocks, "owner": owner, "N": N, "C": C, "fine": fine}


# ----------------------------------------------------------------------------- analysis
def _metrics(b, blk):
    bb = blk.bbox(b)
    area = max(0.0, (bb[2] - bb[0]) * (bb[3] - bb[1]))
    comp = sum(b.parts[r].area for r in blk.parts)
    cs = [b.parts[r].pos for r in blk.parts]
    span = math.hypot(max(c[0] for c in cs) - min(c[0] for c in cs), max(c[1] for c in cs) - min(c[1] for c in cs))
    return {"bbox": [round(v, 1) for v in bb], "area": round(area, 1), "parts": len(blk.parts), "fill": round(comp / area, 2) if area > 0 else 0.0,
            "span": round(span, 1)}


def _net_links(F):
    """Block-level ratsnest: {net: [(i, j, pa, pb)]} MST edges over the blocks a signal net touches (nodes = centroid of the net's pads there)."""
    b, owner, N = F["board"], F["owner"], F["N"]
    edges = {}
    for net, pads in N.pads.items():
        if N.gnd(net) or N.supply(net):
            continue
        by = defaultdict(list)
        for d in pads:
            if d.ref in owner:
                by[owner[d.ref]].append(d.pos)
        if len(by) < 2:
            continue
        ids = sorted(by)
        pts = [(sum(p[0] for p in by[i]) / len(by[i]), sum(p[1] for p in by[i]) / len(by[i])) for i in ids]
        edges[net] = [(ids[i], ids[j], pts[i], pts[j]) for i, j in geom.mst(len(ids), lambda i, j: geom.dist(pts[i], pts[j]))]
    return edges


def _crossings(edges):
    segs = [(net, e[2], e[3]) for net, L in edges.items() for e in L]
    n = 0
    for i in range(len(segs)):
        a = segs[i]
        for j in range(i + 1, len(segs)):
            c = segs[j]
            if a[0] != c[0] and geom.seg_intersect(a[1], a[2], c[1], c[2]):
                n += 1
    return n


def _edge_side(b, bb):
    x0, y0, x1, y1 = b.bbox
    d = {"front": y1 - bb[3], "back": bb[1] - y0, "left": bb[0] - x0, "right": x1 - bb[2]}
    s = min(d, key=d.get)
    return s, d[s]


def _escape(F, depth):
    """For every escape side of the fine-pitch ICs: what sits in the strict fan zone and in the corridor out to `depth` mm."""
    from . import audit
    b, owner, blocks = F["board"], F["owner"], F["blocks"]
    rows = []

    class Sh:
        pass
    sh = Sh()
    sh.b = b
    sh.layer_of = lambda r: "B.Cu" if b.parts[r].side == "B" else "F.Cu"
    for ic in F["fine"]:
        if ic not in b.parts:
            continue
        for z in sorted(audit.escape_zones(sh, {ic}, any_pitch=True), key=lambda z: z.side):
            r0 = z.rect                                              # strict zone (pad row .. D)
            ax = z.ax
            ext = z.D if depth <= z.D else depth
            lo, hi = (z.a_n, z.a_n + z.sgn * ext)
            lo, hi = min(lo, hi), max(lo, hi)
            cor = (z.t0, lo, z.t1, hi) if ax == 0 else (lo, z.t0, hi, z.t1)
            fan, near = defaultdict(float), {}
            for r, p in b.parts.items():
                if r == ic or not p.pads:
                    continue
                a_fan = IN.overlap_area(p.bbox, r0)
                a_cor = IN.overlap_area(p.bbox, cor)
                if a_cor <= 0.05 and a_fan <= 0.05:
                    continue
                i = owner.get(r)
                blk = blocks[i].name if i is not None else "-"
                if a_fan > 0.05:
                    fan[blk] += a_fan
                d = abs(p.pos[1 - ax] - z.line)
                near[blk] = min(near.get(blk, 1e9), d)
            rows.append({"side": z.name, "pads": z.n_pads, "D": round(z.D, 1), "rect": [round(v, 1) for v in r0], "corridor": [round(v, 1) for v in cor],
                         "fan": {k: round(v, 1) for k, v in sorted(fan.items(), key=lambda kv: -kv[1])},
                         "corridor_blocks": {k: round(v, 1) for k, v in sorted(near.items(), key=lambda kv: kv[1])}, "ic": ic})
    return rows


def _links(F):
    """Critical links: for every block that is not an array/connector, its signal nets to a fine-pitch IC in another block (pad to pad)."""
    b, owner, blocks, N = F["board"], F["owner"], F["blocks"], F["N"]
    from . import audit
    out = []
    for i, blk in enumerate(blocks):
        if blk.kind in ("array", "conn"):
            continue
        for ic in F["fine"]:
            if ic not in owner or owner[ic] == i or ic not in b.parts:
                continue
            ds, nets = [], []
            for net, pads in N.pads.items():
                if N.gnd(net) or N.supply(net):
                    continue
                mine = [d for d in pads if owner.get(d.ref) == i]
                host = [d for d in pads if d.ref == ic]
                if mine and host:
                    ds.append(min(geom.dist(m.pos, h.pos) for m in mine for h in host))
                    nets.append(net)
            if not ds:
                continue
            xt = any(r.startswith("Y") or "Crystal" in b.parts[r].fpid for r in blk.parts)
            fl = any(FLASH_RX.search(b.parts[r].value or "") for r in blk.parts) or any(FLASH_RX.search(n) for n in nets)
            lim = LONG_XTAL_MM if xt else LONG_FLASH_MM if fl else LONG_DEFAULT_MM
            kind = "crystal" if xt else "flash" if fl else "block"
            out.append({"block": blk.name, "ic": ic, "kind": kind, "nets": len(ds), "min": round(min(ds), 1), "max": round(max(ds), 1),
                        "mean": round(sum(ds) / len(ds), 1), "limit": lim, "long": max(ds) > lim, "nets_list": nets[:6]})
    out.sort(key=lambda r: (not r["long"], -r["max"]))
    return out


REG_BLOCK_RX = re.compile(r"vreg|regulat|dcdc|buck|ldo|smps|psu|power", re.I)       # intent block names that declare a regulator loop
REG_LOOP_NET_RX = re.compile(r"(^|_)(LX|SW|VREG|FB|BOOT|BST)|FB$", re.I)             # switch node / feedback / regulator-internal nets


def _declared_loop(F, r):
    """Parts of the declared regulator block (intent `blocks`, name like vreg/regulator/dcdc...) that contains `r`, else None."""
    for blk in F["blocks"]:
        it = getattr(blk, "item", None)
        if blk.declared and it is not None and REG_BLOCK_RX.search(blk.name):
            members = set(it["A"]) | set(blk.parts)
            if r in members:
                return members, blk.name
    return None


def _regulators(F):
    """Regulator groups: every inductor and every regulator-looking IC with the capacitors of its LOOP and their distance to its pads.

    Loop caps (only these are measured against REG_CAP_MM): caps declared in the regulator's intent block (`vreg`/regulator...), caps on the
    switch-node / feedback / regulator-internal nets (LX, VREG_*, FB), and on each output rail the cap nearest the part (plus further caps within
    REG_CAP_MM). Other caps on the output rail are the load's own decaps (e.g. DVDD pin decaps the reference puts anywhere around the IC): they are
    not loop caps, and are listed as `rail_decaps` (not counted, not flagged)."""
    b, N = F["board"], F["N"]
    out = []
    for r, p in sorted(b.parts.items(), key=lambda kv: _nat(kv[0])):
        ind = r.startswith("L") and not r.startswith("LED") and len(p.pads) <= 3
        ic = REG_RX.search(p.value or "") and N.is_ic(p) or (REG_RX.search(p.value or "") and len(p.pads) >= 3 and r.startswith("U"))
        if not (ind or ic):
            continue
        host = None
        for d in p.pads:
            ics = [q for q in N.pads.get(d.net, ()) if q.ref != r and N.is_ic(b.parts[q.ref])] if d.net and not N.gnd(d.net) else []
            if ics:
                q = min(ics, key=lambda x: geom.dist(x.pos, d.pos))
                host = (q.ref, geom.dist(q.pos, d.pos), d.net)
                break
        decl = _declared_loop(F, r)

        def is_cap(q):
            return q.ref.startswith("C") and q.ref != r and len(b.parts[q.ref].pads) <= 3 and any(N.gnd(x.net) for x in b.parts[q.ref].pads)
        # candidate caps by net: nets of the part's own pads, plus regulator-internal nets of its host IC (VREG_AVDD, VREG_FB ...)
        nets = {d.net: d.pos for d in p.pads if d.net and not N.gnd(d.net)}
        if host:
            for hd in b.parts[host[0]].pads:
                if hd.net and not N.gnd(hd.net) and hd.net not in nets and REG_LOOP_NET_RX.search(hd.net):
                    nets[hd.net] = min((d.pos for d in p.pads), key=lambda x: geom.dist(x, hd.pos))
        caps, rail = {}, {}
        for net, pos0 in nets.items():
            loop_net = bool(REG_LOOP_NET_RX.search(net))
            cs = []
            for q in N.pads.get(net, ()):
                if is_cap(q):
                    ref_pad = min((d.pos for d in p.pads if d.net == net), key=lambda x: geom.dist(x, q.pos)) if any(d.net == net for d in p.pads) else pos0
                    cs.append((geom.dist(q.pos, ref_pad), q.ref))
            cs.sort()
            for k, (dd, ref) in enumerate(cs):
                in_decl = decl is not None and ref in decl[0]
                if in_decl or loop_net or k == 0 or dd <= REG_CAP_MM:
                    if ref not in caps or dd < caps[ref][0]:
                        caps[ref] = (dd, net)
                else:
                    rail.setdefault(net, []).append(ref)
        if decl:                                   # declared loop parts on other nets (input cap, AVDD filter cap): distance to the nearest pad of the part
            for ref in decl[0]:
                if ref in b.parts and ref not in caps and ref != r and ref.startswith("C") and len(b.parts[ref].pads) <= 3:
                    caps[ref] = (min(geom.dist(b.parts[ref].pos, d.pos) for d in p.pads), "declared:" + decl[1])
        cs = sorted(caps.items(), key=lambda kv: kv[1][0])
        pts = [p.pos] + [b.parts[c].pos for c, _ in cs[:6]]
        span = math.hypot(max(x[0] for x in pts) - min(x[0] for x in pts), max(x[1] for x in pts) - min(x[1] for x in pts))
        out.append({"part": r, "value": p.value, "host": host[0] if host else None, "host_pin_mm": round(host[1], 1) if host else None, "host_net": host[2] if host else None,
                    "block": decl[1] if decl else None,
                    "caps": [{"ref": c, "net": n, "mm": round(d, 1), "far": d > REG_CAP_MM} for c, (d, n) in cs[:8]],
                    "rail_decaps": {n: sorted(v, key=_nat) for n, v in rail.items()}, "span": round(span, 1)})
    return out


def N_is_ic(b, N, r, fine):
    return r in fine or N.is_ic(b.parts[r])


def analyse(world, fine=None, depth=8.0, planes=()):
    b = world.board
    F = build(world, fine, planes)
    blocks = F["blocks"]
    order = sorted(range(len(blocks)), key=lambda i: (0 if blocks[i].declared else 1, {"ic": 0, "conn": 1, "array": 2, "misc": 3, "declared": 0}[blocks[i].kind], _nat(blocks[i].name)))
    for k, i in enumerate(order):
        blocks[i].letter = LETTERS[k] if k < len(LETTERS) else "#"
    F["order"] = order
    F["m"] = [_metrics(b, blk) for blk in blocks]
    # declared intent per block: span / drift
    F["intent"], F["ref"] = {}, {}
    if F["C"]:
        for it, pen, viol in IN.report(world):
            if it["kind"] == "block":
                F["intent"][it["e"]["name"]] = {"penalty": round(pen, 1), "viol": viol}
                dr = IN.drift(world, it) if it.get("refpos") else None
                if dr:
                    F["ref"][it["e"]["name"]] = dr
    edges = _net_links(F)
    pair = defaultdict(int)
    for net, L in edges.items():
        for i, j, _, _ in L:
            pair[tuple(sorted((i, j)))] += 1
    F["pairs"] = pair
    F["x"] = _crossings(edges)
    F["conn"] = []
    for i, blk in enumerate(blocks):
        if blk.kind == "conn":
            side, d = _edge_side(b, blk.bbox(b))
            F["conn"].append({"block": blk.name, "side": side, "mm": round(d, 1), "inboard": d > 5.0})
    ics = {i for i, blk in enumerate(blocks) if any(N_is_ic(b, F["N"], r, F["fine"]) for r in blk.parts)}
    F["foreign"] = {}                  # IC -> names of blocks that are NOT its support: connectors, arrays, misc, blocks with another IC
    for ic in F["fine"]:
        if ic in F["owner"]:
            F["foreign"][ic] = {blk.name for i, blk in enumerate(blocks)
                                if i != F["owner"][ic] and (blk.kind in ("conn", "array", "misc") or i in ics)}
    F["esc"] = _escape(F, depth)
    F["links"] = _links(F)
    F["regs"] = _regulators(F)
    F["ipen"] = IN.penalty(world)[0] if world.intent else None
    return F


# ----------------------------------------------------------------------------- text
def _map(F, cols=64):
    b, blocks = F["board"], F["blocks"]
    x0, y0, x1, y1 = b.bbox
    w, h = max(1e-6, x1 - x0), max(1e-6, y1 - y0)
    rows = max(5, min(14, int(round(cols * h / w * 0.5))))
    cw, ch = w / cols, h / rows
    cover = defaultdict(lambda: defaultdict(float))            # cell -> {block index: courtyard area inside the cell}
    for r, p in b.parts.items():
        if r not in F["owner"]:
            continue
        bb = p.bbox
        i = F["owner"][r]
        for yy in range(max(0, int((bb[1] - y0) // ch)), min(rows - 1, int((bb[3] - y0) // ch)) + 1):
            for xx in range(max(0, int((bb[0] - x0) // cw)), min(cols - 1, int((bb[2] - x0) // cw)) + 1):
                a = IN.overlap_area(bb, (x0 + xx * cw, y0 + yy * ch, x0 + (xx + 1) * cw, y0 + (yy + 1) * ch))
                cover[(xx, yy)][i] += a if a > 0 else 1e-3
    grid = [["." for _ in range(cols)] for _ in range(rows)]
    for (xx, yy), d in cover.items():
        best = max(d, key=lambda i: (d[i] / max(1.0, F["m"][i]["area"]) ** 0.25, d[i]))   # damp the biggest blocks so small ones stay visible
        grid[yy][xx] = blocks[best].letter
    return ["  " + "".join(row) for row in grid], cw, ch


def text(F, name="", max_rows=12):
    b, blocks, m = F["board"], F["blocks"], F["m"]
    nd = sum(1 for x in blocks if x.declared)
    L = ["badpcb floorplan %s: %d parts -> %d blocks (%d declared, %d auto), board %.0fx%.0f mm%s" % (
        name, sum(len(x.parts) for x in blocks), len(blocks), nd, len(blocks) - nd, b.width, b.height,
        ", intent=%.1f" % F["ipen"] if F["ipen"] is not None else "")]
    grid, cw, ch = _map(F)
    L.append("map (1 char = %.1f x %.1f mm, top = back edge, letters = blocks):" % (cw, ch))
    L += grid
    L.append("blocks   name         parts  bbox x0,y0..x1,y1 (mm)        area  fill  span  note")
    order = F["order"]
    shown = order[:max_rows]
    for i in shown:
        k = blocks[i]
        mm = m[i]
        val = ""
        if k.kind == "ic" and k.parts:
            val = b.parts[k.parts[0]].value
        note = k.note or val
        ii = F["intent"].get(k.name)
        if k.name in F["ref"] and not (ii and any(v.get("kind") == "drift" for v in ii["viol"])):
            note = (note + " " if note else "") + "ref %.1fmm" % F["ref"][k.name]["rms"]
        if ii and ii["penalty"] > 0.05:
            bits = []
            for v in ii["viol"]:
                if v.get("kind") == "span":
                    bits.append("span %.0f>%g" % (v["have"], v["limit"]))
                elif v.get("kind") == "drift":
                    bits.append("drift %.1fmm%s" % (v["drift"]["rms"], "+%drot" % v["drift"]["rot"] if v["drift"]["rot"] else ""))
            note = (note + " " if note else "") + "!" + ",".join(bits)
        L.append(" %s %-12s %-11s %4d  %5.0f,%-4.0f..%5.0f,%-5.0f %7.0f %5.2f %5.1f  %s" % (
            k.letter, (k.name + ("*" if k.declared else ""))[:12], k.kind, mm["parts"], mm["bbox"][0], mm["bbox"][1], mm["bbox"][2], mm["bbox"][3],
            mm["area"], mm["fill"], mm["span"], note[:34]))
    if len(order) > len(shown):
        rest = order[len(shown):]
        L.append("   ... +%d more block(s): %s" % (len(rest), " ".join("%s(%d)" % (blocks[i].name, len(blocks[i].parts)) for i in rest[:8])))
    pairs = sorted(F["pairs"].items(), key=lambda kv: -kv[1])
    L.append("signal nets between blocks: " + (" ".join("%s-%s:%d" % (blocks[i].letter, blocks[j].letter, n) for (i, j), n in pairs[:10]) or "none")
             + (" ..." if len(pairs) > 10 else "") + "   block-level ratsnest crossings: %d" % F["x"])
    if F["conn"]:
        L.append("connectors: " + "  ".join("%s %s edge %.1f mm%s" % (c["block"], c["side"], c["mm"], " (INBOARD)" if c["inboard"] else "") for c in F["conn"][:8]))
    for r in F["esc"]:
        own_blk = blocks[F["owner"][r["ic"]]].name
        bad = [k for k in r["fan"] if k in F["foreign"].get(r["ic"], ())]
        own = [k for k in r["fan"] if k not in bad]
        tag = "FOREIGN IN FAN: " + ", ".join("%s %.0f mm2" % (k, r["fan"][k]) for k in bad) if bad else "fan clear of other blocks"
        far = [k for k in r["corridor_blocks"] if k not in r["fan"] and k in F["foreign"].get(r["ic"], ())]
        L.append("escape %s (%dp, fan %.1f mm): %s%s%s" % (
            r["side"], r["pads"], r["D"], tag, ("; support in fan: %s" % ", ".join("%s %.0f mm2" % (k, r["fan"][k]) for k in own)) if own else "",
            ("; then " + ", ".join("%s @%.0f" % (k, r["corridor_blocks"][k]) for k in far[:4])) if far else ""))
    for k in F["links"][:5]:
        L.append("link %-5s %-8s %s -> %s: %d nets, pad distance %.1f..%.1f mm (mean %.1f), limit %g" % (
            "LONG" if k["long"] else "ok", k["kind"], k["block"], k["ic"], k["nets"], k["min"], k["max"], k["mean"], k["limit"]))
    for g in F["regs"][:3]:
        L.append("regulator %s %s%s: caps %s%s" % (
            g["part"], g["value"], (" (host %s, %s pad %.1f mm)" % (g["host"], g["host_net"], g["host_pin_mm"])) if g["host"] else "",
            " ".join("%s %.1f%s" % (c["ref"], c["mm"], "!" if c["far"] else "") for c in g["caps"][:6]) or "none",
            "  span %.1f mm (! = beyond %g)%s%s" % (g["span"], REG_CAP_MM, ("; loop from block " + g["block"]) if g.get("block") else "",
                                                     ("; rail decaps not counted: " + " ".join("%s:%s" % (n, ",".join(v)) for n, v in g["rail_decaps"].items())) if g.get("rail_decaps") else "")))
    return L


def as_json(F, name=""):
    b, blocks = F["board"], F["blocks"]
    return {"name": name, "board": [round(v, 1) for v in b.bbox], "intent": F["ipen"],
            "blocks": [dict(name=k.name, letter=k.letter, kind=k.kind, declared=k.declared, parts=k.parts, n_parts=F["m"][i]["parts"],
                            **{a: v for a, v in F["m"][i].items() if a != "parts"}, intent=F["intent"].get(k.name, {}).get("penalty")) for i, k in enumerate(blocks)],
            "block_nets": [{"a": blocks[i].name, "b": blocks[j].name, "nets": n} for (i, j), n in sorted(F["pairs"].items(), key=lambda kv: -kv[1])],
            "ratsnest_crossings": F["x"], "connectors": F["conn"], "escape": F["esc"], "links": F["links"], "regulators": F["regs"]}


def main_args(a):
    import os
    from . import actions as A, cli, config, state
    if os.path.exists(a.target) and not os.path.isdir(a.target):
        board = cli.load_board(a.target)
        world = A.World(board)
        name = os.path.basename(a.target)
    else:
        st = state.State.load(a.target)
        world, name = st.world, st.name
        board = world.board
    P = config.current()
    fine = P.fine_pitch_parts(board)
    F = analyse(world, fine, a.depth)
    if a.json:
        print(json.dumps(as_json(F, name), indent=1, default=str))
    else:
        print("\n".join(text(F, name)))
    return 0
