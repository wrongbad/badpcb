"""Feasibility check for fine-pitch parts: can the effective design rules escape them at all?

Effective rules = Default netclass + board minimums in the .kicad_pro + the simple custom rules in the sibling
.kicad_dru (exactly the forms rules.py writes: `# badpcb-area NAME part=REF margin=M bbox=...` comments plus
`(rule .. (condition "A.intersectsArea('NAME') ..") (constraint clearance|track_width (min V)))`).

For every part with a pad pitch < PITCH_LIMIT (per side of the package) it computes
  pitch, pad width, gap, max track between pads, straight-out stub, 45 deg fan stubs, via-in-pad, staggered via escape
and says OK / FAIL with the precise limit. `region_grids` gives the coarsest routing grid that still resolves the pads
of each region; droute uses it for its auto grid.
"""
import math
import os
import re

PITCH_LIMIT = 0.65
NICE_GRIDS = (0.5, 0.25, 0.2, 0.125, 0.1, 0.05)
S45 = math.sqrt(0.5)


class Area:
    def __init__(self, name, part=None, margin=0.0, bbox=None):
        self.name, self.part, self.margin, self.bbox = name, part, margin, bbox
        self.clr = None
        self.track = None

    def resolve(self, board):
        if self.bbox is None and self.part in board.parts:
            b = board.parts[self.part].bbox
            self.bbox = (b[0] - self.margin, b[1] - self.margin, b[2] + self.margin, b[3] + self.margin)
        return self.bbox


class Eff:
    """Effective rules of a project."""

    def __init__(self):
        self.clr, self.track, self.via, self.drill = 0.2, 0.2, 0.6, 0.3
        self.min_track, self.min_via, self.min_drill = 0.15, 0.5, 0.3
        self.areas = []
        self.source = ""

    def at(self, board, bb):
        """(clearance, min track, area names) governing a box (x0,y0,x1,y1): areas that intersect it replace the defaults."""
        clr, tmin, names = self.clr, self.min_track, []
        for a in self.areas:
            ab = a.resolve(board)
            if ab and not (bb[2] < ab[0] or bb[0] > ab[2] or bb[3] < ab[1] or bb[1] > ab[3]):
                names.append(a.name)
                if a.clr is not None:
                    clr = a.clr
                if a.track is not None:
                    tmin = a.track
        return clr, tmin, names

    def line(self):
        s = "clr %.2f track %.2f (min %.2f) via %.2f/%.2f" % (self.clr, self.track, self.min_track, self.via, self.drill)
        for a in self.areas:
            s += "; area %s: clr %s track>=%s" % (a.name, "-" if a.clr is None else "%.2f" % a.clr, "-" if a.track is None else "%.2f" % a.track)
        return s


def _mm(v):
    m = re.match(r"^\s*(-?[0-9.]+)\s*(mm|mil|in)?\s*$", v)
    return float(m.group(1)) * {"mm": 1.0, "mil": 0.0254, "in": 25.4, None: 1.0}[m.group(2)]


def load_rules(pro, board=None):
    import json
    e = Eff()
    e.source = pro or ""
    if pro and os.path.exists(pro):
        d = json.load(open(pro))
        for c in d.get("net_settings", {}).get("classes", []):
            if c["name"] == "Default":
                e.clr, e.track, e.via, e.drill = c["clearance"], c["track_width"], c["via_diameter"], c["via_drill"]
        r = d.get("board", {}).get("design_settings", {}).get("rules", {})
        e.min_track = r.get("min_track_width", e.min_track)
        e.min_via = r.get("min_via_diameter", e.min_via)
        e.min_drill = r.get("min_through_hole_diameter", e.min_drill)
    elif board is not None:
        e.clr, e.track, e.via, e.drill, e.min_track = (board.rules.clearance, board.rules.track_width, board.rules.via_dia,
                                                       board.rules.via_drill, board.rules.min_track)
    dru = pro.rsplit(".", 1)[0] + ".kicad_dru" if pro else ""
    if dru and os.path.exists(dru):
        txt = open(dru).read()
        areas = {}
        for m in re.finditer(r"#\s*(?:rk|badpcb)-area (\S+) part=(\S+) margin=([0-9.]+) bbox=([-0-9.,]+)", txt):
            areas[m.group(1)] = Area(m.group(1), m.group(2), float(m.group(3)), tuple(float(x) for x in m.group(4).split(",")))
        for m in re.finditer(r'\(rule\s+"[^"]*"\s*\(condition\s+"([^"]*)"\)\s*\(constraint\s+(clearance|track_width)\s*\(min\s+([^)]*)\)\)', txt):
            cond, kind, val = m.group(1), m.group(2), _mm(m.group(3))
            for nm in re.findall(r"intersectsArea\('([^']+)'\)", cond) + re.findall(r"insideArea\('([^']+)'\)", cond):
                a = areas.setdefault(nm, Area(nm, part=nm.split("_")[0]))
                if kind == "clearance":
                    a.clr = val
                else:
                    a.track = val
            for ref in re.findall(r"insideCourtyard\('([^']+)'\)", cond):
                a = areas.setdefault("courtyard:" + ref, Area("courtyard:" + ref, part=ref))
                if kind == "clearance":
                    a.clr = val
                else:
                    a.track = val
        e.areas = list(areas.values())
    return e


# --------------------------------------------------------------------------- package geometry
def _extent(pd):
    """World-frame (dx, dy) of a pad (orthogonal rotations only)."""
    w, h = pd.size
    return (h, w) if int(round(pd.rot)) % 180 == 90 else (w, h)


def sides(part):
    """{'W'|'E'|'N'|'S': [pads sorted along the edge]} for the perimeter SMD pads of a part (centre pads dropped)."""
    pads = [p for p in part.pads if p.smd and p.size[0] > 0]
    if len(pads) < 4:
        return {}
    cx = sum(p.pos[0] for p in pads) / len(pads)
    cy = sum(p.pos[1] for p in pads) / len(pads)
    far = max(max(abs(p.pos[0] - cx), abs(p.pos[1] - cy)) for p in pads)
    out = {}
    for p in pads:
        dx, dy = p.pos[0] - cx, p.pos[1] - cy
        if max(abs(dx), abs(dy)) < 0.5 * far and len(pads) > 6:     # exposed pad / thermal pads
            continue
        if abs(dx) > abs(dy) * 1.0 + 1e-6 or (abs(abs(dx) - abs(dy)) < 1e-6 and abs(dx) > 0):
            key = "E" if dx > 0 else "W"
        else:
            key = "S" if dy > 0 else "N"
        out.setdefault(key, []).append(p)
    for k, L in out.items():
        ax = 1 if k in "WE" else 0
        L.sort(key=lambda p: p.pos[ax])
    return out


def side_geom(key, L):
    """-> (pitch, pad width along the edge, pad length) of a side group or None."""
    ax = 1 if key in "WE" else 0
    cs = sorted({round(p.pos[ax], 4) for p in L})
    if len(cs) < 2:
        return None
    d = [b - a for a, b in zip(cs, cs[1:]) if b - a > 1e-3]
    if not d:
        return None
    pitch = min(d)
    w = min(_extent(p)[ax] for p in L)
    ln = max(_extent(p)[1 - ax] for p in L)
    return pitch, w, ln


def fine_parts(board, limit=PITCH_LIMIT):
    """{fpid: [refs]} of parts with at least one perimeter side of pitch < limit; plus per-ref side geometry."""
    out, geo = {}, {}
    for ref, p in board.parts.items():
        S = sides(p)
        g = {k: side_geom(k, L) for k, L in S.items()}
        g = {k: v for k, v in g.items() if v}
        if g and min(v[0] for v in g.values()) < limit - 1e-9:
            out.setdefault(p.fpid, []).append(ref)
            geo[ref] = (S, g)
    return out, geo


def _pbox(pads):
    xs = [p.pos[0] for p in pads]
    ys = [p.pos[1] for p in pads]
    return (min(xs), min(ys), max(xs), max(ys))


def side_check(eff, board, pads, pitch, pw, ln, key):
    """Numbers + verdict for one package side."""
    clr, tmin, areas = eff.at(board, _pbox(pads))
    gap = pitch - pw
    r = {"side": key, "pitch": pitch, "pad_w": pw, "pad_len": ln, "gap": gap, "clr": clr, "track_min": tmin, "track_def": eff.track,
         "areas": areas, "n": len(pads)}
    r["via"] = eff.via
    r["max_track_between"] = max(0.0, gap - 2 * clr)
    r["between_ok"] = r["max_track_between"] >= tmin - 1e-9
    r["straight_req"] = tmin + clr
    r["straight_req_def"] = eff.track + clr
    r["straight_ok"] = pitch >= r["straight_req"] - 1e-9
    r["straight_ok_def"] = pitch >= r["straight_req_def"] - 1e-9
    r["fan"] = pitch * S45
    r["fan_req"] = tmin + clr
    r["fan_ok"] = r["fan"] >= r["fan_req"] - 1e-9
    r["fan_max_track"] = max(0.0, r["fan"] - clr)
    r["fan_clr_needed"] = r["fan"] - tmin                  # clearance must be <= this at the minimum track
    r["via_in_pad"] = pw >= eff.via - 1e-9 or min(pw, ln) >= eff.drill + 2 * 0.125
    r["stag_gap"] = 2 * pitch - eff.via                    # space between two vias of the short-stub row
    r["stag_req"] = tmin + 2 * clr                         # a long stub passes between them
    r["stag_ok"] = r["stag_gap"] >= r["stag_req"] - 1e-9
    r["ok"] = r["fan_ok"] or r["stag_ok"] or r["via_in_pad"]
    return r


def fmt_side(r):
    if r["ok"]:
        how = []
        if r["fan_ok"]:
            how.append("fan stubs %.3f mm apart >= %.2f (track %.2f + clr %.2f), max track in fan %.3f" % (r["fan"], r["fan_req"], r["track_min"], r["clr"], r["fan_max_track"]))
        if r["stag_ok"]:
            how.append("staggered via escape ok (via gap %.2f >= %.2f)" % (r["stag_gap"], r["stag_req"]))
        if r["via_in_pad"]:
            how.append("via-in-pad possible")
        s = "OK    %s side: pitch %.2f pad %.2fx%.2f gap %.2f; %s" % (r["side"], r["pitch"], r["pad_w"], r["pad_len"], r["gap"], "; ".join(how))
        if r["fan_ok"] and r["track_def"] > r["fan_max_track"] + 1e-9:
            s += "; default track %.2f is too wide there: fan needs track <= %.3f (rule area allows >= %.2f)" % (r["track_def"], r["fan_max_track"], r["track_min"])
        return s
    s = "FAIL  %s side: fan stubs %.3f mm apart < required %.2f (track %.2f + clr %.2f)" % (r["side"], r["fan"], r["fan_req"], r["track_min"], r["clr"])
    s += " -> needs local clearance <= %.3f (at track %.2f) or alternate-pin escape" % (r["fan_clr_needed"], r["track_min"])
    s += "; straight stub %s (pitch %.2f vs %.2f); staggered vias FAIL (via gap %.2f < %.2f); via-in-pad no (pad %.2f < via %.2f)" % (
        "ok" if r["straight_ok"] else "FAIL", r["pitch"], r["straight_req"], r["stag_gap"], r["stag_req"], r["pad_w"], r["via"])
    return s


def check(board, eff):
    """-> [(fpid, refs, [side results])] for every fine-pitch footprint (one representative part each)."""
    fp, geo = fine_parts(board)
    res = []
    for fpid, refs in sorted(fp.items()):
        rep = None
        best = None
        for ref in refs:                                    # representative = the instance in the tightest rules
            S, g = geo[ref]
            rr = [side_check(eff, board, S[k], *g[k], k) for k in sorted(g)]
            sc = sum(1 for r in rr if not r["ok"])
            if best is None or sc > best:
                best, rep = sc, (ref, rr)
        res.append((fpid, refs, rep[0], rep[1]))
    return res


def region_grids(ctx, eff, check_res=None):
    """{region id: (step, why)}: coarsest nice grid that (a) is <= track+clr (tracks on neighbouring cells stay legal),
    (b) divides the pad pitch of every fine-pitch part in the region, (c) <= the fan spacing when a fine part is there."""
    board, T = ctx.board, ctx.T
    fp, geo = fine_parts(board)
    out = {}
    for R in T.regions.values():
        x0, y0, x1, y1 = R.bbox
        clr, tmin, _ = eff.at(board, R.bbox)
        step_cap = eff.track + clr
        why = "key-field/long runs: <= track+clr %.2f" % step_cap
        pitches = set()
        refs = []
        for ref, (S, g) in geo.items():
            c = board.parts[ref].pos
            if x0 <= c[0] <= x1 and y0 <= c[1] <= y1:
                refs.append(ref)
                pitches |= {round(v[0], 3) for v in g.values()}
        if pitches:
            cap = min(p * S45 for p in pitches) - clr         # fan: grid must resolve the track width available
            cap = max(cap, 0.05)
            step_cap = min(step_cap, cap + 0.05) if cap < step_cap else step_cap
            step_cap = min(step_cap, min(pitches) / 2)
            why = "fine-pitch %s (pitch %s): grid must divide the pitch and be <= %.2f" % (",".join(refs), "/".join("%.2f" % p for p in sorted(pitches)), step_cap)
        pick = None
        for g in NICE_GRIDS:
            if g > step_cap + 1e-9:
                continue
            if all(abs(p / g - round(p / g)) < 1e-6 for p in pitches):
                pick = g
                break
        out[R.id] = (pick or 0.05, why)
    return out


def grid_for_box(ctx, eff, bb):
    """Auto grid for a scope: the finest of the grids of the regions its box touches."""
    rg = region_grids(ctx, eff)
    hit = [(g, rid, why) for rid, (g, why) in rg.items()
           if not (bb[2] < ctx.T.regions[rid].bbox[0] or bb[0] > ctx.T.regions[rid].bbox[2] or bb[3] < ctx.T.regions[rid].bbox[1] or bb[1] > ctx.T.regions[rid].bbox[3])]
    if not hit:
        return 0.25, "no region"
    g, rid, why = min(hit)
    return g, "%s: %s" % (rid, why)


def sec_feasibility(c, full):
    b = c.board
    eff = c.eff
    L = ["effective rules (%s): %s" % (os.path.basename(eff.source) or "board", eff.line())]
    res = check(b, eff)
    if not res:
        L.append("no fine-pitch (< %.2f mm) parts" % PITCH_LIMIT)
    for fpid, refs, rep, rr in res:
        bad = [r for r in rr if not r["ok"]]
        tag = ("%s" % rep) if len(refs) == 1 else "%s (+%d like it: %s)" % (rep, len(refs) - 1, " ".join(r for r in refs if r != rep)[:60])
        L.append("%s FEASIBILITY: %s   [%s]" % (tag, "FAIL" if bad else "OK", fpid))
        for r in (bad if bad and not full else rr):
            L.append("  " + fmt_side(r))
        if not full and bad and len(bad) < len(rr):
            L.append("  (%d of %d sides OK)" % (len(rr) - len(bad), len(rr)))
        if not bad:
            r0 = rr[0]
            L.append("  e.g. %s side: gap %.2f, max track between pads %.2f%s; straight stub needs %.2f (pitch %.2f) %s" % (
                r0["side"], r0["gap"], r0["max_track_between"], "" if r0["between_ok"] else " (below min track %.2f: no track fits between pads)" % r0["track_min"],
                r0["straight_req"], r0["pitch"], "ok" if r0["straight_ok"] else "FAIL"))
    rg = region_grids(c, eff)
    groups = {}
    for rid, (g, why) in rg.items():
        key = (g, re.sub(r"fine-pitch [\w,]+ ", "fine-pitch parts ", why))
        groups.setdefault(key, []).append(rid)
    for (g, why), rids in sorted(groups.items(), key=lambda kv: kv[0][0]):
        names = " ".join(rids) if len(rids) <= 4 else "%s..%s (%d regions)" % (rids[0], rids[-1], len(rids))
        L.append("  min grid step %s: <= %g mm  (%s)" % (names, g, why))
    return L
