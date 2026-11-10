"""Lock awareness: classify existing copper and explain which locked copper blocks a connection.

Classes (Track.cls / Via.cls, set by `classify`; independent of the current lock flag so that an unlocked item keeps its class):
  <configured classes>  copper whose uuids a project lists in badpcb.json "locks.classes": {"name": "file[:key]"} or
                        {"name": {"file": "f.json", "key": "k", "protect": true, "trim": true}}; file = JSON list of uuids or dict
                        (+ key); first match in config order wins. Properties (default off):
                          protect  apply keeps the copper unless a changed pad shorts or breaks it; legal/stubs treat it as hard copper
                          trim     the class is eligible for `trim` (a dict here overrides the "locks.trim" parameters for this class)
  fanout  every other locked copper item (fan-out stubs, plane vias)
  free    everything else (router output): always rippable
The package default has no configured classes.

`unlock TARGET` / `lock TARGET` flip the lock flag of the matching copper (apply.py writes that to the board, so KRT may rip it);
TARGET = class (plural accepted), a net / glob (SENSE4, EN*), a region (R8) or a box x0,y0,x1,y1.
`trim [CLASS]` (alias `trim-lanes`) deletes, for every trim-enabled class (or just CLASS), horizontal segments past the last key of
each chain and chain ends that sit on another net's pad/via (badpcb.json "locks.trim": {"nets": regex of nets to trim, "key_parts": regex of the
refdes that count as keys, "start_part": ref a trunk starts at, "band_depth": mm}).
"""
import fnmatch
import json
import math
import os
import re

from . import geom

from . import config

_ALIASES = {"stubs": "fanout", "fanouts": "fanout", "locked": "all"}

_cache = {}


def class_specs():
    """[(name, json path, key)] of the project's configured lock classes, in precedence order."""
    return [(n, p, k) for n, (p, k) in config.current().lock_classes().items()]


def class_names():
    return tuple(n for n, _, _ in class_specs()) + ("fanout", "free")


def class_alias(word):
    """'lanes' -> 'lane', 'all' -> 'all', ... or None when `word` names no class of this project."""
    w = word.strip().lower()
    names = set(class_names())
    if w == "all":
        return "all"
    if w in names:
        return w
    if w.endswith("s") and w[:-1] in names:
        return w[:-1]
    return _ALIASES.get(w) if _ALIASES.get(w) in names | {"all"} else None


def protected(cl):
    """True when lock class `cl` has the `protect` property."""
    return bool(cl) and config.current().lock_props(cl)["protect"]


def protected_ids(board):
    """{uuid: class name} of the copper of protect-enabled classes."""
    return {i.id: cls_of(i) for i in _items(board) if i.id and protected(getattr(i, "cls", ""))}


def trim_classes(cls=None):
    """Names of trim-enabled classes (just `cls` when given; ValueError when it is not trim-enabled)."""
    names = [n for n, _, _ in class_specs() if config.current().lock_props(n)["trim"]]
    if cls:
        c = class_alias(cls)
        if c not in names:
            raise ValueError("trim: '%s' is not a trim-enabled lock class (%s)" % (cls, ", ".join(names) or "none configured"))
        return [c]
    return names


def label(cl):
    for n, p, _ in class_specs():
        if n == cl:
            return "%s (%s)" % (n, os.path.basename(p))
    return {"fanout": "fanout (other locked copper)", "free": "free copper"}.get(cl, cl)


def _ids(path, key=None):
    if path not in _cache or _cache[path][0] != os.path.getmtime(path):
        d = json.load(open(path))
        _cache[path] = (os.path.getmtime(path), set(d[key] if key else d))
    return _cache[path][1]


def classify(board):
    specs = [(n, _ids(p, k)) for n, p, k in class_specs() if os.path.exists(p)]
    for it in list(board.tracks) + list(board.vias):
        it.cls = next((n for n, ids in specs if it.id in ids), "fanout" if it.locked else "free")
    return board


def cls_of(it):
    return getattr(it, "cls", "") or ("fanout" if it.locked else "free")


def lock_tag(it):
    """'LOCKED lane' / 'LOCKED mimic' / 'LOCKED fanout' / 'lane (unlocked)' / 'free'."""
    c = cls_of(it)
    if it.locked:
        return "LOCKED " + c
    return "free" if c == "free" else "%s (unlocked)" % c


def _items(board):
    return list(board.tracks) + list(board.vias)


def _pos(it):
    return it.a if hasattr(it, "a") else it.pos


def resolve(board, target, regions=None):
    """Copper items (tracks + vias) a lock target refers to; raises ValueError with the legal forms on a bad target."""
    t = target.strip()
    cl = class_alias(t)
    if cl == "all":
        return [i for i in _items(board) if i.cls != "free"], "every classified (non-free) copper item"
    if cl:
        return [i for i in _items(board) if cls_of(i) == cl], "class %s" % cl
    bb = None
    if regions and t.upper() in regions:
        bb = regions[t.upper()]
    elif re.match(r"^-?[\d.]+(,-?[\d.]+){3}$", t):
        bb = tuple(float(x) for x in t.split(","))
    if bb:
        def inside(it):
            if hasattr(it, "a"):
                return geom.seg_hits_box(it.a, it.b, bb) if hasattr(geom, "seg_hits_box") else _seg_box(it.a, it.b, bb)
            return bb[0] <= it.pos[0] <= bb[2] and bb[1] <= it.pos[1] <= bb[3]
        return [i for i in _items(board) if inside(i)], "box %.1f,%.1f..%.1f,%.1f" % bb
    nets = [n for n in board.nets if n == t or fnmatch.fnmatchcase(n, t)]
    if nets:
        s = set(nets)
        return [i for i in _items(board) if i.net in s], "net(s) " + " ".join(nets[:6])
    raise ValueError("unknown lock target '%s': use a class (%s|all), a net or glob (e.g. SENSE4, EN*), "
                     "a region (R8) or a box x0,y0,x1,y1" % (target, "|".join(class_names())))


def _seg_box(a, b, bb):
    lo = (min(a[0], b[0]), min(a[1], b[1]))
    hi = (max(a[0], b[0]), max(a[1], b[1]))
    return not (hi[0] < bb[0] or lo[0] > bb[2] or hi[1] < bb[1] or lo[1] > bb[3])


def summary(board):
    """{class: {'locked': n, 'unlocked': n, 'nets': {net: n}}} over tracks+vias."""
    out = {c: {"locked": 0, "unlocked": 0, "nets": {}} for c in class_names()}
    for it in _items(board):
        c = cls_of(it)
        out[c]["locked" if it.locked else "unlocked"] += 1
        out[c]["nets"][it.net] = out[c]["nets"].get(it.net, 0) + 1
    return out


# --------------------------------------------------------------------------- trim
def trim_chains(board, cls):
    """[(net, y, [segments sorted by x])] for horizontal tracks of lock class `cls` (locked or not)."""
    d = {}
    for t in board.tracks:
        if cls_of(t) == cls and abs(t.a[1] - t.b[1]) < 1e-6:
            d.setdefault((t.net, round(t.a[1], 3), t.layer), []).append(t)
    return [(k[0], k[1], sorted(v, key=lambda t: min(t.a[0], t.b[0]))) for k, v in sorted(d.items())]


def trim_candidates(board, cls=None, near=10.0):
    """[(track, reason)] segments of the trim-enabled classes (or of `cls`) that can go.
    1. past the last key: no pad/via of the chain's net within `near` mm (vertically) at or beyond the segment start -> the
       segment runs on past the last key it serves;
    2. ends on another net's pad/via (within clearance): it blocks that pad's escape (what prune_copper removes)."""
    out = {}
    for c in trim_classes(cls):
        _trim_class(board, c, near, out)
    return list(out.values())


def _trim_class(board, cls, near, out):
    rl = board.rules
    pads_by_net = {}
    for pd in board.pads():
        if pd.net:
            pads_by_net.setdefault(pd.net, []).append(pd)
    tr = config.current().lock_trim(cls)
    net_rx = re.compile(tr.get("nets", ".*"))
    key_rx = re.compile(tr["key_parts"]) if tr.get("key_parts") else None
    start = tr.get("start_part")
    chains = [c for c in trim_chains(board, cls) if net_rx.match(c[0])]      # only the configured nets are trimmed
    band_a_y = min((y for _, y, _ in chains), default=0.0) + float(tr.get("band_depth", 5.0))     # band A = the first (lowest-y) band: trunks that start at `start_part`
    mcu_x = board.parts[start].pos[0] if start and start in board.parts else None
    for net, y, segs in chains:
        band_a = bool(start) and y <= band_a_y
        keys = [pd.pos[0] for pd in pads_by_net.get(net, []) if (key_rx is None or key_rx.match(pd.ref)) and (band_a or abs(pd.pos[1] - y) <= near)]
        if band_a and mcu_x is not None:
            keys.append(mcu_x)                                     # a trunk starts at the start part's column
        if not keys:
            for t in segs:
                out[id(t)] = (t, "%s %s y=%.1f has no key pad within %.0f mm" % (cls, net, y, near))
            continue
        lo, hi = min(keys), max(keys)
        for t in segs:
            x0, x1 = sorted((t.a[0], t.b[0]))
            if x0 >= hi - 1e-6:
                out[id(t)] = (t, "%s %s y=%.1f runs %.1f mm past its last key (x=%.1f)" % (cls, net, y, x1 - hi, hi))
            elif x1 <= lo + 1e-6:
                out[id(t)] = (t, "%s %s y=%.1f runs %.1f mm before its first key (x=%.1f)" % (cls, net, y, lo - x0, lo))
    # 2. copper on foreign pads / vias (same layer as the segment)
    for net, y, segs in trim_chains(board, cls):
        for t in segs:
            if id(t) in out:
                continue
            for pd in board.pads():
                if pd.net == t.net or t.layer not in pd.layers or not pd.net:
                    continue
                if (abs(pd.pos[0] - (t.a[0] + t.b[0]) / 2) > abs(t.a[0] - t.b[0]) / 2 + max(pd.size) + 1 or abs(pd.pos[1] - t.a[1]) > max(pd.size) + 1):
                    continue
                if _seg_pad_dist(t.a, t.b, pd) - t.w / 2 < rl.clearance - 1e-6:
                    out[id(t)] = (t, "%s %s y=%.1f segment x=%.1f..%.1f is %.2f mm from pad %s (%s): blocks its escape" % (
                        cls, net, y, min(t.a[0], t.b[0]), max(t.a[0], t.b[0]), max(0.0, _seg_pad_dist(t.a, t.b, pd) - t.w / 2), pd.id, pd.net))
                    break
            else:
                for v in board.vias:
                    if v.net != t.net and geom.pt_seg_dist(v.pos, t.a, t.b) - t.w / 2 - v.dia / 2 < rl.clearance - 1e-6:
                        out[id(t)] = (t, "%s %s y=%.1f segment x=%.1f..%.1f touches the via of %s at (%.1f,%.1f)" % (
                            cls, net, y, min(t.a[0], t.b[0]), max(t.a[0], t.b[0]), v.net, *v.pos))
                        break


def _seg_pad_dist(a, b, pd):
    """Distance from segment a-b to the pad's bounding rectangle (rotation-aware, 0 = overlap)."""
    la, lb = geom.to_local(a, pd.pos, pd.rot), geom.to_local(b, pd.pos, pd.rot)
    hx, hy = pd.size[0] / 2, pd.size[1] / 2
    best = 1e9
    for i in range(41):
        p = (la[0] + (lb[0] - la[0]) * i / 40, la[1] + (lb[1] - la[1]) * i / 40)
        best = min(best, math.hypot(max(abs(p[0]) - hx, 0), max(abs(p[1]) - hy, 0)))
    return best


# --------------------------------------------------------------------------- blocking analysis
def _seg_seg_dist(a, b, c, d):
    if geom.seg_intersect(a, b, c, d):
        return 0.0
    return min(geom.pt_seg_dist(a, c, d), geom.pt_seg_dist(b, c, d), geom.pt_seg_dist(c, a, b), geom.pt_seg_dist(d, a, b))


def straight_blockage(board, conn, step=0.25):
    """For the straight line of one open connection: per signal layer the fraction of its length lying within
    clearance + half-width of foreign copper (tracks, vias, pads, other nets), and who blocks it.
    -> {layer: (fraction, {(net, tag): length_mm})}"""
    rl = board.rules
    r = rl.clearance + rl.track_width / 2
    a, b = conn.pa, conn.pb
    L = geom.dist(a, b)
    n = max(2, int(L / step))
    pts = [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]
    x0, x1 = min(a[0], b[0]) - 2, max(a[0], b[0]) + 2
    y0, y1 = min(a[1], b[1]) - 2, max(a[1], b[1]) + 2
    sig = board.signal_layers()
    planes = set(board.plane_nets())
    cand_t = [t for t in board.tracks if t.net != conn.net and t.net not in planes and t.layer in sig and
              not (max(t.a[0], t.b[0]) < x0 or min(t.a[0], t.b[0]) > x1 or max(t.a[1], t.b[1]) < y0 or min(t.a[1], t.b[1]) > y1)]
    cand_v = [v for v in board.vias if v.net != conn.net and v.net not in planes and x0 <= v.pos[0] <= x1 and y0 <= v.pos[1] <= y1]
    cand_p = [p for p in board.pads() if p.net != conn.net and p.net not in planes and x0 <= p.pos[0] <= x1 and y0 <= p.pos[1] <= y1]
    out = {}
    for layer in sig:
        who = {}
        blocked = 0
        for p in pts:
            hit = None
            for t in cand_t:
                if t.layer == layer and geom.pt_seg_dist(p, t.a, t.b) < r + t.w / 2:
                    hit = (t.net, lock_tag(t) + (" " + cls_of(t) if protected(cls_of(t)) and not t.locked else ""))
                    if t.locked:
                        break
            if hit is None:
                for v in cand_v:
                    if geom.dist(p, v.pos) < r + v.dia / 2:
                        hit = (v.net or "-", lock_tag(v) + " via")
                        break
            if hit is None:
                for pd in cand_p:
                    if layer in pd.layers and geom.dist(p, pd.pos) < max(pd.size) / 2 + r and _pt_pad(p, pd) < r:
                        hit = (pd.net or "-", "pad %s" % pd.id)
                        break
            if hit:
                blocked += 1
                who[hit] = who.get(hit, 0.0) + L / n
        out[layer] = (blocked / (n + 1), who)
    return out


def _pt_pad(p, pd):
    l = geom.to_local(p, pd.pos, pd.rot)
    return math.hypot(max(abs(l[0]) - pd.size[0] / 2, 0), max(abs(l[1]) - pd.size[1] / 2, 0))


def locked_blockage(board, conn):
    """-> (fraction, layer, {(net, tag): mm}, {layer: total fraction}) for the signal layer on which LOCKED copper covers most of the straight path."""
    res = straight_blockage(board, conn)
    tot = max(1e-9, geom.dist(conn.pa, conn.pb))
    best = None
    for layer, (frac, who) in res.items():
        lw = {k: v for k, v in who.items() if k[1].startswith("LOCKED")}
        lf = min(1.0, sum(lw.values()) / tot)
        if best is None or lf > best[0]:
            best = (lf, layer, lw)
    return best[0], best[1], best[2], {l: f for l, (f, _) in res.items()}


def describe_blockage(board, conn, min_frac=0.15):
    """One line: 'ADC7 U1.5->R3.1: 88% of straight path blocked on F.Cu by LOCKED SENSE4 lane (lanes.json) 85%; ...', or None
    when locked copper covers less than `min_frac` of the straight line on every signal layer."""
    frac, layer, who, tot = locked_blockage(board, conn)
    if frac < min_frac or not who:
        return None
    parts = []
    ln = max(1e-9, geom.dist(conn.pa, conn.pb))
    for (net, tag), mm in sorted(who.items(), key=lambda kv: -kv[1])[:3]:
        w = tag.split()
        cl = w[1] if len(w) > 1 else ""
        what = "LOCKED %s %s" % (net, label(cl) if cl else " ".join(w[1:]))
        parts.append("%s %.0f%%" % (what, 100 * mm / ln))
    others = ", ".join("%s %.0f%%" % (l, 100 * f) for l, f in tot.items() if l != layer)
    return "%s %s->%s: %.0f%% of straight path blocked on %s by %s%s" % (
        conn.net, conn.a, conn.b, 100 * frac, layer, "; ".join(parts), (" (all copper on other layers: %s)" % others) if others else "")
