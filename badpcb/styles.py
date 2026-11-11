"""Net styles: soft per-net routing/lint preferences from badpcb.json.

    "net_styles": {"usb":  {"match": ["USB_D*"], "pair": "USB_DP/USB_DN", "skew_mm": 0.5, "via_penalty": 8, "keep_ref_plane": true, "priority": true},
                   "sw":   {"match": ["VREG_LX"], "via_penalty": 6, "detour_max": 1.2, "avoid": ["XIN", "XOUT", "ADC*"]},
                   "pwr":  {"class": "Power", "via_penalty": 2}}

A net gets a style by name pattern (`match`, fnmatch globs; an exact net name works too) OR by KiCad netclass (`class`, read from the board's
.kicad_pro: `net_settings.netclass_assignments` and `net_settings.netclass_patterns`). Styles stack: when a net matches several, the
stricter value per key wins (max via_penalty, min detour_max, min skew_mm, union of avoid / pair, OR of the flags).

Everything is a soft preference (groute costs, lint weights, droute pass settings): a style never vetoes a route.
Keys: match, class, via_penalty (extra via cost, same unit as groute --via-cost: mm of path), detour_max (routed length / direct length the net
should stay under), avoid (net globs to keep away from), pair ("P/N" or ["P", "N"]), skew_mm (max P/N length difference), keep_ref_plane
(stay over the reference plane: lint weights splits x3), priority (route first, alone, with stricter settings).
"""
import fnmatch
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

PRIORITY_VIA = 8.0           # a style with via_penalty >= this routes in the priority pass
STYLE_KEYS = ("match", "class", "via_penalty", "detour_max", "avoid", "pair", "skew_mm", "keep_ref_plane", "priority")


class StyleError(ValueError):
    pass


def parse(raw):
    """Validate the `net_styles` object -> {name: normalised dict}. Raises StyleError."""
    out = {}
    if raw in (None, {}):
        return out
    if not isinstance(raw, dict):
        raise StyleError("net_styles must be an object {name: style}")
    for name, s in raw.items():
        if name.startswith("_"):
            continue
        if not isinstance(s, dict):
            raise StyleError("style %r: expected an object" % name)
        bad = [k for k in s if k not in STYLE_KEYS and not k.startswith("_")]
        if bad:
            raise StyleError("style %r: unknown key(s) %s (known: %s)" % (name, ", ".join(bad), ", ".join(STYLE_KEYS)))

        def lst(k):
            v = s.get(k) or []
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise StyleError("style %r: %r must be a string or a list of strings" % (name, k))
            return list(v)

        def num(k):
            v = s.get(k)
            if v is None:
                return None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise StyleError("style %r: %r must be a number" % (name, k))
            return float(v)
        pair = s.get("pair")
        if pair is not None and not (isinstance(pair, str) or (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair))):
            raise StyleError("style %r: pair must be \"P/N\" or [\"P\", \"N\"]" % name)
        cls = s.get("class")
        if cls is not None and not isinstance(cls, str):
            raise StyleError("style %r: class must be a netclass name" % name)
        vp, dm, sk = num("via_penalty"), num("detour_max"), num("skew_mm")
        if vp is not None and vp < 0:
            raise StyleError("style %r: via_penalty must be >= 0" % name)
        if dm is not None and dm < 1.0:
            raise StyleError("style %r: detour_max is a ratio >= 1.0" % name)
        out[name] = {"match": lst("match"), "class": cls, "via_penalty": vp, "detour_max": dm, "avoid": lst("avoid"), "pair": pair,
                     "skew_mm": sk, "keep_ref_plane": bool(s.get("keep_ref_plane", False)), "priority": bool(s.get("priority", False))}
        if not out[name]["match"] and not cls and not pair:
            raise StyleError("style %r: needs `match`, `class` or `pair` to select nets" % name)
    return out


def split_pair(spec, nets):
    """"P/N" (or [P, N]) -> (P, N) when both are nets of the board, else None. Net names may themselves contain '/' (e.g. "/USB_DP")."""
    nets = set(nets)
    if isinstance(spec, (list, tuple)):
        return (spec[0], spec[1]) if spec[0] in nets and spec[1] in nets else None
    for i, ch in enumerate(spec):
        if ch != "/":
            continue
        l, r = spec[:i], spec[i + 1:]
        for rr in (r, "/" + r):
            if l in nets and rr in nets:
                return (l, rr)
    return None


# ----------------------------------------------------------------------------- KiCad netclasses
def read_netclasses(pro_path):
    """.kicad_pro -> ({net: [classes]} from netclass_assignments, [(glob, class)] from netclass_patterns, [known class names]). Missing file -> empty."""
    if not pro_path or not os.path.isfile(pro_path):
        return {}, [], []
    try:
        with open(pro_path) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}, [], []
    ns = d.get("net_settings") or {}
    assign = {}
    for net, c in (ns.get("netclass_assignments") or {}).items():
        assign[net] = [c] if isinstance(c, str) else list(c or [])
    pats = [(p.get("pattern", ""), p.get("netclass", "")) for p in (ns.get("netclass_patterns") or []) if isinstance(p, dict)]
    names = [c.get("name") for c in (ns.get("classes") or []) if isinstance(c, dict)]
    return assign, pats, names


def netclasses_of(nets, pro_path):
    """{net: sorted netclass names it is explicitly assigned to (exact assignment or pattern)}; nets with none are absent."""
    assign, pats, _ = read_netclasses(pro_path)
    out = {}
    for n in nets:
        cs = set(assign.get(n, ()))
        for pat, c in pats:
            if pat and c and (pat == n or fnmatch.fnmatchcase(n, pat)):
                cs.add(c)
        if cs:
            out[n] = sorted(cs)
    return out


# ----------------------------------------------------------------------------- resolution
@dataclass
class NetStyle:
    net: str
    styles: List[str] = field(default_factory=list)
    via_penalty: float = 0.0
    detour_max: Optional[float] = None
    avoid: List[str] = field(default_factory=list)      # net globs (union over the matched styles)
    pair: List[tuple] = field(default_factory=list)     # [(P, N)] resolved against the board's nets
    skew_mm: Optional[float] = None
    keep_ref_plane: bool = False
    priority: bool = False
    classes: List[str] = field(default_factory=list)    # KiCad netclasses of the net (info)
    via: str = ""                                       # why: "name"/"class"/"pair" per matched style (info)

    def partner(self):
        for p, n in self.pair:
            if self.net == p:
                return n
            if self.net == n:
                return p
        return None

    def as_dict(self):
        return {"net": self.net, "styles": self.styles, "via_penalty": self.via_penalty, "detour_max": self.detour_max, "avoid": self.avoid,
                "pair": ["%s/%s" % x for x in self.pair], "skew_mm": self.skew_mm, "keep_ref_plane": self.keep_ref_plane,
                "priority": self.priority, "classes": self.classes}


def resolve(nets, styles, pro_path=None):
    """-> ({net: NetStyle} for nets with at least one style, {style name: how many nets it matched}). `styles` = parse() output."""
    nets = list(nets)
    ncl = netclasses_of(nets, pro_path) if any(s["class"] for s in styles.values()) else {}
    out, count = {}, {}
    for name, s in styles.items():
        hit = {}
        for n in nets:
            if any(n == pat or fnmatch.fnmatchcase(n, pat) for pat in s["match"]):
                hit[n] = "name"
            elif s["class"] and s["class"] in ncl.get(n, ()):
                hit[n] = "class"
        pr = split_pair(s["pair"], nets) if s["pair"] else None
        if pr:
            for n in pr:
                hit.setdefault(n, "pair")
        count[name] = len(hit)
        for n, why in hit.items():
            ns = out.setdefault(n, NetStyle(n, classes=ncl.get(n, [])))
            ns.styles.append(name)
            ns.via += ("," if ns.via else "") + why
            if s["via_penalty"] is not None:
                ns.via_penalty = max(ns.via_penalty, s["via_penalty"])
            if s["detour_max"] is not None:
                ns.detour_max = s["detour_max"] if ns.detour_max is None else min(ns.detour_max, s["detour_max"])
            if s["skew_mm"] is not None:
                ns.skew_mm = s["skew_mm"] if ns.skew_mm is None else min(ns.skew_mm, s["skew_mm"])
            for a in s["avoid"]:
                if a not in ns.avoid:
                    ns.avoid.append(a)
            if pr and pr not in ns.pair:
                ns.pair.append(pr)
            ns.keep_ref_plane = ns.keep_ref_plane or s["keep_ref_plane"]
            ns.priority = ns.priority or s["priority"]
    return out, count


def board_pro(board):
    """The .kicad_pro netclasses are read from: the board's own sibling, else the configured rules project."""
    from . import config
    src = getattr(board, "source", "") or ""
    if src.endswith(".kicad_pcb"):
        p = src[:-len(".kicad_pcb")] + ".kicad_pro"
        if os.path.isfile(p):
            return p
    p = config.current().rules_pro
    return p if p and os.path.isfile(p) else (src[:-len(".kicad_pcb")] + ".kicad_pro" if src.endswith(".kicad_pcb") else None)


def for_board(board, raw=None, pro_path=None):
    """Resolved styles of `board` from badpcb.json `net_styles` (or `raw`): {net: NetStyle}; {} when none are configured."""
    from . import config
    st = parse(raw) if raw is not None else config.current().net_styles()
    if not st:
        return {}
    return resolve(board.nets, st, pro_path or board_pro(board))[0]


def priority_nets(resolved, via_threshold=PRIORITY_VIA):
    """Nets routed first in their own pass: style `priority`, or via_penalty >= threshold, or a pair style."""
    return sorted(n for n, s in resolved.items() if s.priority or s.via_penalty >= via_threshold or s.pair)


# ----------------------------------------------------------------------------- CLI: badpcb styles BOARD
def table(board, resolved, counts, styles, pro_path):
    L = []
    ncl = netclasses_of(board.nets, pro_path)
    L.append("net styles: %d defined, %d of %d nets styled; netclasses from %s (%d net(s) with an explicit class)" % (
        len(styles), len(resolved), len(board.nets), pro_path if pro_path and os.path.isfile(pro_path) else "(no .kicad_pro found)", len(ncl)))
    dead = [n for n, c in counts.items() if not c]
    L.append("%-16s %-12s %5s %6s %-22s %-24s %s" % ("net", "styles", "viaP", "detour", "avoid", "pair/skew", "flags"))
    for n in sorted(resolved, key=lambda x: (-resolved[x].via_penalty, x)):
        s = resolved[n]
        L.append("%-16s %-12s %5.1f %6s %-22s %-24s %s" % (
            n[:16], ",".join(s.styles)[:12], s.via_penalty, ("%.2f" % s.detour_max) if s.detour_max else "-", ",".join(s.avoid)[:22] or "-",
            ("%s%s" % (",".join("%s/%s" % x for x in s.pair), (" skew<=%.2g" % s.skew_mm) if s.skew_mm is not None else ""))[:24] or "-",
            " ".join(f for f, on in (("ref_plane", s.keep_ref_plane), ("priority", s.priority), ("class:" + "+".join(s.classes), bool(s.classes))) if on) or "-"))
    if dead:
        L.append("WARN styles matching no net: %s" % ", ".join(dead))
    if not styles:
        L.append("no `net_styles` in badpcb.json: every net routes with the global settings (unchanged behaviour)")
    pr = priority_nets(resolved)
    if pr:
        L.append("priority pass nets (droute --priority): %s" % " ".join(pr))
    return L


def main_args(a):
    from . import cli, config
    P = config.current()
    b = cli.load_board(a.board)
    try:
        st = P.net_styles()
    except StyleError as e:
        raise SystemExit("badpcb: bad net_styles in badpcb.json: %s" % e)
    pro = board_pro(b)
    resolved, counts = resolve(b.nets, st, pro)
    if a.json:
        print(json.dumps({"styles": st, "matched": counts, "nets": {n: s.as_dict() for n, s in resolved.items()}}, indent=1))
        return
    print("\n".join(table(b, resolved, counts, st, pro)))
